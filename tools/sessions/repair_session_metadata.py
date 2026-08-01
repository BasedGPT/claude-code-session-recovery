"""
Invoked via diagnose.py. Not intended for direct invocation.
To diagnose your state: python tools/diagnose.py

Repair session metadata files that are missing the cliSessionId field.
Adds cliSessionId by matching each broken metadata file's createdAt
timestamp against the first timestamp in each JSONL transcript, within
a configurable window (default: 5 seconds). Single-candidate matches
only -- ambiguous matches are skipped with a report.

Files read:
  - %APPDATA%\\Claude\\claude-code-sessions\\<account-uuid>\\<org-uuid>\\local_*.json
  - %USERPROFILE%\\.claude\\projects\\<slug>\\*.jsonl

Files written:
  - %APPDATA%\\Claude\\claude-code-sessions\\<account-uuid>\\<org-uuid>\\<filename>.json
    (only with --apply; cliSessionId field added in-place)

Backup created at:
  - ./repair-backup/<account-uuid>/<org-uuid>/<original-filename>.json
    (alongside this script; account/org directories prevent filename collisions)

Rollback:
  - restore each file from the backup tree to its matching account/org directory

Usage:
    python tools/sessions/repair_session_metadata.py --diagnosis-id <hex>
    python tools/sessions/repair_session_metadata.py --diagnosis-id <hex> --apply
    python tools/sessions/repair_session_metadata.py --diagnosis-id <hex> --window-ms 8000
"""
import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone

# Import shared session-state helpers from tools/ (parent directory).
_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _TOOLS_DIR)
try:
    from platform_support import desktop_process_check_command, desktop_process_running
    from session_state import default_claude_paths, find_metadata_directories
    from mutator_safety import (
        current_snapshot_and_diagnosis_id, diagnosis_mode,
        metadata_backup_path, resolve_state_paths, verified_backup,
        write_json_in_place,
    )
    from transcript_files import build_transcript_index, first_iso_timestamp_and_user
except ImportError as exc:
    print("ERROR: cannot import from diagnose.py: {}".format(exc))
    print("Run from the repo root: python tools/sessions/repair_session_metadata.py")
    sys.exit(1)

# --- Configuration ---
# Used when --state is not supplied (live mode).

APPDATA_CLAUDE_DIR, PROJECTS_DIR = default_claude_paths()

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(TOOL_DIR, "repair-backup")


# ---------------------------------------------------------------------------
# Gate 5 -- Known-do-not-run conditions
# Checked after diagnosis-token validation.
# Refusal exits 3 with the message.
# ---------------------------------------------------------------------------

def _msix_do_not_run_message(s):
    real = s.get("msix_real_path")
    path_note = f" Your data is at: {real}" if real else ""
    return (
        "Microsoft Store (MSIX) install detected."
        " repair_session_metadata.py is unlikely to surface sessions on MSIX:"
        " Desktop maintains an internal session index that takes precedence over"
        " externally-written files, regardless of write access to the package path."
        f"{path_note}"
        " Use 'claude --resume' in the project directory to re-enter a session."
        " Run diagnose.py to see the full state report."
    )


KNOWN_DO_NOT_RUN = [
    (
        lambda s: s["metadata_missing_cli_count"] == 0,
        "All metadata files already have cliSessionId. Nothing to repair.",
    ),
    (
        lambda s: s["schema_version"] == "unrecognised",
        (
            "State schema not recognised. Run diagnose.py and report "
            "the unsupported state to the maintainer."
        ),
    ),
    (
        lambda s: s.get("install_type") == "msix",
        _msix_do_not_run_message,
    ),
]


# ---------------------------------------------------------------------------
# Indexing helpers
# ---------------------------------------------------------------------------

