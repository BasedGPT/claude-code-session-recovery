"""
Search backup directories for JSONL transcripts missing from the live projects
directory.

For each metadata file whose cliSessionId points to a JSONL that no longer
exists under ~/.claude/projects/, this tool searches the configured backup
roots for a matching <cli-session-id>.jsonl file. Reports which sessions are
recoverable and which are gone entirely.

Recovery sources searched, in priority order:
  1. The path given by --backup (if provided).
  2. Each directory listed in BACKUP_ROOTS (edit the Configuration block below).
  3. Default cloud-sync locations probed automatically when neither of the
     above is configured.

Read-only. Mutates nothing.

Files read:
  - %APPDATA%\\Claude\\claude-code-sessions\\<account-uuid>\\<org-uuid>\\local_*.json
  - %USERPROFILE%\\.claude\\projects\\<slug>\\*.jsonl
  - Each path listed in BACKUP_ROOTS

Files written:
  - Nothing. Read-only.

Exit code:
  0  script ran successfully
  2  state schema not recognised

Usage:
  python tools/sessions/find_missing_jsonls_in_backup.py
  python tools/sessions/find_missing_jsonls_in_backup.py --quiet
  python tools/sessions/find_missing_jsonls_in_backup.py --backup <path>
  python tools/sessions/find_missing_jsonls_in_backup.py --state <fixture-state-path>
"""
import argparse
import glob
import json
import os
import sys

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _TOOLS_DIR)
try:
    from session_state import (
        build_snapshot, default_claude_paths, find_metadata_directories,
    )
    from transcript_files import build_transcript_path_inventory
except ImportError as exc:
    print("ERROR: cannot import from diagnose.py: {}".format(exc))
    print("Run from the repo root: python tools/sessions/find_missing_jsonls_in_backup.py")
    sys.exit(1)

# --- Configuration ---
APPDATA_CLAUDE_DIR, PROJECTS_DIR = default_claude_paths()

# Add backup directory roots here. Each entry should be the parent directory
# that contains slug-named subdirectories (same layout as ~/.claude/projects/).
# Example: r"D:\Backup\.claude-userdata\projects"
BACKUP_ROOTS = [
    # r"<path-to-backup>\projects",
]


