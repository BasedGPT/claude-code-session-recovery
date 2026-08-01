"""
Find Claude Desktop sessions where branch=claude/* but cwd is a bare project root.

These sessions were started from the repository root before a worktree was set up
(or after a worktree was removed). Desktop lists them under the bare project path
rather than under a named worktree, which can make them hard to find. They are
otherwise healthy.

Files read:
  - %APPDATA%\\Claude\\claude-code-sessions\\<account-uuid>\\<org-uuid>\\local_*.json

Files written:
  - Nothing. Read-only.

Exit code:
  0  script ran successfully
  2  state schema not recognised

Configuration:
  Set REPO_ROOTS in the Configuration block below to the set of bare paths
  you want to check (lower-cased, backslash-separated). Sessions whose cwd
  matches one of these roots AND whose branch starts with "claude/" appear
  in the report.

Usage:
  python tools/sessions/audit_root_cwd_sessions.py
  python tools/sessions/audit_root_cwd_sessions.py --state <fixture-state-path>
"""
import argparse
import datetime
import json
import os
import sys

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _TOOLS_DIR)
try:
    from session_state import build_snapshot, default_claude_paths, find_metadata_directories
except ImportError as exc:
    print("ERROR: cannot import from diagnose.py: {}".format(exc))
    print("Run from the repo root: python tools/sessions/audit_root_cwd_sessions.py")
    sys.exit(1)

# --- Configuration ---
APPDATA_CLAUDE_DIR, _PROJECTS_DIR = default_claude_paths()

# Add your repository root(s) here. Paths are normalised with the current
# platform's path rules before comparison.
REPO_ROOTS = {
    # r"C:\Users\you\projects\my-repo",
}


def _normalise_path(path):
    """Normalise a configured or recorded path for this operating system."""
    return os.path.normcase(os.path.normpath(os.path.expanduser(path)))


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
        projects_dir = os.path.join(os.path.expanduser("~"), ".claude", "projects")

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
    for _acct, _org, meta_dir in find_metadata_directories(appdata_claude_dir):
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
            branch = d.get("branch", "") or ""
            if not branch.startswith("claude/"):
                continue

            cwd_norm = _normalise_path(cwd)
            configured_roots = {_normalise_path(root) for root in REPO_ROOTS}
            if configured_roots and cwd_norm not in configured_roots:
                continue

            rows.append({
                "file": fname,
                "cwd": cwd,
                "branch": branch,
                "title": d.get("title", "") or "",
                "archived": bool(d.get("isArchived", False)),
                "last": d.get("lastActivityAt", 0) or 0,
            })

    rows.sort(key=lambda r: r["last"], reverse=True)
    active = [r for r in rows if not r["archived"]]
    archived_rows = [r for r in rows if r["archived"]]

    print("Sessions with branch=claude/* and cwd at a configured root: {}".format(len(rows)))
    print("  non-archived (picker-visible): {}".format(len(active)))
    print("  archived:                      {}".format(len(archived_rows)))

    if not REPO_ROOTS:
        print()
        print("NOTE: REPO_ROOTS is empty. Edit the Configuration block in this script to")
        print("set the project root path(s) you want to audit.")
        return 0

    def fmt_ts(ms):
        if not ms:
            return "<none>"
        return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")

    if active:
        print()
        print("=== Non-archived sessions with branch=claude/* at bare repo root ===")
        print("{:>3}  {:<19}  {:<45} {}".format("#", "lastActivityAt", "branch", "title"))
        for i, r in enumerate(active, 1):
            print("{:>3}  {:<19}  {:<45} {}".format(
                i, fmt_ts(r["last"]), r["branch"][:45], r["title"][:60]))

    if archived_rows:
        print()
        print("=== Archived sessions in same state (top 20) ===")
        for i, r in enumerate(archived_rows[:20], 1):
            print("{:>3}  {:<19}  {:<45} {}".format(
                i, fmt_ts(r["last"]), r["branch"][:45], r["title"][:60]))

    if parse_errors:
        print()
        print("=== {} metadata files failed to parse ===".format(len(parse_errors)))
        for fname, err in parse_errors[:20]:
            print("  {:<40} {}".format(fname[:40], err))

    return 0


if __name__ == "__main__":
    sys.exit(main())
