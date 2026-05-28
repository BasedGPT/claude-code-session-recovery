"""
Invoked via diagnose.py. Not intended for direct invocation.
To diagnose your state: python tools/diagnose.py

Restore JSONL transcripts from Windows Volume Shadow Copy Service (VSS).

Searches VSS shadow copies for JSONL transcripts that are missing from
~\\.claude\\projects\\ but still referenced by Desktop metadata files
(cliSessionId set, transcript deleted). For each missing JSONL found,
picks the version with the most valid records (append-only, so more records
= more complete) and optionally copies it back to the expected location.

VSS shadow copies are the "Previous Versions" you see in Windows Explorer.
This tool does not require elevation — reading from VSS paths is unprivileged.

Files read:
  - %APPDATA%\\Claude\\claude-code-sessions\\<account-uuid>\\<org-uuid>\\local_*.json
  - %USERPROFILE%\\.claude\\projects\\<slug>\\  (to verify JSONL is absent)
  - \\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopyN\\...  (VSS shadow paths)

Files written (--apply only):
  - %USERPROFILE%\\.claude\\projects\\<slug>\\<cli-session-id>.jsonl
  - ./repair-backup/restore_from_vss_<date>.log  (restore log)

Usage:
    python tools/sessions/restore_from_vss.py --diagnosis-id <hex>
    python tools/sessions/restore_from_vss.py --diagnosis-id <hex> --apply
    python tools/sessions/restore_from_vss.py --diagnosis-id <hex> --state <fixture-path>

Windows only. VSS is a Windows feature.
"""
import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _TOOLS_DIR)
try:
    from diagnose import (
        build_snapshot, make_diagnosis_id, _find_meta_dirs,
        _slug_encode, _build_jsonl_index,
    )
except ImportError as exc:
    print("ERROR: cannot import from diagnose.py: {}".format(exc))
    print("Run from the repo root: python tools/sessions/restore_from_vss.py")
    sys.exit(1)

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(TOOL_DIR, "repair-backup")


def _default_paths():
    return (
        os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Claude"),
        os.path.join(os.path.expanduser("~"), ".claude", "projects"),
    )


# ---------------------------------------------------------------------------
# Known-do-not-run conditions (schema probe + empty-target guard)
# ---------------------------------------------------------------------------

KNOWN_DO_NOT_RUN = [
    (
        lambda s: s["metadata_dangling_cli_count"] == 0,
        "No sessions with missing JSONL transcripts found. Nothing to restore.",
    ),
    (
        lambda s: s["schema_version"] == "unrecognised",
        "State schema not recognised. Run diagnose.py and report the "
        "unsupported state to the maintainer.",
    ),
]


# ---------------------------------------------------------------------------
# VSS enumeration
# ---------------------------------------------------------------------------

def _shadow_projects_path(device_object, projects_dir):
    """Construct the VSS shadow path for projects_dir under device_object.

    device_object is e.g. '\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy5'
    projects_dir  is e.g. 'C:\\Users\\Robbie\\.claude\\projects'
    result        is e.g. '\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy5\\Users\\Robbie\\.claude\\projects'
    """
    _drive, rel = os.path.splitdrive(os.path.abspath(projects_dir))
    rel = rel.lstrip("\\/")
    return device_object.rstrip("\\/") + "\\" + rel


