"""
Invoked via diagnose.py. Not intended for direct invocation.
To diagnose your state: python tools/diagnose.py

Rewrite cwd, originCwd, and worktreePath in AppData metadata files when a
project has been moved or renamed. Replaces every occurrence of the old path
prefix with the new path prefix across all affected fields.

Common scenario: a project root was renamed or moved after Claude Code sessions
were already recorded against it. Desktop history entries still show the old
cwd and will not resume correctly. This script patches the stored paths so the
Desktop history links to the correct location.

Safety: refuses to run in apply mode if Claude Desktop is detected, because Desktop
holds metadata in memory and will silently overwrite disk changes on its next
periodic flush (typically within a few minutes of Desktop being open).

Files read:
  - %APPDATA%\\Claude\\claude-code-sessions\\<account-uuid>\\<org-uuid>\\local_*.json

Files written:
  - %APPDATA%\\Claude\\claude-code-sessions\\<account-uuid>\\<org-uuid>\\<filename>.json
    (only with --apply; cwd-related fields rewritten in-place)

Backup created at:
  - ./cwd-rewrite-backup/<account-uuid>/<org-uuid>/<original-filename>.json
    (alongside this script; account/org directories prevent filename collisions)

Rollback:
  - restore each file from the backup tree to its matching account/org directory

Usage:
    # Dry-run: show what would change
    python tools/sessions/rewrite_metadata_cwd.py --old-cwd "C:\\old\\path" --new-cwd "C:\\new\\path"

    # Apply: backup originals then rewrite
    python tools/sessions/rewrite_metadata_cwd.py --old-cwd "C:\\old\\path" --new-cwd "C:\\new\\path" --apply
"""
import argparse
import glob
import json
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Import shared helpers from tools/diagnose.py (parent directory).
_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _TOOLS_DIR)
try:
    from session_state import default_claude_appdata_dir, find_metadata_directories
    from mutator_safety import (
        current_snapshot_and_diagnosis_id,
        desktop_process_running as _desktop_running,
        diagnosis_mode, metadata_backup_path, resolve_state_paths,
        verified_backup, write_json_in_place,
    )
except ImportError as exc:
    print("ERROR: cannot import from diagnose.py: {}".format(exc))
    print("Run from the repo root: python tools/sessions/rewrite_metadata_cwd.py")
    sys.exit(1)

# --- Configuration ---

APPDATA_CLAUDE_DIR = default_claude_appdata_dir()

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(TOOL_DIR, "cwd-rewrite-backup")

# Fields that may contain the old path prefix
CWD_FIELDS = ("cwd", "originCwd", "worktreePath")


# ---------------------------------------------------------------------------
# Gate 5 -- Known-do-not-run conditions
# ---------------------------------------------------------------------------

def _make_known_do_not_run(old_cwd):
    return [
        (
            lambda s: s["schema_version"] == "unrecognised",
            (
                "State schema not recognised. Run diagnose.py and report "
                "the unsupported state to the maintainer."
            ),
        ),
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_targets(appdata_claude_dir, old_lower):
    """Return [(path, parsed_dict)] for metadata files containing old path."""
    targets = []
    for _acct, _org, meta_dir in find_metadata_directories(appdata_claude_dir):
        for f in sorted(glob.glob(os.path.join(meta_dir, "local_*.json"))):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            needs_update = any(
                old_lower in data.get(field, "").lower()
                for field in CWD_FIELDS
            )
            if needs_update:
                targets.append((f, data))
    targets.sort(key=lambda x: x[1].get("lastActivityAt", 0), reverse=True)
    return targets


def _apply_rewrites(data, old_cwd, new_cwd):
    """Return (updated_dict, list_of_changed_fields)."""
    old_lower = old_cwd.lower()
    updated = dict(data)
    changed = []
    for field in CWD_FIELDS:
        val = data.get(field, "")
        if val and old_lower in val.lower():
            updated[field] = val.replace(old_cwd, new_cwd)
            changed.append(field)
    return updated, changed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description=(
            "Rewrite cwd/originCwd in metadata files after a project rename. "
            "Dry-run by default -- add --apply to mutate."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--old-cwd",
        metavar="PATH",
        required=True,
        dest="old_cwd",
        help="Old project path prefix to replace (case-insensitive match).",
    )
    ap.add_argument(
        "--new-cwd",
        metavar="PATH",
        required=True,
        dest="new_cwd",
        help="New project path prefix to use instead.",
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
        help="Backup originals and rewrite in-place. Default is dry-run.",
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
    for predicate, message in _make_known_do_not_run(args.old_cwd):
        try:
            if predicate(snapshot):
                print("REFUSED: " + message)
                sys.exit(3)
        except Exception:
            pass

    # Desktop running check (apply mode only)
    if args.apply and not args.state and _desktop_running():
        from platform_support import desktop_process_check_command

        print("REFUSED: Claude Desktop is running.")
        print("Quit Claude Desktop fully, then verify with:")
        print("  {}".format(desktop_process_check_command()))
        print("Then re-run with --apply.")
        sys.exit(3)

    old_cwd = args.old_cwd
    new_cwd = args.new_cwd
    old_lower = old_cwd.lower()

    targets = _find_targets(appdata_claude_dir, old_lower)
    used_diagnosis_id = args.diagnosis_id if not force_mode else "(forced-audit-only)"

    print("Metadata files with old cwd: {}".format(len(targets)))
    print("OLD: {}".format(old_cwd))
    print("NEW: {}".format(new_cwd))
    print("Mode: {}".format("APPLY" if args.apply else "dry-run (use --apply to mutate)"))
    print("Diagnosis ID: {}".format(used_diagnosis_id))
    print()

    if not targets:
        print("No metadata files contain the old cwd. Nothing to do.")
        return

    if args.apply:
        os.makedirs(BACKUP_DIR, exist_ok=True)

    changed_count = 0

    for path, data in targets:
        fname = os.path.basename(path)
        updated, changed_fields = _apply_rewrites(data, old_cwd, new_cwd)
        title = data.get("title", "")[:60].encode("ascii", errors="replace").decode()
        cli = data.get("cliSessionId", "MISSING")

        print("  {}".format(fname))
        print("    title:   {!r}".format(title))
        print("    cli:     {}".format(cli))
        print("    changes: {}".format(changed_fields))
        for field in changed_fields:
            print("    {}: old-path -> new-path".format(field))

        if args.apply:
            backup_path = metadata_backup_path(path, appdata_claude_dir, BACKUP_DIR)
            print("    BACKUP -> {}".format(backup_path))
            print("    ROLLBACK: restore {} -> {}".format(backup_path, path))
            verified_backup(
                path,
                backup_path,
            )
            write_json_in_place(path, updated)
            print("    WRITTEN (backup at {})".format(
                os.path.relpath(backup_path, TOOL_DIR).replace(os.sep, "/")
            ))
        else:
            print("    [dry-run]")
        print()
        changed_count += 1

    if args.apply:
        print("Rewritten: {}".format(changed_count))
        print("Backups:   cwd-rewrite-backup/")
        print("Restore each backup to its matching account/org metadata directory;")
        print("the backup tree preserves those relative paths.")
    else:
        print("Total that would be updated: {}".format(changed_count))
        print("Re-run with --apply to execute. Desktop must be fully quit first.")


if __name__ == "__main__":
    main()
