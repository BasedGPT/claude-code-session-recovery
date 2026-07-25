"""
Claude Code Desktop Session Recovery Tools -- Diagnostic
=========================================================

Files read:
  - %APPDATA%\\Claude\\claude-code-sessions\\<account-uuid>\\<org-uuid>\\local_*.json
  - %USERPROFILE%\\.claude\\projects\\<slug>\\*.jsonl
  - %LOCALAPPDATA%\\AnthropicClaude\\  (version detection only)

Files written:
  - Nothing. This tool is read-only.

Invoked directly. Mutators are invoked via the commands this tool prints.

Usage:
    python tools/diagnose.py                   # probe live state
    python tools/diagnose.py --json            # machine-readable output
    python tools/diagnose.py --state <path>    # probe a fixture state dir
                                               # (<path>/appdata/Claude/... and
                                               #  <path>/projects/...)
"""
import argparse
import json
import os
import platform
import re
import sys

from session_state import (
    build_snapshot,
    make_diagnosis_id,
    scan_vscode_dropped_sessions,
    slug_encode,
)


# ---------------------------------------------------------------------------
# Compatibility
# ---------------------------------------------------------------------------

# Focused slug tests and downstream callers historically import this name
# from diagnose.py; retain this narrow compatibility alias while state lives in
# session_state.py.
_slug_encode = slug_encode


def _redact_user_home(path):
    """Replace the current user's home prefix before displaying a path."""
    if not isinstance(path, str) or not path:
        return path
    home = os.path.abspath(os.path.expanduser("~"))
    path_norm = os.path.normcase(os.path.normpath(path))
    home_norm = os.path.normcase(os.path.normpath(home))
    if path_norm == home_norm:
        return "%USERPROFILE%" if platform.system() == "Windows" else "~"
    home_prefix = home_norm.rstrip("\\/") + os.sep
    if path_norm.startswith(home_prefix):
        placeholder = "%USERPROFILE%" if platform.system() == "Windows" else "~"
        return placeholder + path[len(home):]
    return path


# ---------------------------------------------------------------------------
# Predicate evaluator
# ---------------------------------------------------------------------------

def _eval_comparison(actual, op, expected):
    """Evaluate a single comparison operation."""
    if op == "==":
        return actual == expected
    if op == "!=":
        return actual != expected
    if op == ">=":
        return actual is not None and actual >= expected
    if op == "<=":
        return actual is not None and actual <= expected
    if op == ">":
        return actual is not None and actual > expected
    if op == "<":
        return actual is not None and actual < expected
    if op == "in":
        return actual in expected
    if op == "regex":
        return bool(re.search(expected, str(actual or "")))
    return False


def eval_match(predicate, snapshot):
    """Evaluate a match predicate dict against a snapshot. Returns bool."""
    if not predicate:
        return False
    if "any" in predicate:
        return any(eval_match(p, snapshot) for p in predicate["any"])
    if "all" in predicate:
        return all(eval_match(p, snapshot) for p in predicate["all"])
    # Leaf node: {"snapshot.field[.subfield...]": {"op": value}}
    # Supports dot-notation for nested dicts, e.g. "snapshot.cwd_prefix_types.bare_root".
    for key, comparison in predicate.items():
        field = key[len("snapshot."):] if key.startswith("snapshot.") else key
        parts = field.split(".")
        actual = snapshot
        for part in parts:
            if isinstance(actual, dict):
                actual = actual.get(part)
            else:
                actual = None
                break
        for op, expected in comparison.items():
            if not _eval_comparison(actual, op, expected):
                return False
    return True


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

_SEP = "-" * 60