def _enumerate_vss_roots_powershell(projects_dir):
    """Return list of shadow_projects_path strings via CIM Win32_ShadowCopy.

    Returns None on failure (caller falls back to vssadmin).
    Uses structured CIM output to avoid locale-sensitive text parsing.
    Filters shadows to only those on the same volume as projects_dir.
    """
    ps_script = (
        "$s = Get-CimInstance Win32_ShadowCopy | "
        "Select-Object DeviceObject,VolumeName,InstallDate; "
        "$v = Get-CimInstance Win32_Volume | "
        "Select-Object DeviceID,DriveLetter; "
        "[ordered]@{ShadowCopies=$s;Volumes=$v} | ConvertTo-Json -Depth 3"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None
        data = json.loads(r.stdout)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return None

    volumes_raw = data.get("Volumes") or []
    if isinstance(volumes_raw, dict):
        volumes_raw = [volumes_raw]
    # Map DeviceID (volume GUID path) -> drive letter, e.g. "C:"
    drive_map = {}
    for v in volumes_raw:
        did = (v.get("DeviceID") or "").upper().rstrip("\\")
        dl = (v.get("DriveLetter") or "").upper()
        if did and dl:
            drive_map[did] = dl

    target_drive = os.path.splitdrive(os.path.abspath(projects_dir))[0].upper()

    shadows_raw = data.get("ShadowCopies") or []
    if isinstance(shadows_raw, dict):
        shadows_raw = [shadows_raw]

    roots = []
    for s in shadows_raw:
        device = (s.get("DeviceObject") or "").strip()
        vol_name = (s.get("VolumeName") or "").upper().rstrip("\\")
        if not device:
            continue
        mapped_drive = drive_map.get(vol_name, "")
        # If we can determine the drive and it doesn't match, skip.
        # If drive_map is empty or vol_name unrecognised, include the shadow
        # (fail-safe: better to search too much than miss the right shadow).
        if mapped_drive and mapped_drive != target_drive:
            continue
        roots.append(_shadow_projects_path(device, projects_dir))
    return roots


def _enumerate_vss_roots_vssadmin(projects_dir):
    """Fallback: parse 'vssadmin list shadows' text output.

    Locale-sensitive but widely available. Used when PowerShell CIM fails.
    """
    try:
        r = subprocess.run(
            ["vssadmin", "list", "shadows"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30,
        )
        # Exit 1 means no shadow copies — not an error
        if r.returncode not in (0, 1):
            return []
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        return []

    target_drive = os.path.splitdrive(os.path.abspath(projects_dir))[0].upper()
    roots = []
    current_orig_drive = ""
    for line in r.stdout.splitlines():
        stripped = line.strip().lower()
        # "Original Volume:" or "Original Volume Name:" lines identify the volume
        if "original volume" in stripped and ":" in stripped:
            paren_start = line.find("(")
            paren_end = line.find(")")
            if paren_start >= 0 and paren_end > paren_start:
                candidate = line[paren_start + 1:paren_end].upper().rstrip("\\")
                if len(candidate) == 2 and candidate[1] == ":":
                    current_orig_drive = candidate
        # "Shadow Copy Volume:" lines give the device object path
        if "shadow copy volume" in stripped and ":" in stripped:
            colon_pos = line.find(":")
            if colon_pos >= 0:
                device = line[colon_pos + 1:].strip()
                if device and "\\" in device:
                    if not current_orig_drive or current_orig_drive == target_drive:
                        roots.append(_shadow_projects_path(device, projects_dir))
    return roots


def _enumerate_vss_roots(projects_dir, state=None):
    """Return list of shadow_projects_path strings to search.

    In fixture mode (state != None): scans state/vss/shadow-*/ directories.
    In live mode: tries PowerShell CIM, falls back to vssadmin.
    """
    if state is not None:
        vss_root = os.path.join(state, "vss")
        if not os.path.isdir(vss_root):
            return []
        roots = []
        for name in sorted(os.listdir(vss_root)):
            shadow_dir = os.path.join(vss_root, name)
            if not os.path.isdir(shadow_dir):
                continue
            roots.append(os.path.join(shadow_dir, "projects"))
        return roots

    roots = _enumerate_vss_roots_powershell(projects_dir)
    if roots is None:
        roots = _enumerate_vss_roots_vssadmin(projects_dir)
    return roots or []


# ---------------------------------------------------------------------------
# Candidate validation and selection
# ---------------------------------------------------------------------------

def _count_valid_records(path, expected_sid):
    """Parse JSONL, count records whose sessionId == expected_sid.

    Returns (count, last_timestamp_str). Rejects files where sessionId never
    matches — guards against wrong-session files with the right UUID filename.
    """
    count = 0
    last_ts = ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, TypeError):
                    continue
                sid = rec.get("sessionId") or ""
                if isinstance(sid, str) and sid == expected_sid:
                    count += 1
                    ts = rec.get("timestamp") or ""
                    if isinstance(ts, str) and ts > last_ts:
                        last_ts = ts
    except OSError:
        pass
    return count, last_ts


def _find_best_candidate(cli_sid, vss_roots):
    """Search all VSS shadow roots for cli_sid.jsonl.

    Searches every slug directory in each shadow root (not just the one
    derived from current metadata cwd) to handle renamed projects.

    Returns (best_path, shadow_root, valid_count) or (None, None, 0).
    Ranks by: valid record count (desc), last timestamp (desc), byte size (desc).
    """
    candidates = []
    fname = "{}.jsonl".format(cli_sid)
    for shadow_projects in vss_roots:
        if not os.path.isdir(shadow_projects):
            continue
        try:
            for slug in sorted(os.listdir(shadow_projects)):
                slug_dir = os.path.join(shadow_projects, slug)
                if not os.path.isdir(slug_dir):
                    continue
                candidate = os.path.join(slug_dir, fname)
                if not os.path.isfile(candidate):
                    continue
                valid_count, last_ts = _count_valid_records(candidate, cli_sid)
                if valid_count > 0:
                    size = os.path.getsize(candidate)
                    candidates.append((valid_count, last_ts, size, candidate, shadow_projects))
        except OSError:
            pass

    if not candidates:
        return None, None, 0

    candidates.sort(key=lambda c: (c[0], c[1], c[2]), reverse=True)
    best = candidates[0]
    return best[3], best[4], best[0]


# ---------------------------------------------------------------------------
# Session discovery
# ---------------------------------------------------------------------------

def _find_dangling(appdata_claude_dir, projects_dir):
    """Yield (meta_file, cli_sid, title, cwd) for sessions with no live JSONL."""
    jsonl_index = _build_jsonl_index(projects_dir)
    for _acct, _org, meta_dir in _find_meta_dirs(appdata_claude_dir):
        for fname in sorted(os.listdir(meta_dir)):
            if not (fname.startswith("local_") and fname.endswith(".json")):
                continue
            fpath = os.path.join(meta_dir, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as fh:
                    d = json.load(fh)
            except Exception:
                continue
            cli = (d.get("cliSessionId") or "").strip()
            if not cli or cli in jsonl_index:
                continue
            yield (fname, cli, (d.get("title") or "")[:60], d.get("cwd") or "")


# ---------------------------------------------------------------------------
# Atomic restore
# ---------------------------------------------------------------------------

def _atomic_copy(src, dest):
    """Copy src to dest atomically via a temp file.

    Returns (True, "") on success or (False, error_message) on failure.
    Creates parent directories as needed. Never overwrites an existing dest.
    """
    dest_dir = os.path.dirname(dest)
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as e:
        return False, "makedirs failed: {}".format(e)
    tmp = dest + ".tmp"
    try:
        shutil.copy2(src, tmp)
    except OSError as e:
        return False, "copy failed: {}".format(e)
    try:
        if os.path.exists(dest):
            os.unlink(tmp)
            return False, "destination appeared unexpectedly; skipped"
        os.rename(tmp, dest)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False, "rename failed: {}".format(e)
    return True, ""


# ---------------------------------------------------------------------------
# Process check
# ---------------------------------------------------------------------------

def _desktop_running():
    """Return True if claude.exe is in the Windows process list."""
    try:
        r = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq claude.exe", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        return "claude.exe" in r.stdout.lower()
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _display_path(path, base):
    """Return path relative to base for deterministic fixture output, else absolute."""
    if base is None:
        return path
    try:
        return os.path.relpath(path, base)
    except ValueError:
        return path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if platform.system() != "Windows":
        # Fixture tests on CI use windows-latest; this guard is for direct misuse.
        print("ERROR: restore_from_vss.py is Windows-only.")
        print("VSS (Volume Shadow Copy Service) is a Windows feature.")
        sys.exit(1)

    ap = argparse.ArgumentParser(
        description=(
            "Restore JSONL transcripts from Windows VSS shadow copies. "
            "Dry-run by default — add --apply to restore."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--diagnosis-id", metavar="HEX", default=None, dest="diagnosis_id",
        help="Diagnosis token from diagnose.py (required).",
    )
    ap.add_argument(
        "--force-with-diagnosis-id", metavar="VALUE", default=None,
        dest="force_diagnosis_id",
        help="Set to 'audit-only' to run dry-run without a current token.",
    )
    ap.add_argument(
        "--apply", action="store_true",
        help="Restore files in-place. Default is dry-run.",
    )
    ap.add_argument(
        "--state", metavar="PATH", default=None,
        help="Fixture state directory for testing "
             "(must contain appdata/Claude/... and projects/ subdirectories).",
    )
    ap.add_argument(
        "--quiet", action="store_true",
        help="Print summary counts only.",
    )
    args = ap.parse_args()

    force_mode = args.force_diagnosis_id == "audit-only"
    if not args.diagnosis_id and not force_mode:
        print("ERROR: --diagnosis-id required.")
        print("Run: python tools/diagnose.py")
        sys.exit(2)
    if args.apply and force_mode:
        print("ERROR: --apply cannot be combined with --force-with-diagnosis-id=audit-only.")
        sys.exit(2)

    fixture_mode = args.state is not None
    if args.state:
        state_abs = os.path.abspath(args.state)
        appdata_claude_dir = os.path.join(state_abs, "appdata", "Claude")
        projects_dir = os.path.join(state_abs, "projects")
    else:
        state_abs = None
        appdata_claude_dir, projects_dir = _default_paths()

    snapshot = build_snapshot(
        appdata_claude_dir, projects_dir,
        fixture_mode=fixture_mode,
    )
    current_id = make_diagnosis_id(snapshot)

    if not force_mode and current_id != args.diagnosis_id:
        print(
            "ERROR: Diagnosis token mismatch.\n"
            "  Supplied : {}\n"
            "  Current  : {}".format(args.diagnosis_id, current_id)
        )
        print("State has changed since diagnose.py was last run. "
              "Re-run: python tools/diagnose.py")
        sys.exit(2)

    for predicate, message in KNOWN_DO_NOT_RUN:
        try:
            if predicate(snapshot):
                print("REFUSED: " + message)
                sys.exit(3)
        except Exception:
            pass

    dangling = list(_find_dangling(appdata_claude_dir, projects_dir))
    vss_roots = _enumerate_vss_roots(projects_dir, state=state_abs)

    used_diagnosis_id = args.diagnosis_id if not force_mode else "(forced-audit-only)"
    print("Sessions with missing JSONL: {}".format(len(dangling)))
    print("VSS shadow copies found:     {}".format(len(vss_roots)))
    print("Diagnosis ID: {}".format(used_diagnosis_id))
    print()

    # Find best VSS candidate for each dangling session
    recoverable = []
    not_found = []
    for meta_file, cli_sid, title, cwd in dangling:
        best_path, _shadow_root, valid_count = _find_best_candidate(cli_sid, vss_roots)
        if best_path and cwd:
            slug = _slug_encode(cwd)
            dest = os.path.join(projects_dir, slug, "{}.jsonl".format(cli_sid))
            recoverable.append((meta_file, cli_sid, title, best_path, valid_count, dest))
        else:
            not_found.append((meta_file, cli_sid, title))

    print("=== Recoverable from VSS ({}) ===".format(len(recoverable)))
    for meta_file, cli_sid, title, best_path, valid_count, dest in recoverable:
        size = os.path.getsize(best_path)
        print("  {} {} {}".format(meta_file, cli_sid[:8], title))
        if not args.quiet:
            print("      source:  {}".format(_display_path(best_path, state_abs)))
            print("      dest:    {}".format(_display_path(dest, state_abs)))
            print("      records: {}  size: {} B".format(valid_count, size))

    print()
    print("=== Not found in VSS ({}) ===".format(len(not_found)))
    for meta_file, cli_sid, title in not_found:
        print("  {} {} {}".format(meta_file, cli_sid[:8], title))
    if not_found:
        print()
        print("  See docs/recovering-deleted-jsonls.md for other recovery options,")
        print("  including user-configured backup search:")
        print("    python tools/sessions/find_missing_jsonls_in_backup.py [--backup PATH]")

    print()
    print("Summary: {} recoverable from VSS, {} not found.".format(
        len(recoverable), len(not_found)
    ))

    if not recoverable:
        return 0

    if not args.apply:
        print()
        print("Dry-run -- no files written. Add --apply to restore.")
        return 0

    # --- Apply mode ---
    if not fixture_mode and _desktop_running():
        print()
        print("ERROR: Claude Desktop is running.")
        print("Quit Desktop fully before restoring (new files can conflict with")
        print("Desktop's in-progress session state):")
        print("  Right-click the tray icon -> Quit")
        print('  tasklist /FI "IMAGENAME eq claude.exe"  -- must show no results')
        sys.exit(4)

    os.makedirs(BACKUP_DIR, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_path = os.path.join(BACKUP_DIR, "restore_from_vss_{}.log".format(today))

    restored = 0
    skipped = 0
    failed = 0

    print()
    print("=== Restoring ===")
    for meta_file, cli_sid, title, best_path, valid_count, dest in recoverable:
        if os.path.exists(dest):
            print("  SKIP {} -- destination already present: {}".format(
                cli_sid[:8], _display_path(dest, state_abs)))
            skipped += 1
            continue
        ok, err = _atomic_copy(best_path, dest)
        if ok:
            size = os.path.getsize(dest)
            print("  RESTORED {} -> {} ({} B)".format(
                cli_sid[:8], _display_path(dest, state_abs), size))
            restored += 1
            _write_log(log_path, "RESTORED", cli_sid,
                       _display_path(best_path, state_abs),
                       _display_path(dest, state_abs),
                       valid_count, size)
        else:
            print("  FAILED   {} -- {}".format(cli_sid[:8], err))
            failed += 1
            _write_log(log_path, "FAILED", cli_sid, "", "", 0, 0, err)

    print()
    print("Restore complete: {} restored, {} skipped, {} failed.".format(
        restored, skipped, failed))
    if restored > 0 and not fixture_mode:
        print("Log written to: {}".format(log_path))

    return 0 if failed == 0 else 1


def _write_log(log_path, status, cli_sid, src, dest, records, size, err=""):
    try:
        ts = datetime.now(timezone.utc).isoformat()
        with open(log_path, "a", encoding="utf-8") as lf:
            if status == "RESTORED":
                lf.write("{} RESTORED {}\n  from:    {}\n  to:      {}\n"
                         "  records: {}  size: {} B\n\n".format(
                             ts, cli_sid, src, dest, records, size))
            else:
                lf.write("{} FAILED {} -- {}\n\n".format(ts, cli_sid, err))
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
