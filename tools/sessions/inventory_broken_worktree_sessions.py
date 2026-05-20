"""
List session metadata files whose worktree cwd no longer exists on disk.

Reads every local_*.json in the AppData session-metadata directory. Filters
to entries whose cwd points at a worktrees/ path. For each, checks whether
that path exists on disk. Sessions whose cwd is absent are likely orphaned
by a worktree removal or rename.

Output goes to stdout: counts at the top, then active (non-archived) sessions
sorted by lastActivityAt descending, then archived sessions.

Files read:
  - %APPDATA%\\Claude\\claude-code-sessions\\<account-uuid>\\<org-uuid>\\local_*.json

Files written:
  - Nothing. Read-only.

Exit code:
  0  script ran successfully
  2  state schema not recognised

Usage:
  python tools/sessions/inventory_broken_worktree_sessions.py
  python tools/sessions/inventory_broken_worktree_sessions.py --state <fixture-state-path>
"""
import argparse
import datetime
import json
import os
import sys

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _TOOLS_DIR)
try:
    from diagnose import build_snapshot, _find_meta_dirs
except ImportError as exc:
    print("ERROR: cannot import from diagnose.py: {}".format(exc))
    print("Run from the repo root: python tools/sessions/inventory_broken_worktree_sessions.py")
    sys.exit(1)

# --- Configuration ---
APPDATA_CLAUDE_DIR = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")), "Claude"
)
PROJECTS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "projects")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    ap.add_argument("--state", metavar="PATH", default=None,
                    help="Fixture state directory for testing.")
    args = ap.parse_args()

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
    if snapshot["schema_version"] == "unrecognised":
        print("ERROR: state schema not recognised. Run diagnose.py and report the "
              "unsupported state to the maintainer.", file=sys.stderr)
        return 2

    rows = []
    parse_errors = []
    for _acct, _org, meta_dir in _find_meta_dirs(appdata_claude_dir):
        for fname in sorted(os.listdir(meta_dir)):
            if not (fname.startswith("local_") and fname.endswith(".json")):
                continue
            fpath = os.path.join(meta_dir, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as fh:
                    d = json.load(fh)
            except Exception as e:
                parse_errors.append((fname, "{}: {}".format(type(e).__name__, e)))
                continue

            cwd = d.get("cwd", "") or ""
            if "worktrees" not in cwd.lower():
                continue

            branch = d.get("branch", "") or ""
            title = d.get("title", "") or ""
            last = d.get("lastActivityAt", 0) or 0
            archived = bool(d.get("isArchived", False))
            cwd_exists = os.path.isdir(cwd)

            rows.append({
                "file": fname,
                "cwd": cwd,
                "branch": branch,
                "title": title,
                "last": last,
                "archived": archived,
                "exists": cwd_exists,
            })

    total = len(rows)
    absent = [r for r in rows if not r["exists"]]
    absent_active = [r for r in absent if not r["archived"]]
    absent_archived = [r for r in absent if r["archived"]]

    print("Total worktree-cwd metadata files:    {}".format(total))
    print("Worktree cwd absent on disk:          {}".format(len(absent)))
    print("  non-archived (likely picker-visible): {}".format(len(absent_active)))
    print("  archived:                             {}".format(len(absent_archived)))

    def fmt_ts(ms):
        if not ms:
            return "<none>"
        return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")

    absent_active.sort(key=lambda r: r["last"], reverse=True)
    if absent_active:
        print()
        print("=== Non-archived sessions with worktree cwd absent ===")
        print("{:>3}  {:<25}  {:<40} {}".format("#", "lastActivityAt", "branch", "title"))
        for i, r in enumerate(absent_active, 1):
            print("{:>3}  {:<25}  {:<40} {}".format(
                i, fmt_ts(r["last"]), r["branch"][:40], r["title"][:60]))

    absent_archived.sort(key=lambda r: r["last"], reverse=True)
    if absent_archived:
        print()
        print("=== Archived sessions with worktree cwd absent (top 20) ===")
        for i, r in enumerate(absent_archived[:20], 1):
            print("{:>3}  {:<25}  {:<40} {}".format(
                i, fmt_ts(r["last"]), r["branch"][:40], r["title"][:60]))

    if parse_errors:
        print()
        print("=== {} metadata files failed to parse ===".format(len(parse_errors)))
        for fname, err in parse_errors[:20]:
            print("  {:<40} {}".format(fname[:40], err))

    return 0


if __name__ == "__main__":
    sys.exit(main())