def _format_human(diagnosis_id, snapshot, matches, schema_ok, repo_root=None,
                  vscode_dropped=0, vscode_db_count=0):
    lines = [
        "DIAGNOSE -- Claude Code Desktop Session Recovery Tools",
        _SEP,
        f"Diagnosis ID : {diagnosis_id}",
    ]
    if snapshot.get("desktop_version"):
        lines.append(f"Desktop ver. : {snapshot['desktop_version']}")
    if snapshot.get("cli_version"):
        lines.append(f"CLI ver.     : {snapshot['cli_version']}")
    lines.append(
        f"Metadata     : {snapshot['total_metadata_count']} files"
        f"  ({snapshot['metadata_with_cli_count']} with cliSessionId,"
        f"  {snapshot['metadata_missing_cli_count']} missing)"
    )
    lines.append(f"JSONL files  : {snapshot['jsonl_count']}")
    if snapshot.get("desktop_session_pairs"):
        pairs = snapshot["desktop_session_pairs"]
        lines.append(f"Desktop pairs : {len(pairs)}")
        for pair in pairs:
            lines.append(
                "  account={} organisation={} local_*.json={}".format(
                    pair["account_uuid"],
                    pair["organisation_uuid"],
                    pair["local_metadata_count"],
                )
            )
        lines.append("")
    if snapshot.get("truncated_jsonl_count", 0) > 0:
        lines.append(
            f"Truncated    : {snapshot['truncated_jsonl_count']} session(s) have fewer"
            " messages than completedTurns records (history appears cut off)"
        )
    if snapshot.get("metadata_null_timestamp_count", 0) > 0:
        lines.append(
            f"Null stamps  : {snapshot['metadata_null_timestamp_count']} metadata file(s)"
            " have null createdAt/updatedAt (can blank the entire session list)"
        )
    lines.append("")

    if snapshot.get("desktop_running"):
        if snapshot.get("running_inside_desktop"):
            lines.append("WARNING: You are running inside Claude Desktop itself.")
            lines.append("  You cannot quit Desktop to run mutators from this session.")
            lines.append("")
            lines.append("  Do this in order:")
            lines.append("    1. Open a new terminal (cmd, PowerShell, or Windows Terminal)")
            if repo_root:
                lines.append('    2. cd "{}"'.format(repo_root))
            lines.append("    3. Quit Claude Desktop: right-click the tray icon -> Quit")
            lines.append('    4. tasklist /FI "IMAGENAME eq claude.exe"  -- must show no results')
            lines.append("    5. Run the repair commands printed below")
        else:
            lines.append("WARNING: Claude Desktop appears to be running (claude.exe in tasklist).")
            lines.append("  Diagnose is safe -- it is read-only.")
            lines.append("  Repair commands below will NOT work until Desktop is fully quit.")
            lines.append("  Quit: right-click the tray icon -> Quit. Then verify:")
            lines.append('    tasklist /FI "IMAGENAME eq claude.exe"')
        lines.append("")

    if snapshot.get("install_type") == "msix":
        lines.append("NOTE: Microsoft Store (MSIX) install detected.")
        msix_path = _redact_user_home(snapshot.get("msix_real_path"))
        if msix_path:
            lines.append(f"  Data path: {msix_path}")
        lines.append("  diagnose.py is read-only and works correctly on MSIX.")
        lines.append("  The write-bearing repair scripts (repair_session_metadata.py,")
        lines.append("  synth_session_metadata.py) are unlikely to surface sessions on")
        lines.append("  an MSIX install: Desktop maintains an internal index that takes")
        lines.append("  precedence over files written externally, regardless of write")
        lines.append("  access to the real package path. Community-confirmed 2026-06-03.")
        lines.append("  See README.md for details.")
        lines.append("")

    if snapshot.get("mapped_drive_unc_mismatch_count", 0) > 0:
        drives = snapshot.get("mapped_drive_affected_drives", [])
        drive_label = "drives" if len(drives) > 1 else "drive"
        drives_str = ", ".join(f"{d}:\\" for d in drives)
        lines.append(f"NOTE: Mapped network {drive_label} detected ({drives_str}).")
        lines.append("  Project slugs under these drive letters won't appear in the VS Code")
        lines.append("  extension's session history sidebar. The extension resolves drive letters")
        lines.append("  to UNC paths (\\\\server\\share\\...) and derives a different slug — one that")
        lines.append("  doesn't exist on disk. The Desktop app and CLI are not affected.")
        lines.append("")
        lines.append("  To fix the VS Code extension sidebar, choose one option:")
        lines.append("    A: Open VS Code via the UNC path instead of the drive letter.")
        lines.append("    B: Rename ~/.claude/projects/<drive-slug>/ to the UNC-encoded slug.")
        lines.append("       See README.md for step-by-step instructions.")
        lines.append("")

    if vscode_dropped > 0:
        lines.append(
            f"NOTE: {vscode_dropped} transcript file(s) on disk are not in the VS Code"
            " extension's session cache."
        )
        lines.append(
            "  Sessions may be missing from Local -> Session History in VS Code even"
        )
        lines.append("  though the transcripts are intact and resumable via the CLI.")
        lines.append("  To recover them:")
        lines.append(
            "    python tools/sessions/recover_vscode_sessions.py"
        )
        lines.append(
            "    python tools/sessions/recover_vscode_sessions.py --apply  "
            "(VS Code must be closed)"
        )
        lines.append("")

    if not schema_ok:
        lines.append("State layout not in supported fixture set. Audit-only mode.")
        lines.append("No repair commands will be suggested for this state.")
        lines.append(
            "Please open an issue at https://github.com/BasedGPT/"
            "claude-code-session-recovery with your diagnose.py --json output."
        )
        return "\n".join(lines)

    if not matches:
        lines.append("State looks healthy. No known broken patterns matched.")
        return "\n".join(lines)

    quit_prefix = "QUIT DESKTOP FIRST: " if snapshot.get("desktop_running") else ""

    for row in matches:
        lines.append(f"PROBLEM FOUND: {row['problem']}")
        lines.append(f"  Details: {row['details']}")
        label = "Safety" if row.get("mutator") else "Status"
        lines.append(f"  {label} : {row['safety']}")
        lines.append("")
        if row.get("mutator"):
            mutator = row["mutator"]
            lines.append("  To repair -- dry-run first, review output, then add --apply:")
            lines.append(
                f"    {quit_prefix}python {mutator}"
                f" --diagnosis-id {diagnosis_id}"
            )
            lines.append(
                f"    {quit_prefix}python {mutator}"
                f" --diagnosis-id {diagnosis_id} --apply"
            )
        elif row.get("next_command"):
            lines.append(f"  Next:  {row['next_command']}")
        else:
            lines.append("  No automatic repair for this state. See:")
            lines.append(f"    {row['details']}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # REL-09 / PY-06: all I/O and side effects inside main()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(
        description="Diagnose Claude Code Desktop session state. Read-only.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Run this first. It tells you exactly what to do next.\n"
            "Mutators shown in the output require --diagnosis-id from this tool."
        ),
    )
    ap.add_argument(
        "--state",
        metavar="PATH",
        default=None,
        help=(
            "Path to a fixture state directory (for testing). "
            "Must contain appdata/Claude/... and projects/ subdirectories."
        ),
    )
    ap.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    args = ap.parse_args()

    # Resolve state directories
    if args.state:
        state_abs = os.path.abspath(args.state)
        appdata_claude_dir = os.path.join(state_abs, "appdata", "Claude")
        projects_dir = os.path.join(state_abs, "projects")
    else:
        _sys = platform.system()
        if _sys == "Darwin":
            appdata_claude_dir = os.path.expanduser("~/Library/Application Support/Claude")
            projects_dir = os.path.expanduser("~/.claude/projects")
        elif _sys == "Linux":
            appdata_claude_dir = os.path.expanduser("~/.config/Claude")
            projects_dir = os.path.expanduser("~/.claude/projects")
        else:
            appdata_claude_dir = os.path.join(
                os.environ.get("APPDATA", os.path.expanduser("~")), "Claude"
            )
            if not os.path.isdir(appdata_claude_dir):
                _local = os.environ.get("LOCALAPPDATA", "")
                _pkgs = os.path.join(_local, "Packages")
                if os.path.isdir(_pkgs):
                    for _pkg in sorted(os.listdir(_pkgs)):
                        if _pkg.startswith("Claude_"):
                            _cand = os.path.join(
                                _pkgs, _pkg, "LocalCache", "Roaming", "Claude"
                            )
                            if os.path.isdir(_cand):
                                appdata_claude_dir = _cand
                                break
            projects_dir = os.path.join(os.path.expanduser("~"), ".claude", "projects")

    snapshot = build_snapshot(appdata_claude_dir, projects_dir, fixture_mode=args.state is not None)
    diagnosis_id = make_diagnosis_id(snapshot)

    # Load troubleshooting.json from the repo root (tools/ -> parent = repo root)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(script_dir)
    ts_path = os.path.join(repo_root, "troubleshooting.json")

    rows = []
    if os.path.isfile(ts_path):
        with open(ts_path, "r", encoding="utf-8") as fh:
            rows = json.load(fh)

    schema_ok = snapshot["schema_version"] == "recognised"
    matches = [row for row in rows if eval_match(row.get("match", {}), snapshot)]

    # VS Code session cache check — live mode only; skipped in fixture/json mode
    # so golden outputs stay deterministic.
    vscode_dropped = 0
    vscode_db_count = 0
    if not args.state and not args.json_output:
        vscode_dropped, vscode_db_count = scan_vscode_dropped_sessions(projects_dir)

    if args.json_output:
        output = {
            "diagnosis_id": diagnosis_id,
            "tested_against": {
                "claude_desktop": snapshot.get("desktop_version"),
                "claude_code_cli": snapshot.get("cli_version"),
                "windows": "11",
            },
            "schema_probe": snapshot["schema_version"],
            "install_type": snapshot.get("install_type"),
            "msix_real_path": _redact_user_home(snapshot.get("msix_real_path")),
            "desktop_running": snapshot["desktop_running"],
            "matched_problems": [
                {
                    "id": row["id"],
                    "domain": row["domain"],
                    "mutator": row.get("mutator"),
                    "next_command": (
                        "python {} --diagnosis-id {}".format(row["mutator"], diagnosis_id)
                        if row.get("mutator") else None
                    ),
                    "safety_preconditions": [row["safety"]],
                }
                for row in matches
            ],
            "audit_only_problems": [],
            "schema_mismatch": not schema_ok,
            "snapshot": snapshot,
        }
        print(json.dumps(output, indent=2))
    else:
        print(_format_human(
            diagnosis_id, snapshot, matches, schema_ok, repo_root,
            vscode_dropped=vscode_dropped, vscode_db_count=vscode_db_count,
        ))


if __name__ == "__main__":
    main()
