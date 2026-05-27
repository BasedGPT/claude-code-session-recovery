r"""
Restore deleted Claude Code JSONL transcripts from Windows Volume Shadow Copies (VSS).

Scans all available VSS shadow copies for JSONL transcript files that are missing
from or smaller than their counterpart in ~/.claude/projects/. Selects the largest
version of each file across all shadow copies (append-only files, so largest =
most complete), then restores it.

Requires elevation -- VSS shadow copy access requires administrator privileges.
Run from an elevated PowerShell or Command Prompt.

Files read:
  - \\?\GLOBALROOT\Device\HarddiskVolumeShadowCopyN\Users\<user>\.claude\projects\**\*.jsonl

Files written (--apply only):
  - %USERPROFILE%\.claude\projects\<slug>\<session-id>.jsonl

Usage:
  py -3 restore_from_vss.py                        # dry-run (default)
  py -3 restore_from_vss.py --apply                # restore candidates
  py -3 restore_from_vss.py --projects-dir <path>  # override default projects dir

Restoring from VSS -- read this first:
  This tool preserves original filesystem mtimes when copying. Claude Code's
  cleanup (cleanupOldSessionFiles) deletes JSONLs based on mtime, not message
  timestamp. If you restore files while cleanupPeriodDays is at its default
  (30 days), any JSONL with an mtime older than 30 days will be re-deleted on
  the next Claude Desktop launch.

  Before running with --apply:
    1. Open ~/.claude/settings.json
    2. Set "cleanupPeriodDays": 36500  (approximately 100 years)
    3. Run this script with --apply, then run synth_session_metadata.py to restore visibility
    4. Revert cleanupPeriodDays to your preferred value once sessions are recovered

  This applies to any restore method that preserves original file timestamps
  (this script, manual zip extraction, or VSS/Time Machine snapshot tools).
"""

import argparse
import atexit
import io
import os
import re
import shutil
import subprocess
import sys

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except AttributeError:
    pass  # no buffer when stdout is None (e.g. pythonw.exe or redirected NUL)

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOL_DIR)
from lock_utils import acquire_lock, release_lock  # noqa: E402

# --- Configuration ---
# Where JSONL transcripts live. Override with --projects-dir for testing.
PROJECTS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "projects")

# Lock file -- prevents concurrent runs.
LOCK_FILE = os.path.join(TOOL_DIR, "restore_from_vss.lock")

# --- End configuration ---

# WMI datetime format: YYYYMMDDHHMMSS.ffffff+offset
_WMI_DT_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})")


# ---------------------------------------------------------------------------
# Admin + VSS availability
# ---------------------------------------------------------------------------

