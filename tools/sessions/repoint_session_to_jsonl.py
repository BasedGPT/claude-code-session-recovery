"""
Invoked via diagnose.py. Not intended for direct invocation.
To diagnose your state: python tools/diagnose.py

Correct Desktop metadata where the recorded cwd no longer matches the slug
directory that holds the session's JSONL transcript. Claude Code resolves
cwd to its canonical (realpath) form before slug-encoding; a rename,
junction removal, or folder move silently leaves the metadata's cwd pointing
at the old path while the JSONL migrates to the new slug directory.

This tool discovers mismatches automatically and rewrites the affected
metadata files so the cwd field slug-encodes to the directory where the
JSONL actually lives. JSONL files are never moved.

Files read:
  - %APPDATA%\\Claude\\claude-code-sessions\\<account-uuid>\\<org-uuid>\\local_*.json
  - %USERPROFILE%\\.claude\\projects\\<slug>\\*.jsonl

Files written:
  - (nothing with dry-run / default)
  - %APPDATA%\\Claude\\claude-code-sessions\\<account-uuid>\\<org-uuid>\\local_<uuid>.json
    (updated in place, with --apply only)

Backup created at:
  - ./repair-backup/<timestamp>/<filename>.json  (alongside this script, always)

Rollback command:
  - copy /Y ".\\repair-backup\\<timestamp>\\local_<uuid>.json"
         "%APPDATA%\\Claude\\claude-code-sessions\\<account-uuid>\\<org-uuid>\\local_<uuid>.json"
    (overwrites the applied file with the original)

Caveats:
  - This tool updates cwd and originCwd. It does not move JSONL files.
  - worktreePath is updated only when it matched the old cwd exactly.
  - The new cwd is read from the first cwd field in the JSONL transcript.
    If the transcript contains no cwd record, the session is skipped.

Usage:
    python tools/sessions/repoint_session_to_jsonl.py --diagnosis-id <hex>
    python tools/sessions/repoint_session_to_jsonl.py --diagnosis-id <hex> --apply
"""
import argparse
import glob
import json
import os
import shutil
import sys
from datetime import datetime

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _TOOLS_DIR)
try:
    from diagnose import (
        build_snapshot, make_diagnosis_id, _find_meta_dirs,
        _build_jsonl_index, _slug_encode,
    )
except ImportError as exc:
    print("ERROR: cannot import from diagnose.py: {}".format(exc))
    print("Run from the repo root: python tools/sessions/repoint_session_to_jsonl.py")
    sys.exit(1)

# --- Configuration ---
APPDATA_CLAUDE_DIR = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")), "Claude"
)
PROJECTS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "projects")

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# Gate 5 -- Known-do-not-run conditions
# ---------------------------------------------------------------------------