def _parse_created_at_ms(value):
    """Return createdAt as ms-since-epoch int, handling int or ISO string."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except ValueError:
            return None
    return None


def index_metadata(appdata_claude_dir):
    """Return (by_cli, broken_no_cli).

    by_cli: {cliSessionId: (path, parsed_dict)}
    broken_no_cli: [(path, parsed_dict)] for files lacking cliSessionId
    """
    by_cli = {}
    broken = []
    for _acct, _org, meta_dir in find_metadata_directories(appdata_claude_dir):
        for f in sorted(glob.glob(os.path.join(meta_dir, "local_*.json"))):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            cli = data.get("cliSessionId")
            if cli:
                by_cli[cli] = (f, data)
            else:
                # Archived sessions are hidden from Desktop's picker, so
                # repairing a missing cliSessionId on one has no user-visible
                # effect. Skip them so the repair loop does not attempt
                # timestamp-matching against the JSONL pool for entries the
                # user deliberately archived.
                if not data.get("isArchived"):
                    broken.append((f, data))
    return by_cli, broken


def index_jsonls(projects_dir):
    """Return {session_id: (jsonl_path, first_ts_ms, first_user_text)}."""
    out = {}
    for sid, path in build_transcript_index(projects_dir).items():
        first_ts_ms, first_user = first_iso_timestamp_and_user(path)
        out[sid] = (path, first_ts_ms, first_user)
    return out


# ---------------------------------------------------------------------------
# Match logic
# ---------------------------------------------------------------------------

def find_match(broken_meta, jsonl_index, by_cli, window_ms):
    """Return (cliSessionId, ambiguity_label).

    Labels:
      "unique"              -- single candidate in window, not yet claimed
      "unique-with-dupe"    -- single candidate, already claimed by another meta
      "none"                -- no candidate in window
      "multi"               -- multiple candidates too close to distinguish
    """
    created_ms = _parse_created_at_ms(broken_meta.get("createdAt"))
    if created_ms is None:
        return None, "none"

    candidates = []
    for sid, (path, jfirst, juser) in jsonl_index.items():
        if jfirst is None:
            continue
        delta = abs(jfirst - created_ms)
        if delta <= window_ms:
            candidates.append((delta, sid, path, juser))
    candidates.sort()

    if not candidates:
        return None, "none"
    if len(candidates) > 1 and candidates[1][0] - candidates[0][0] < 500:
        return candidates[0][1], "multi"

    cli_match = candidates[0][1]
    if cli_match in by_cli:
        return cli_match, "unique-with-dupe"
    return cli_match, "unique"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(
        description=(
            "Repair session metadata files missing cliSessionId. "
            "Dry-run by default -- add --apply to mutate."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--diagnosis-id",
        metavar="HEX",
        default=None,
        dest="diagnosis_id",
        help="Diagnosis token from diagnose.py (required).",
    )
    ap.add_argument(
        "--force-with-diagnosis-id",
        metavar="VALUE",
        default=None,
        dest="force_diagnosis_id",
        help="Set to 'audit-only' to run dry-run without a current token.",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Apply repairs in-place. Default is dry-run.",
    )
    ap.add_argument(
        "--window-ms",
        type=int,
        default=5000,
        metavar="MS",
        help="createdAt vs JSONL first-timestamp match window in ms (default: 5000).",
    )
    ap.add_argument(
        "--state",
        metavar="PATH",
        default=None,
        help=(
            "Fixture state directory for testing. "
            "Must contain appdata/Claude/... and projects/ subdirectories."
        ),
    )
    args = ap.parse_args()

    # --- Gate 3: diagnosis-token check ---
    force_mode, invocation_error = diagnosis_mode(
        args.diagnosis_id, args.force_diagnosis_id, args.apply,
    )
    if invocation_error == "missing":
        print("ERROR: --diagnosis-id required.")
        print("Run: python tools/diagnose.py")
        sys.exit(2)
    if invocation_error == "force_apply":
        print("ERROR: --apply cannot be combined with --force-with-diagnosis-id=audit-only.")
        sys.exit(2)

    # Resolve directories
    appdata_claude_dir, projects_dir = resolve_state_paths(
        args.state, APPDATA_CLAUDE_DIR, PROJECTS_DIR,
    )

    # Compute current snapshot and diagnosis ID
    snapshot, current_id = current_snapshot_and_diagnosis_id(
        appdata_claude_dir, projects_dir, fixture_mode=(args.state is not None),
    )

    if not force_mode and current_id != args.diagnosis_id:
        print(
            "ERROR: Diagnosis token mismatch.\n"
            "  Supplied : {}\n"
            "  Current  : {}".format(args.diagnosis_id, current_id)
        )
        print(
            "State has changed since diagnose.py was last run. "
            "Re-run: python tools/diagnose.py"
        )
        sys.exit(2)

    # --- Gate 5: known-do-not-run conditions ---
    for predicate, message in KNOWN_DO_NOT_RUN:
        try:
            if predicate(snapshot):
                msg = message(snapshot) if callable(message) else message
                print("REFUSED: " + msg)
                sys.exit(3)
        except Exception:
            pass

    # Re-check immediately before live-mode mutation. Fixture applies are
    # isolated from the user's Desktop process and intentionally skip this.
    if args.apply and args.state is None and desktop_process_running():
        print("REFUSED: Claude Desktop is running.")
        print("Quit Claude Desktop fully, then verify with:")
        print("  {}".format(desktop_process_check_command()))
        print("Then re-run with --apply.")
        sys.exit(3)

    # Index state
    by_cli, broken = index_metadata(appdata_claude_dir)
    jsonl_index = index_jsonls(projects_dir)

    used_diagnosis_id = args.diagnosis_id if not force_mode else "(forced-audit-only)"
    print("Metadata files: {} (linked: {}, missing cliSessionId: {})".format(
        len(by_cli) + len(broken), len(by_cli), len(broken)
    ))
    print("JSONL files: {}".format(len(jsonl_index)))
    print("Match window: +-{}ms".format(args.window_ms))
    print("Mode: {}".format("APPLY" if args.apply else "dry-run (use --apply to mutate)"))
    print("Diagnosis ID: {}".format(used_diagnosis_id))
    print()

    if args.apply:
        os.makedirs(BACKUP_DIR, exist_ok=True)

    repaired = 0
    refused_multi = 0
    orphan = 0

    for path, meta in sorted(broken, key=lambda x: _parse_created_at_ms(x[1].get("createdAt")) or 0):
        fname = os.path.basename(path)
        title = meta.get("title", "")[:60]
        created_ms = _parse_created_at_ms(meta.get("createdAt"))
        created_display = (
            datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            if created_ms else "?"
        )

        cli_match, kind = find_match(meta, jsonl_index, by_cli, args.window_ms)

        if kind == "none":
            print("  ORPHAN  {} | {} | {!r} -- no JSONL match in window".format(
                fname, created_display, title
            ))
            orphan += 1
            continue

        if kind == "multi":
            print("  REFUSE  {} | {} | {!r} -- multiple JSONL candidates, manual review needed".format(
                fname, created_display, title
            ))
            refused_multi += 1
            continue

        _jsonl_path, _jfirst, juser = jsonl_index[cli_match]
        dupe_note = " (dupe already linked)" if kind == "unique-with-dupe" else ""

        print("  REPAIR  {}{}".format(fname, dupe_note))
        print("          {} | {!r}".format(created_display, title))
        print("          cliSessionId = {}".format(cli_match))
        if juser:
            print("          first user: {!r}".format(juser))
        print()

        if args.apply:
            backup_path = metadata_backup_path(path, appdata_claude_dir, BACKUP_DIR)
            print("          backup: {}".format(backup_path))
            print("          rollback: restore {} -> {}".format(backup_path, path))
            verified_backup(
                path,
                backup_path,
            )
            repaired_meta = dict(meta)
            repaired_meta["cliSessionId"] = cli_match
            repaired_meta.pop("transcriptUnavailable", None)
            write_json_in_place(path, repaired_meta)

        repaired += 1

    print("Repaired: {}  Refused (multi-candidate): {}  Orphan (no JSONL): {}".format(
        repaired, refused_multi, orphan
    ))
    if not args.apply and repaired:
        print("Review dry-run output above, then re-run with --apply to apply changes.")


if __name__ == "__main__":
    main()
