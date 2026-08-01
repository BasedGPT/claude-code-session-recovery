"""
Invoked via diagnose.py. Not intended for direct invocation.
To diagnose your state: python tools/diagnose.py

Delete synthetic-duplicate metadata files created by a prior recovery attempt.
A synthetic duplicate exists when two (or more) metadata files in AppData share
the same cliSessionId -- meaning two Desktop history entries both render the
same JSONL transcript. The synthetic file is identified by sessionId != cliSessionId
(Claude Desktop sets them equal; recovery scripts use a fresh UUID for sessionId
while pointing cliSessionId at the existing JSONL session ID).

Files read:
  - %APPDATA%\\Claude\\claude-code-sessions\\<account-uuid>\\<org-uuid>\\local_*.json

Files written:
  - (nothing with dry-run / default)
  - %APPDATA%\\Claude\\claude-code-sessions\\<account-uuid>\\<org-uuid>\\<filename>.json
    (deleted, with --apply only)

Backup created at:
  - ./cleanup-backup/<account-uuid>/<org-uuid>/<original-filename>.json
    (alongside this script; account/org directories prevent filename collisions)

Rollback:
  - restore each file from the backup tree to its matching account/org directory

Usage:
    python tools/sessions/cleanup_synth_duplicates.py --diagnosis-id <hex>
    python tools/sessions/cleanup_synth_duplicates.py --diagnosis-id <hex> --apply
"""
import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone

# Import shared helpers from tools/diagnose.py (parent directory).
_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _TOOLS_DIR)
try:
    from platform_support import desktop_process_check_command, desktop_process_running
    from session_state import default_claude_appdata_dir, find_metadata_directories
    from mutator_safety import (
        current_snapshot_and_diagnosis_id, diagnosis_mode, resolve_state_paths,
        metadata_backup_path, verified_backup,
    )
except ImportError as exc:
    print("ERROR: cannot import from diagnose.py: {}".format(exc))
    print("Run from the repo root: python tools/sessions/cleanup_synth_duplicates.py")
    sys.exit(1)

# --- Configuration ---

APPDATA_CLAUDE_DIR = default_claude_appdata_dir()

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(TOOL_DIR, "cleanup-backup")


# ---------------------------------------------------------------------------
# Gate 5 -- Known-do-not-run conditions
# Checked after diagnosis-token validation. Refusal exits 3.
# ---------------------------------------------------------------------------

KNOWN_DO_NOT_RUN = [
    (
        lambda s: s["metadata_duplicate_cli_count"] == 0,
        "No duplicate cliSessionId values found. Nothing to clean up.",
    ),
    (
        lambda s: s["schema_version"] == "unrecognised",
        (
            "State schema not recognised. Run diagnose.py and report "
            "the unsupported state to the maintainer."
        ),
    ),
]


# ---------------------------------------------------------------------------
# Indexing helpers
# ---------------------------------------------------------------------------

def _created_at_ms(data):
    """Return createdAt as ms-since-epoch int, handling int or ISO string."""
    value = data.get("createdAt")
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except ValueError:
            return 0
    return 0


def _created_display(ms):
    if not ms:
        return "?"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def index_metadata(appdata_claude_dir):
    """Return list of (path, parsed_dict) for all local_*.json files."""
    rows = []
    for _acct, _org, meta_dir in find_metadata_directories(appdata_claude_dir):
        for f in sorted(glob.glob(os.path.join(meta_dir, "local_*.json"))):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            rows.append((f, data))
    return rows


def find_duplicate_groups(rows):
    """Return groups: {cliSessionId: [(path, data), ...]} for cliSessionId
    values that appear in more than one metadata file."""
    by_cli = {}
    for path, data in rows:
        cli = data.get("cliSessionId")
        if not cli:
            continue
        by_cli.setdefault(cli, []).append((path, data))
    return {cli: group for cli, group in by_cli.items() if len(group) > 1}


