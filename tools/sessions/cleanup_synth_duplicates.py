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
  - ./cleanup-backup/<original-filename>.json  (alongside this script)

Rollback command:
  - copy /Y cleanup-backup\\*.json "%APPDATA%\\Claude\\claude-code-sessions\\<account-uuid>\\<org-uuid>\\"

Usage:
    python tools/sessions/cleanup_synth_duplicates.py --diagnosis-id <hex>
    python tools/sessions/cleanup_synth_duplicates.py --diagnosis-id <hex> --apply
"""
import argparse
import glob
import json
import os
import platform
import shutil
import sys
from datetime import datetime, timezone

# Import shared helpers from tools/diagnose.py (parent directory).
_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _TOOLS_DIR)
try:
    from diagnose import build_snapshot, make_diagnosis_id, _find_meta_dirs
except ImportError as exc:
    print("ERROR: cannot import from diagnose.py: {}".format(exc))
    print("Run from the repo root: python tools/sessions/cleanup_synth_duplicates.py")
    sys.exit(1)

# --- Configuration ---

def _default_appdata_claude_dir():
    """Return the platform-appropriate Claude app-data directory."""
    _sys = platform.system()
    if _sys == "Darwin":
        return os.path.expanduser("~/Library/Application Support/Claude")
    if _sys == "Linux":
        return os.path.expanduser("~/.config/Claude")
    return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Claude")


APPDATA_CLAUDE_DIR = _default_appdata_claude_dir()

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
    for _acct, _org, meta_dir in _find_meta_dirs(appdata_claude_dir):
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
    force_mode = args.force_diagnosis_id == "audit-only"
    if not args.diagnosis_id and not force_mode:
        print("ERROR: --diagnosis-id required.")
        print("Run: python tools/diagnose.py")
        sys.exit(2)
    if args.apply and force_mode:
        print("ERROR: --apply cannot be combined with --force-with-diagnosis-id=audit-only.")
        sys.exit(2)

    # Resolve directories
    if args.state:
        state_abs = os.path.abspath(args.state)
        appdata_claude_dir = os.path.join(state_abs, "appdata", "Claude")
        projects_dir = os.path.join(state_abs, "projects")
    else:
        appdata_claude_dir = APPDATA_CLAUDE_DIR
        projects_dir = os.path.join(os.path.expanduser("~"), ".claude", "projects")

    # Compute current snapshot and diagnosis ID
    snapshot = build_snapshot(
        appdata_claude_dir, projects_dir,
        fixture_mode=(args.state is not None),
    )
    current_id = make_diagnosis_id(snapshot)

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
                print("REFUSED: " + message)
                sys.exit(3)
        except Exception:
            pass

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
                shutil.copy2(path, os.path.join(BACKUP_DIR, fname))
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