KNOWN_DO_NOT_RUN = [
    (
        lambda s: s["cwd_slug_mismatch_count"] == 0,
        "No sessions have a cwd slug mismatch. Nothing to do.",
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
# JSONL reading
# ---------------------------------------------------------------------------

def _read_cwd_from_jsonl(path):
    """Return the first cwd field found in a JSONL transcript, or None."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = rec.get("cwd")
                if cwd:
                    return cwd
    except OSError:
        pass
    return None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _find_mismatches(appdata_claude_dir, projects_dir):
    """Return list of (meta_path, meta_dict, old_cwd, new_cwd, actual_slug_dir)."""
    jsonl_index = _build_jsonl_index(projects_dir)
    results = []

    for _acct, _org, meta_dir in _find_meta_dirs(appdata_claude_dir):
        for f in sorted(glob.glob(os.path.join(meta_dir, "local_*.json"))):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    meta = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue

            cli = meta.get("cliSessionId")
            cwd = meta.get("cwd", "")
            if not cli or not cwd:
                continue
            if cli not in jsonl_index:
                continue

            expected_slug = _slug_encode(cwd)
            jsonl_path = jsonl_index[cli]
            actual_slug_dir = os.path.basename(os.path.dirname(jsonl_path))

            if actual_slug_dir == expected_slug:
                continue

            new_cwd = _read_cwd_from_jsonl(jsonl_path)
            results.append((f, meta, cwd, new_cwd, actual_slug_dir))

    return results


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def _plan_changes(meta, old_cwd, new_cwd):
    """Return (new_meta_or_None, change_log).

    Returns (None, log) when new_cwd is unresolvable -- caller should skip.
    """
    if new_cwd is None:
        return None, [
            "SKIP: no cwd field found in JSONL transcript -- cannot determine correct cwd."
        ]

    if _slug_encode(new_cwd) != _slug_encode(old_cwd):
        pass  # expected -- this is the mismatch we are fixing

    log = []
    new_meta = dict(meta)

    # cwd
    if meta.get("cwd") != new_cwd:
        log.append("cwd: {!r} -> {!r}".format(meta.get("cwd"), new_cwd))
        new_meta["cwd"] = new_cwd

    # originCwd: update only when it matched the old cwd
    if meta.get("originCwd") == old_cwd and old_cwd != new_cwd:
        log.append("originCwd: {!r} -> {!r}".format(meta["originCwd"], new_cwd))
        new_meta["originCwd"] = new_cwd

    # worktreePath: update only when it matched the old cwd
    if meta.get("worktreePath") == old_cwd and old_cwd != new_cwd:
        log.append("worktreePath: {!r} -> {!r}".format(meta["worktreePath"], new_cwd))
        new_meta["worktreePath"] = new_cwd

    if not log:
        log.append("(no field changes required -- slug already consistent)")

    return new_meta, log


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(
        description=(
            "Update metadata cwd to match the slug directory containing each "
            "session's JSONL. Dry-run by default -- add --apply to write to AppData."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--diagnosis-id",
        metavar="HEX", default=None, dest="diagnosis_id",
        help="Diagnosis token from diagnose.py (required).",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Write updated metadata files to AppData. Default is dry-run only.",
    )
    ap.add_argument(
        "--state",
        metavar="PATH", default=None,
        help="Fixture state directory for testing.",
    )
    args = ap.parse_args()

    # Gate 3: diagnosis-token check
    if not args.diagnosis_id:
        print("ERROR: --diagnosis-id required.")
        print("Run: python tools/diagnose.py")
        sys.exit(2)

    # Resolve directories
    if args.state:
        state_abs = os.path.abspath(args.state)
        appdata_claude_dir = os.path.join(state_abs, "appdata", "Claude")
        projects_dir = os.path.join(state_abs, "projects")
    else:
        appdata_claude_dir = APPDATA_CLAUDE_DIR
        projects_dir = PROJECTS_DIR

    snapshot = build_snapshot(
        appdata_claude_dir, projects_dir,
        fixture_mode=(args.state is not None),
    )
    current_id = make_diagnosis_id(snapshot)

    if current_id != args.diagnosis_id:
        print(
            "ERROR: Diagnosis token mismatch.\n"
            "  Supplied : {}\n"
            "  Current  : {}".format(args.diagnosis_id, current_id)
        )
        print("State has changed since diagnose.py was last run. Re-run: python tools/diagnose.py")
        sys.exit(2)

    # Gate 5: known-do-not-run conditions
    for predicate, message in KNOWN_DO_NOT_RUN:
        try:
            if predicate(snapshot):
                print("REFUSED: " + message)
                sys.exit(3)
        except Exception:
            pass

    mismatches = _find_mismatches(appdata_claude_dir, projects_dir)

    print("Sessions with cwd slug mismatch: {}".format(len(mismatches)))
    print("Mode: {}".format("APPLY (writing to AppData)" if args.apply else "dry-run"))
    print("Diagnosis ID: {}".format(args.diagnosis_id))
    print()

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")  # standards: local timing
    backup_dir = os.path.join(TOOL_DIR, "repair-backup", ts)

    updated = 0
    skipped = 0
    failed = 0

    for meta_path, meta, old_cwd, new_cwd, actual_slug_dir in mismatches:
        meta_name = os.path.basename(meta_path)
        sid = meta.get("cliSessionId", "(unknown)")

        new_meta, log = _plan_changes(meta, old_cwd, new_cwd)

        print("  REPOINT {}".format(sid))
        print("          file={}".format(meta_name))
        print("          slug_dir={}".format(actual_slug_dir))
        for line in log:
            print("          {}".format(line))
        print()

        if new_meta is None:
            skipped += 1
            continue

        if not args.apply:
            updated += 1
            continue

        # APPLY -----------------------------------------------------------------

        os.makedirs(backup_dir, exist_ok=True)
        backup_path = os.path.join(backup_dir, meta_name)
        try:
            shutil.copy2(meta_path, backup_path)
        except OSError as e:
            print("  FAIL {}: backup failed: {}".format(meta_name, e))
            failed += 1
            continue

        tmp_path = meta_path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(new_meta, fh, indent=2)
                fh.write("\n")
            os.replace(tmp_path, meta_path)
        except OSError as e:
            print("  FAIL {}: write failed: {}".format(meta_name, e))
            # Attempt to remove partial tmp
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            failed += 1
            continue

        print("  APPLIED -> {}".format(meta_path))
        updated += 1

    print("Updated: {}  Skipped: {}  Failed: {}".format(updated, skipped, failed))
    if args.apply and updated:
        print("Backups at: {}".format(backup_dir))
    elif not args.apply and updated:
        print("\nRe-run with --apply to write changes to AppData.")


if __name__ == "__main__":
    main()
