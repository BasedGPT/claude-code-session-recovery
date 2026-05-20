"""
Search backup directories for JSONL transcripts missing from the live projects
directory.

For each metadata file whose cliSessionId points to a JSONL that no longer
exists under ~/.claude/projects/, this tool searches the configured backup
roots for a matching <cli-session-id>.jsonl file. Reports which sessions are
recoverable and which are gone entirely.

Recovery sources searched, in priority order:
  1. Each directory listed in BACKUP_ROOTS (edit the Configuration block below).
  2. Any slug directory under the live PROJECTS_DIR that holds the file at a
     different path than expected (slug mismatch).

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
  python tools/sessions/find_missing_jsonls_in_backup.py --state <fixture-state-path>
"""
import argparse
import json
import os
import sys

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _TOOLS_DIR)
try:
    from diagnose import build_snapshot, _find_meta_dirs, _build_jsonl_index
except ImportError as exc:
    print("ERROR: cannot import from diagnose.py: {}".format(exc))
    print("Run from the repo root: python tools/sessions/find_missing_jsonls_in_backup.py")
    sys.exit(1)

# --- Configuration ---
APPDATA_CLAUDE_DIR = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")), "Claude"
)
PROJECTS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "projects")

# Add backup directory roots here. Each entry should be the parent directory
# that contains slug-named subdirectories (same layout as ~/.claude/projects/).
# Example: r"D:\Backup\.claude-userdata\projects"
BACKUP_ROOTS = [
    # r"<path-to-backup>\projects",
]


def _verify_jsonl(path, expected_sid):
    """Return True if the first non-empty record in the JSONL has sessionId == expected_sid."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, TypeError):
                    continue
                sid = rec.get("sessionId") or ""
                return isinstance(sid, str) and sid == expected_sid
    except OSError:
        pass
    return False


def _find_missing(appdata_claude_dir, projects_dir):
    """Return [(meta_file, cli_sid, title, created_at)] for sessions with no live JSONL."""
    jsonl_index = _build_jsonl_index(projects_dir)
    out = []
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
            cli = d.get("cliSessionId") or ""
            if not cli or cli in jsonl_index:
                continue
            out.append((fname, cli, (d.get("title") or "")[:60], int(d.get("createdAt") or 0)))
    return out


def _search_backup(cli_sid, backup_roots):
    """Search backup root directories for <cli_sid>.jsonl."""
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


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    ap.add_argument("--quiet", action="store_true",
                    help="Print summary counts only.")
    ap.add_argument("--state", metavar="PATH", default=None,
                    help="Fixture state directory for testing.")
    args = ap.parse_args()

    if args.state:
        state_abs = os.path.abspath(args.state)
        appdata_claude_dir = os.path.join(state_abs, "appdata", "Claude")
        projects_dir = os.path.join(state_abs, "projects")
        backup_roots = []
    else:
        appdata_claude_dir = APPDATA_CLAUDE_DIR
        projects_dir = PROJECTS_DIR
        backup_roots = BACKUP_ROOTS

    snapshot = build_snapshot(
        appdata_claude_dir, projects_dir,
        fixture_mode=(args.state is not None),
    )
    if snapshot["schema_version"] == "unrecognised":
        print("ERROR: state schema not recognised. Run diagnose.py and report the "
              "unsupported state to the maintainer.", file=sys.stderr)
        return 2

    targets = _find_missing(appdata_claude_dir, projects_dir)
    print("Sessions with cliSessionId but no live JSONL: {}".format(len(targets)))
    print()

    if not targets:
        return 0

    in_backup = []
    not_found = []
    for meta_file, cli_sid, title, created_at in targets:
        hits = _search_backup(cli_sid, backup_roots)
        if hits:
            in_backup.append((meta_file, cli_sid, title, hits))
        else:
            not_found.append((meta_file, cli_sid, title))

    print("=== Recoverable from backup ({}) ===".format(len(in_backup)))
    if not BACKUP_ROOTS:
        print("  (no backup roots configured -- edit BACKUP_ROOTS in this script)")
    for meta_file, cli_sid, title, hits in in_backup:
        print("  {} {} {}".format(meta_file, cli_sid[:8], title))
        if not args.quiet:
            for h in hits:
                print("      -> {}".format(h))

    print()
    print("=== Not found in any backup ({}) ===".format(len(not_found)))
    print("  Try system shadow copies (Explorer > Previous Versions) for any")
    print("  listed here.")
    for meta_file, cli_sid, title in not_found:
        print("  {} {} {}".format(meta_file, cli_sid[:8], title))

    print()
    print("Summary: {} recoverable from backup, {} not found.".format(
        len(in_backup), len(not_found)))

    return 0


if __name__ == "__main__":
    sys.exit(main())