def classify_group(cli_id, group):
    """Classify a group of files sharing cli_id.

    Returns (to_delete, to_keep, refusal_reason):
      to_delete -- list of (path, data) for synthetic files
      to_keep   -- list of (path, data) for original files
      refusal_reason -- non-empty string if classification is ambiguous
    """
    synths = [(p, d) for p, d in group if d.get("sessionId") != cli_id]
    originals = [(p, d) for p, d in group if d.get("sessionId") == cli_id]

    if not originals:
        # All files are synthetic -- can't determine which to keep.
        return [], group, "all files have sessionId != cliSessionId; cannot determine original"

    if not synths:
        # All files have matching sessionId == cliSessionId -- unexpected.
        return [], group, "all files have sessionId == cliSessionId; manual review needed"

    return synths, originals, ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(
        description=(
            "Delete synthetic-duplicate metadata files. "
            "Dry-run by default -- add --apply to delete."
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
        help="Delete synthetic duplicates. Default is dry-run.",
    )
    ap.add_argument(
        "--state",
        metavar="PATH",
        default=None,
        help=(
            "Fixture state directory for testing. "
            "Must contain appdata/Claude/... subdirectory."
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
        args.state, APPDATA_CLAUDE_DIR,
        os.path.join(os.path.expanduser("~"), ".claude", "projects"),
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

    # Re-check immediately before live-mode deletion. Fixture applies are
    # isolated from the user's Desktop process and intentionally skip this.
    if args.apply and args.state is None and desktop_process_running():
        print("REFUSED: Claude Desktop is running.")
        print("Quit Claude Desktop fully, then verify with:")
        print("  {}".format(desktop_process_check_command()))
        print("Then re-run with --apply.")
        sys.exit(3)

    # Index state
    all_rows = index_metadata(appdata_claude_dir)
    dup_groups = find_duplicate_groups(all_rows)

    used_diagnosis_id = args.diagnosis_id if not force_mode else "(forced-audit-only)"
    print("Metadata files: {}  Duplicate cliSessionId count: {}".format(
        len(all_rows), len(dup_groups)
    ))
    print("Mode: {}".format("APPLY" if args.apply else "dry-run (use --apply to delete)"))
    print("Diagnosis ID: {}".format(used_diagnosis_id))
    print()

    if args.apply:
        os.makedirs(BACKUP_DIR, exist_ok=True)

    deleted = 0
    refused = 0

    for cli_id in sorted(dup_groups):
        group = dup_groups[cli_id]
        to_delete, to_keep, refusal_reason = classify_group(cli_id, group)

        if refusal_reason:
            print("  REFUSE  cliSessionId={}".format(cli_id))
            print("          {} files -- {}".format(len(group), refusal_reason))
            for p, d in sorted(group, key=lambda x: _created_at_ms(x[1])):
                print("          {} | sessionId={}".format(
                    os.path.basename(p), d.get("sessionId", "?")))
            print()
            refused += len(group)
            continue

        for path, data in sorted(to_delete, key=lambda x: _created_at_ms(x[1])):
            fname = os.path.basename(path)
            keep_names = ", ".join(
                os.path.basename(p) for p, _ in
                sorted(to_keep, key=lambda x: _created_at_ms(x[1]))
            )
            created_ms = _created_at_ms(data)

            print("  DELETE  {}".format(fname))
            print("          {} | cliSessionId={}".format(
                _created_display(created_ms), cli_id))
            print("          sessionId={} | original: {}".format(
                data.get("sessionId", "?"), keep_names))
            print()

            if args.apply:
                backup_path = metadata_backup_path(path, appdata_claude_dir, BACKUP_DIR)
                print("          backup: {}".format(backup_path))
                print("          rollback: restore {} -> {}".format(backup_path, path))
                verified_backup(
                    path,
                    backup_path,
                )
                os.remove(path)

            deleted += 1

    print("Deleted: {}{}  Refused (ambiguous): {}".format(
        deleted,
        " (dry-run)" if not args.apply else "",
        refused,
    ))
    if not args.apply and deleted:
        print("Review dry-run output above, then re-run with --apply to delete.")


if __name__ == "__main__":
    main()