def _find_missing(appdata_claude_dir, projects_dir):
    """Return [(meta_file, cli_sid, title, created_at)] for sessions with no live JSONL."""
    inventory = build_transcript_path_inventory(projects_dir)
    if not inventory.is_complete:
        return None, inventory
    jsonl_index = inventory.by_session_id
    out = []
    for _acct, _org, meta_dir in find_metadata_directories(appdata_claude_dir):
        for fname in sorted(os.listdir(meta_dir)):
            if not (fname.startswith("local_") and fname.endswith(".json")):
                continue
            fpath = os.path.join(meta_dir, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as fh:
                    d = json.load(fh)
            except Exception:
                continue
            cli = d.get("cliSessionId") or ""
            if not cli or cli in jsonl_index:
                continue
            out.append((fname, cli, (d.get("title") or "")[:60], int(d.get("createdAt") or 0)))
    return out, inventory


# Subdirectory names to skip during recursive default-location search.
# These are large or irrelevant trees that would slow the walk significantly.
_SKIP_DIRS = {
    "node_modules", ".git", "Photos", "Videos", "Music",
    ".venv", "__pycache__", "build", "dist", ".tox",
}

# Default cloud-sync roots to probe when --backup is not given and BACKUP_ROOTS
# is empty. Glob patterns are expanded at runtime.
_DEFAULT_LOCATIONS_PATTERNS = [
    os.path.join(os.path.expanduser("~"), "OneDrive"),
    os.path.join(os.path.expanduser("~"), "OneDrive - *"),
    os.path.join(os.path.expanduser("~"), "Dropbox"),
    os.path.join(os.path.expanduser("~"), "iCloudDrive"),
    os.path.join(os.path.expanduser("~"), "Box"),
    os.path.join(os.path.expanduser("~"), "Google Drive"),
]


def _expand_default_locations():
    """Return (pattern, resolved_paths) pairs for the default location patterns."""
    results = []
    for pattern in _DEFAULT_LOCATIONS_PATTERNS:
        if "*" in pattern:
            expanded = sorted(glob.glob(pattern))
        else:
            expanded = [pattern] if os.path.isdir(pattern) else []
        results.append((pattern, expanded))
    return results


def _search_backup(cli_sid, backup_roots):
    """Search backup root directories for <cli_sid>.jsonl (shallow, one level deep)."""
    fname = "{}.jsonl".format(cli_sid)
    matches = []
    for root in backup_roots:
        if not os.path.isdir(root):
            continue
        try:
            for entry in os.listdir(root):
                candidate = os.path.join(root, entry, fname)
                if os.path.isfile(candidate):
                    matches.append(candidate)
        except OSError:
            pass
    return matches


def _search_location_recursive(location_dir, dangling_sids, quiet=False):
    """
    Walk location_dir recursively, skipping _SKIP_DIRS.
    Print 'FOUND: <sid> at <path>' for any .jsonl whose stem is in dangling_sids.
    Returns the count of matches found.
    """
    found = 0
    target_sids = set(dangling_sids)
    for dirpath, dirnames, filenames in os.walk(location_dir, topdown=True):
        # Prune unwanted subtrees in-place so os.walk won't descend into them.
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in filenames:
            if not fname.endswith(".jsonl"):
                continue
            stem = fname[:-6]  # strip ".jsonl"
            if stem in target_sids:
                full_path = os.path.join(dirpath, fname)
                if not quiet:
                    print("FOUND: {} at {}".format(stem, full_path))
                found += 1
    return found


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    ap.add_argument("--quiet", action="store_true",
                    help="Print summary counts only.")
    ap.add_argument("--state", metavar="PATH", default=None,
                    help="Fixture state directory for testing.")
    ap.add_argument("--backup", metavar="PATH", default=None,
                    help="Single backup root to search (overrides BACKUP_ROOTS and "
                         "default location probing).")
    args = ap.parse_args()

    if args.state:
        state_abs = os.path.abspath(args.state)
        appdata_claude_dir = os.path.join(state_abs, "appdata", "Claude")
        projects_dir = os.path.join(state_abs, "projects")
        backup_roots = []
        use_defaults = False
    elif args.backup:
        appdata_claude_dir = APPDATA_CLAUDE_DIR
        projects_dir = PROJECTS_DIR
        backup_roots = [args.backup]
        use_defaults = False
    else:
        appdata_claude_dir = APPDATA_CLAUDE_DIR
        projects_dir = PROJECTS_DIR
        backup_roots = BACKUP_ROOTS
        use_defaults = not BACKUP_ROOTS  # probe defaults only when BACKUP_ROOTS is empty

    snapshot = build_snapshot(
        appdata_claude_dir, projects_dir,
        fixture_mode=(args.state is not None),
    )
    if snapshot["schema_version"] == "unrecognised":
        print("ERROR: state schema not recognised. Run diagnose.py and report the "
              "unsupported state to the maintainer.", file=sys.stderr)
        return 2

    targets, inventory = _find_missing(appdata_claude_dir, projects_dir)
    if not inventory.is_complete:
        print(
            "PARTIAL: transcript inventory is incomplete; "
            "missing-JSONL conclusions were suppressed."
        )
        print("Inventory errors: {}".format(
            ", ".join(error.code for error in inventory.errors)
        ))
        return 0
    print("Sessions with cliSessionId but no live JSONL: {}".format(len(targets)))
    print()

    if not targets:
        return 0

    # --- Explicit backup roots (--backup or BACKUP_ROOTS) ---
    in_backup = []
    not_found = []
    for meta_file, cli_sid, title, created_at in targets:
        hits = _search_backup(cli_sid, backup_roots)
        if hits:
            in_backup.append((meta_file, cli_sid, title, hits))
        else:
            not_found.append((meta_file, cli_sid, title))

    print("=== Recoverable from backup ({}) ===".format(len(in_backup)))
    if not backup_roots and not use_defaults:
        print("  (no backup roots configured -- edit BACKUP_ROOTS in this script or use --backup)")
    if not args.quiet:
        for meta_file, cli_sid, title, hits in in_backup:
            print("  {} {} {}".format(meta_file, cli_sid[:8], title))
            for h in hits:
                print("      -> {}".format(h))

    print()
    print("=== Not found in any backup ({}) ===".format(len(not_found)))
    if not use_defaults:
        print("  Try system shadow copies (Explorer > Previous Versions) for any")
        print("  listed here.")
        if args.backup and not_found:
            print("  For a full recovery checklist: docs/recovering-deleted-jsonls.md")
    if not args.quiet:
        for meta_file, cli_sid, title in not_found:
            print("  {} {} {}".format(meta_file, cli_sid[:8], title))

    print()
    print("Summary: {} recoverable from backup, {} not found.".format(
        len(in_backup), len(not_found)))

    # --- Default location probing (when no explicit roots configured) ---
    if use_defaults and not_found:
        dangling_sids = [cli_sid for _, cli_sid, _ in not_found]
        print()
        _probe_default_locations(dangling_sids, quiet=args.quiet)

    return 0


def _probe_default_locations(dangling_sids, quiet=False):
    """Probe default cloud-sync locations for any of the dangling session IDs."""
    location_pairs = _expand_default_locations()

    # Separate into found (dirs that exist) and not-found (patterns with no match).
    existing_dirs = []
    checked_patterns = []
    for pattern, resolved in location_pairs:
        checked_patterns.append(pattern)
        for d in resolved:
            if os.path.isdir(d):
                existing_dirs.append(d)

    if not existing_dirs:
        print("No default backup locations found. Locations checked:")
        if not quiet:
            for p in checked_patterns:
                print("  {}".format(p))
        print("See docs/recovering-deleted-jsonls.md for recovery options.")
        return

    total_found = 0
    if quiet:
        print("Searching {} default backup location(s) ...".format(len(existing_dirs)))
    for location_dir in existing_dirs:
        if not quiet:
            print("Searching {} ...".format(location_dir))
        count = _search_location_recursive(location_dir, dangling_sids, quiet=quiet)
        total_found += count
        if not quiet:
            print("  {} match(es) found.".format(count))

    if quiet:
        print("{} match(es) found.".format(total_found))

    print()
    if total_found == 0:
        print("No matches found in any probed location.")
        print("See docs/recovering-deleted-jsonls.md for more recovery options.")


if __name__ == "__main__":
    sys.exit(main())