def _is_admin():
    """Return True if the current process has administrator privileges."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _check_vss_available():
    """
    Return True if vssadmin reports shadow storage on any volume.
    Does not filter by drive letter -- shadow copies on the relevant volume
    are identified later by whether they contain the expected path.
    """
    try:
        result = subprocess.run(
            ["vssadmin", "list", "shadowstorage"],
            capture_output=True, text=True, timeout=30,
        )
        output = (result.stdout + result.stderr).lower()
        # vssadmin exits 0 even when nothing is found; look for the no-items phrase
        if "no items found" in output and result.returncode != 0:
            return False
        # If vssadmin ran at all and didn't error out, VSS is available
        return result.returncode == 0 or "shadow copy storage" in output
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


# ---------------------------------------------------------------------------
# Shadow copy enumeration
# ---------------------------------------------------------------------------

def _enumerate_shadow_copies():
    """
    Return a list of dicts [{device, install_date}] for all available shadow copies.

    device       -- e.g. '\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy14'
    install_date -- ISO-8601 string, e.g. '2026-05-20T06:00:00'

    Tries PowerShell (Get-CimInstance) first, falls back to wmic.
    Returns [] when no shadow copies exist or both methods fail.
    """
    copies = []

    # Primary: PowerShell Get-CimInstance with explicit s-format date string.
    # Single quotes only -- avoids subprocess quoting collisions with double quotes.
    ps_cmd = (
        "Get-CimInstance Win32_ShadowCopy "
        "| ForEach-Object { $_.DeviceObject + '|' + $_.InstallDate.ToString('s') }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=30,
        )
        for line in result.stdout.splitlines():
            line = line.strip()
            if "|" not in line:
                continue
            device, install_date = line.split("|", 1)
            device = device.strip()
            if device.startswith("\\\\?\\") or device.startswith("\\\\.\\"):
                copies.append({"device": device, "install_date": install_date.strip()})
        if copies:
            return copies
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    # Fallback: wmic shadowcopy (available on all Windows editions).
    try:
        result = subprocess.run(
            ["wmic", "shadowcopy", "get", "DeviceObject,InstallDate", "/FORMAT:CSV"],
            capture_output=True, text=True, timeout=30,
        )
        for line in result.stdout.splitlines():
            parts = [p.strip() for p in line.split(",")]
            # CSV format: Node,DeviceObject,InstallDate
            if len(parts) < 3:
                continue
            device = parts[1]
            install_date_wmi = parts[2]
            if not device.startswith("\\\\?\\"):
                continue
            m = _WMI_DT_RE.match(install_date_wmi)
            install_date = (
                "{}-{}-{}T{}:{}:{}".format(*m.groups()) if m else install_date_wmi
            )
            copies.append({"device": device, "install_date": install_date})
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    return copies


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _shadow_projects_path(device_object, projects_dir):
    """
    Return the shadow copy equivalent of projects_dir.

    e.g. device='\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy14',
         projects_dir='C:\\Users\\Robbie\\.claude\\projects'
         -> '\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy14\\Users\\Robbie\\.claude\\projects'
    """
    _, path_from_drive = os.path.splitdrive(projects_dir)
    return device_object + path_from_drive


def _rel_path(full_path, base_path):
    """Compute the relative path robustly for extended (\\?\\...) paths."""
    # os.path.relpath works on extended paths since both share the same prefix.
    try:
        rel = os.path.relpath(full_path, base_path)
        # Sanity check: relpath should not climb above base_path.
        if rel.startswith(".."):
            raise ValueError("relpath escaped base: {}".format(rel))
        return rel
    except ValueError:
        # Fall back to manual strip for UNC/extended paths.
        if full_path.startswith(base_path):
            return full_path[len(base_path):].lstrip("\\/")
        raise


# ---------------------------------------------------------------------------
# JSONL scanning
# ---------------------------------------------------------------------------

def _scan_shadow_for_jsonls(shadow_projects_dir, device_object, install_date):
    """
    Walk shadow_projects_dir and return a list of dicts for each .jsonl file.

    Each dict: {rel_path, shadow_path, shadow_size, install_date, device}
    rel_path is relative to shadow_projects_dir.
    Returns [] if the path does not exist or is inaccessible.
    """
    results = []
    try:
        for root, _dirs, files in os.walk(shadow_projects_dir):
            for fname in files:
                if not fname.endswith(".jsonl"):
                    continue
                full = os.path.join(root, fname)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                try:
                    rel = _rel_path(full, shadow_projects_dir)
                except (ValueError, TypeError):
                    continue
                results.append({
                    "rel_path": rel,
                    "shadow_path": full,
                    "shadow_size": st.st_size,
                    "install_date": install_date,
                    "device": device_object,
                })
    except (OSError, PermissionError):
        pass
    return results


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------

def _build_candidates(shadow_copies, projects_dir):
    """
    Scan all shadow copies and return the best restore candidate per file.

    Selection rule: largest shadow copy version of each file, where
    shadow_size > live_size (or the live file is absent).

    Returns a list of candidate dicts sorted by rel_path, each containing:
      rel_path, live_path, live_size, shadow_path, shadow_size, install_date, device
    """
    best = {}  # rel_path -> best candidate so far

    for sc in shadow_copies:
        shadow_projects = _shadow_projects_path(sc["device"], projects_dir)
        found = _scan_shadow_for_jsonls(shadow_projects, sc["device"], sc["install_date"])
        for item in found:
            rel = item["rel_path"]
            if rel not in best or item["shadow_size"] > best[rel]["shadow_size"]:
                best[rel] = item

    candidates = []
    for rel, item in sorted(best.items()):
        live_path = os.path.join(projects_dir, rel)
        live_size = os.path.getsize(live_path) if os.path.isfile(live_path) else 0
        if item["shadow_size"] <= live_size:
            continue  # live copy is already at least as complete
        candidate = dict(item)
        candidate["live_path"] = live_path
        candidate["live_size"] = live_size
        candidates.append(candidate)

    return candidates


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _fmt_size(n):
    if n == 0:
        return "(missing)"
    if n < 1024:
        return "{:,} B".format(n)
    if n < 1024 * 1024:
        return "{:.1f} KB".format(n / 1024)
    return "{:.1f} MB".format(n / (1024 * 1024))


def _print_candidates_table(candidates):
    """Print a summary table of restore candidates."""
    if not candidates:
        print("No candidates -- all live files are at least as large as their shadow copies.")
        return

    print("Candidates: {}".format(len(candidates)))
    print()

    # Column widths
    col_date = max(len("Shadow Date"), max(len(c["install_date"]) for c in candidates))
    col_path = min(
        max(len("File"), max(len(c["rel_path"]) for c in candidates)),
        60,  # cap for display
    )
    col_sz = 12

    header = "  {:<{d}}  {:<{p}}  {:>{s}}  {:>{s}}".format(
        "Shadow Date", "File", "Shadow", "Live",
        d=col_date, p=col_path, s=col_sz,
    )
    print(header)
    print("  " + "-" * (len(header) - 2))

    for c in candidates:
        path_display = c["rel_path"]
        if len(path_display) > col_path:
            path_display = "..." + path_display[-(col_path - 3):]
        print("  {:<{d}}  {:<{p}}  {:>{s}}  {:>{s}}".format(
            c["install_date"], path_display,
            _fmt_size(c["shadow_size"]), _fmt_size(c["live_size"]),
            d=col_date, p=col_path, s=col_sz,
        ))


def _print_reminders(dry_run):
    """Print the mtime warning and the synth_session_metadata next-step reminder."""
    print()
    print("=" * 72)
    print("IMPORTANT -- mtime / cleanupPeriodDays interaction")
    print()
    print("  This tool preserves original filesystem mtimes when copying.")
    print("  Claude Code's cleanup deletes JSONLs based on mtime, not message")
    print("  timestamp. If cleanupPeriodDays is at its default (30 days), any")
    print("  JSONL with an mtime older than 30 days will be re-deleted on the")
    print("  next Claude Desktop launch.")
    print()
    if dry_run:
        print("  Before running with --apply:")
    else:
        print("  If you have not already done so:")
    print("    1. Open ~/.claude/settings.json")
    print('    2. Set "cleanupPeriodDays": 36500  (approximately 100 years)')
    print("    3. Run this script with --apply, then run synth_session_metadata.py")
    print("    4. Revert cleanupPeriodDays to your preferred value once sessions are recovered")
    print()
    print("  This applies to any restore method that preserves original file")
    print("  timestamps (this script, backup_claude_state.py zip extracts, or")
    print("  other snapshot tools).")
    print()
    print("Next step after restoring:")
    print("  python tools/diagnose.py")
    print("  python tools/sessions/synth_session_metadata.py --diagnosis-id <id>")
    print("  (diagnose.py prints the current diagnosis ID)")
    print("=" * 72)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(
        description=(
            "Restore deleted Claude Code JSONL transcripts from Windows VSS shadow copies. "
            "Dry-run by default -- add --apply to write files."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Print candidates without restoring (default behaviour).",
    )
    mode.add_argument(
        "--apply", action="store_true",
        help="Copy selected files from shadow copies to the live projects directory.",
    )
    ap.add_argument(
        "--projects-dir", metavar="PATH", default=None, dest="projects_dir",
        help="Override the default ~/.claude/projects/ path (for testing).",
    )
    args = ap.parse_args()

    DRY_RUN = not args.apply  # noqa: N806

    # OS guard -- must run inside main() so import-time scanning doesn't crash.
    if sys.platform != "win32":
        print("ERROR: restore_from_vss.py is Windows-only.", file=sys.stderr)
        sys.exit(1)

    if not _is_admin():
        print("ERROR: This script requires administrator privileges.", file=sys.stderr)
        print("  VSS shadow copy access is restricted to elevated processes.",
              file=sys.stderr)
        print("  Re-run from an elevated PowerShell prompt:", file=sys.stderr)
        print("    Right-click PowerShell -> 'Run as administrator'", file=sys.stderr)
        print("    py -3 tools/sessions/restore_from_vss.py", file=sys.stderr)
        sys.exit(1)

    acquire_lock(LOCK_FILE, "restore_from_vss")
    atexit.register(release_lock, LOCK_FILE)

    projects_dir = (
        os.path.abspath(args.projects_dir) if args.projects_dir else PROJECTS_DIR
    )

    mode_label = "DRY-RUN -- no files will be written" if DRY_RUN else "APPLY"
    print("=== restore_from_vss  ({}) ===".format(mode_label))
    print("  Projects dir : {}".format(projects_dir))
    print()

    # [1/3] VSS availability check.
    print("--- [1/3] Checking VSS availability ---")
    if not _check_vss_available():
        print("  VSS shadow storage not found or vssadmin not accessible.")
        print("  Check that the Volume Shadow Copy service is running and shadow")
        print("  copies are configured: vssadmin list shadowstorage")
        sys.exit(0)
    print("  VSS available.")
    print()

    # [2/3] Enumerate shadow copies.
    print("--- [2/3] Enumerating shadow copies ---")
    shadow_copies = _enumerate_shadow_copies()
    if not shadow_copies:
        print("  No shadow copies found.")
        print("  Shadow copies must be created before files can be restored.")
        print("  Check System Restore or Task Scheduler shadow copy jobs.")
        sys.exit(0)
    print("  Found {} shadow copy/copies:".format(len(shadow_copies)))
    for sc in shadow_copies:
        print("    {}  {}".format(sc["install_date"], sc["device"]))
    print()

    # [3/3] Scan and select candidates.
    print("--- [3/3] Scanning for restorable JSONLs ---")
    candidates = _build_candidates(shadow_copies, projects_dir)
    print()
    _print_candidates_table(candidates)

    if not candidates:
        _print_reminders(dry_run=DRY_RUN)
        sys.exit(0)

    if DRY_RUN:
        print()
        print("Dry-run complete. Re-run with --apply to restore these files.")
        _print_reminders(dry_run=True)
        sys.exit(0)

    # Apply: copy files, preserving mtime via shutil.copy2.
    print()
    print("Restoring {} file(s) ...".format(len(candidates)))
    restored = 0
    skipped = 0
    failed = 0

    for c in candidates:
        live_path = c["live_path"]
        shadow_path = c["shadow_path"]

        # Re-check live size -- state may have changed since the scan.
        current_live_size = (
            os.path.getsize(live_path) if os.path.isfile(live_path) else 0
        )
        if c["shadow_size"] <= current_live_size:
            print("  SKIP  {} (live copy grew since scan)".format(c["rel_path"]))
            skipped += 1
            continue

        try:
            os.makedirs(os.path.dirname(live_path), exist_ok=True)
            shutil.copy2(shadow_path, live_path)
            print("  OK    {}  ({} from {})".format(
                c["rel_path"], _fmt_size(c["shadow_size"]), c["install_date"],
            ))
            restored += 1
        except OSError as exc:
            print("  FAIL  {} : {}".format(c["rel_path"], exc), file=sys.stderr)
            failed += 1

    print()
    print("Restored: {}  Skipped: {}  Failed: {}".format(restored, skipped, failed))
    _print_reminders(dry_run=False)

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
