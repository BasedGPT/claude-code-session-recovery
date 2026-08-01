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
  - ./repair-backup/<timestamp>/<account-uuid>/<org-uuid>/<filename>.json
    (alongside this script, always)

Rollback:
  - restore the file from its timestamp/account/org backup path to the matching
    metadata directory (overwrites the applied file with the original)

Caveats:
  - This tool updates cwd and originCwd. It does not move JSONL files.
  - worktreePath is updated only when it matched the old cwd exactly.
  - The new cwd is read from the first cwd field in the JSONL transcript.
    If the transcript contains no cwd record, the session is skipped.
  - If the JSONL's recorded cwd encodes to a slug other than the directory
    the JSONL actually lives in, that cwd is itself stale (e.g. a junction
    that has been removed). The session is skipped with WARN -- manual
    intervention is required for these.

Usage:
    python tools/sessions/repoint_session_to_jsonl.py --diagnosis-id <hex>
    python tools/sessions/repoint_session_to_jsonl.py --diagnosis-id <hex> --apply
    python tools/sessions/repoint_session_to_jsonl.py --diagnosis-id <hex> --summary
"""
import argparse
import glob
import json
import os
import sys
from datetime import datetime

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _TOOLS_DIR)
try:
    from session_state import default_claude_paths, find_metadata_directories, slug_encode
    from mutator_safety import (
        atomic_write_json, current_snapshot_and_diagnosis_id, diagnosis_mode,
        metadata_backup_path, resolve_state_paths, verified_backup,
    )
    from transcript_files import build_transcript_index, first_cwd
except ImportError as exc:
    print("ERROR: cannot import from diagnose.py: {}".format(exc))
    print("Run from the repo root: python tools/sessions/repoint_session_to_jsonl.py")
    sys.exit(1)

# --- Configuration ---

APPDATA_CLAUDE_DIR, PROJECTS_DIR = default_claude_paths()

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
# Discovery
# ---------------------------------------------------------------------------

def _find_mismatches(appdata_claude_dir, projects_dir):
    """Return list of (meta_path, meta_dict, old_cwd, new_cwd, actual_slug_dir)."""
    jsonl_index = build_transcript_index(projects_dir)
    results = []

    for _acct, _org, meta_dir in find_metadata_directories(appdata_claude_dir):
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

            expected_slug = slug_encode(cwd)
            jsonl_path = jsonl_index[cli]
            actual_slug_dir = os.path.basename(os.path.dirname(jsonl_path))

            if actual_slug_dir == expected_slug:
                continue

            new_cwd = first_cwd(jsonl_path)
            results.append((f, meta, cwd, new_cwd, actual_slug_dir))

    return results


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def _plan_changes(meta, old_cwd, new_cwd, actual_slug_dir):
    """Return (new_meta_or_None, change_log).

    Returns (None, log) when new_cwd is unresolvable -- caller should skip.
    """
    if new_cwd is None:
        return None, [
            "SKIP: no cwd field found in JSONL transcript -- cannot determine correct cwd."
        ]

    # Validate that the cwd read from the JSONL actually encodes to the
    # directory where the JSONL lives. If it doesn't, the JSONL's recorded
    # cwd is itself stale (e.g. a junction that has since been removed), and
    # writing it would leave the metadata pointing at a path that doesn't
    # resolve to the right slug either.
    if slug_encode(new_cwd) != actual_slug_dir:
        return None, [
            "WARN: cwd from JSONL ({!r}) encodes to slug {!r}, "
            "which does not match the actual JSONL directory {!r}. "
            "The JSONL transcript's recorded cwd is itself stale "
            "(e.g. a junction that has been removed). "
            "Cannot automatically determine the correct cwd -- "
            "manual intervention required.".format(
                new_cwd, slug_encode(new_cwd), actual_slug_dir,
            )
        ]

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
    ap.add_argument(
        "--summary",
        action="store_true",
        help="One line per session instead of per-field detail. "
             "Useful when the mismatch count is large.",
    )
    args = ap.parse_args()

    # Gate 3: diagnosis-token check
    force_mode, invocation_error = diagnosis_mode(
        args.diagnosis_id, None, args.apply,
    )
    if invocation_error == "missing":
        print("ERROR: --diagnosis-id required.")
        print("Run: python tools/diagnose.py")
        sys.exit(2)
    # Resolve directories
    appdata_claude_dir, projects_dir = resolve_state_paths(
        args.state, APPDATA_CLAUDE_DIR, PROJECTS_DIR,
    )

    snapshot, current_id = current_snapshot_and_diagnosis_id(
        appdata_claude_dir, projects_dir, fixture_mode=(args.state is not None),
    )

    if not force_mode and current_id != args.diagnosis_id:
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

        new_meta, log = _plan_changes(meta, old_cwd, new_cwd, actual_slug_dir)

        if args.summary:
            if new_meta is None:
                # Pick the most informative log line for the summary
                msg = log[0] if log else "(no detail)"
                # Truncate to keep the line readable
                if len(msg) > 100:
                    msg = msg[:97] + "..."
                print("  SKIP    {} {} -- {}".format(sid, meta_name, msg))
            else:
                short_old = (old_cwd[:40] + "...") if len(old_cwd) > 43 else old_cwd
                short_new = (new_cwd[:40] + "...") if new_cwd and len(new_cwd) > 43 else (new_cwd or "")
                print("  REPOINT {} {} : {} -> {}".format(sid, meta_name, short_old, short_new))
        else:
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
        backup_path = metadata_backup_path(meta_path, appdata_claude_dir, backup_dir)
        try:
            verified_backup(meta_path, backup_path)
        except OSError as e:
            print("  FAIL {}: backup failed: {}".format(meta_name, e))
            failed += 1
            continue

        try:
            atomic_write_json(meta_path, new_meta, trailing_newline=True)
        except OSError as e:
            print("  FAIL {}: write failed: {}".format(meta_name, e))
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
