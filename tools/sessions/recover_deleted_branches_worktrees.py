"""
Invoked via diagnose.py. Not intended for direct invocation.
To diagnose your state: python tools/diagnose.py

Recreate deleted chip branches and re-register stub worktrees for Claude Desktop
session metadata entries whose worktree cwd no longer exists on disk.

Two-phase, all idempotent:

  Phase 1: Branch recovery
    For each branch name in the supplied list, if the branch does not currently
    exist, recreate it. The target SHA is parent[1] of the merge commit on the
    target branch whose subject mentions "claude/<name>" (when one exists),
    otherwise falls back to HEAD of the target branch. The session content lives
    in the JSONL transcript, not in git state -- Desktop only needs the branch to
    exist for the session picker to resume.

  Phase 2: Worktree stub registration
    For each unique cwd among broken (non-existent on disk) Desktop session
    metadata files, if no worktree is currently registered at that path and the
    path doesn't exist on disk, register a stub:
        git worktree add --no-checkout <path> claude/<name>
    Skipped when the branch wasn't recreated or when the path collides with an
    existing directory.

Files read:
  - <branch-list-file> (one branch name per line, with or without claude/ prefix)
  - %APPDATA%\\Claude\\claude-code-sessions\\<account-uuid>\\<org-uuid>\\local_*.json

Files written:
  - git branch refs (with --apply only)
  - git worktree registrations (with --apply only)

Backup created at:
  - (git operations are reversible; no separate backup file)

Rollback:
  - This script creates only branches and worktree registrations. To undo,
    use the branch and worktree removal commands appropriate to your workflow.
    Note: the maintainer's chosen workflow preserves recovered branches rather
    than removing them, since branches are the cheapest possible record of
    past session work. See docs/worktree-lifecycle.md for that reasoning.

Usage:
    python tools/sessions/recover_deleted_branches_worktrees.py --branch-list branches.txt
    python tools/sessions/recover_deleted_branches_worktrees.py --branch-list branches.txt --apply
    python tools/sessions/recover_deleted_branches_worktrees.py --branch-list branches.txt --apply --limit 10
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Import shared helpers from tools/diagnose.py (parent directory).
_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _TOOLS_DIR)
try:
    from session_state import find_metadata_directories
    from mutator_safety import (
        current_snapshot_and_diagnosis_id, diagnosis_mode, resolve_state_paths,
    )
except ImportError as exc:
    print("ERROR: cannot import from diagnose.py: {}".format(exc))
    print("Run from the repo root: python tools/sessions/recover_deleted_branches_worktrees.py")
    sys.exit(1)

# --- Configuration ---
# REPO_ROOT: absolute path to the git repository root.
# Change this to the root of the repository you are recovering worktrees for.
REPO_ROOT = os.getcwd()

# APPDATA_CLAUDE_DIR: used in live mode (not --state).
APPDATA_CLAUDE_DIR = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")), "Claude"
)


# ---------------------------------------------------------------------------
# Gate 5 -- Known-do-not-run conditions
# ---------------------------------------------------------------------------

KNOWN_DO_NOT_RUN = [
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

def _run(cmd, check=True, cwd=None):
    if cwd is None:
        cwd = REPO_ROOT
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=cwd, check=check,
    )


def _load_branch_names(path):
    """Return short names (no claude/ prefix), in file order."""
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            name = line.strip()
            if not name:
                continue
            if name.startswith("claude/"):
                name = name[len("claude/"):]
            out.append(name)
    return out


def _load_merge_sha_map():
    """Map short-name -> original branch tip SHA from merge commits on HEAD."""
    out = {}
    try:
        r = _run(["git", "log", "--merges", "--pretty=format:%H|%P|%s"])
    except subprocess.CalledProcessError:
        return out
    pat = re.compile(r"claude/([\w\-]+)")
    for line in r.stdout.splitlines():
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        _commit, parents, subject = parts
        m = pat.search(subject)
        if not m:
            continue
        name = m.group(1)
        parent_ids = parents.split()
        if len(parent_ids) >= 2:
            out[name] = parent_ids[1]
    return out


def _existing_branches():
    r = _run(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/"])
    return set(line.strip() for line in r.stdout.splitlines() if line.strip())


def _registered_worktrees():
    """Map normalised path (lower, forward slashes) -> branch."""
    out = {}
    try:
        r = _run(["git", "worktree", "list", "--porcelain"])
    except subprocess.CalledProcessError:
        return out
    cur_path = None
    cur_branch = None
    for line in r.stdout.splitlines():
        if line.startswith("worktree "):
            cur_path = line[len("worktree "):].strip()
        elif line.startswith("branch "):
            cur_branch = line[len("branch "):].strip()
            if cur_path:
                out[cur_path.replace("\\", "/").lower()] = cur_branch
                cur_path = None
                cur_branch = None
        elif line == "":
            cur_path = None
            cur_branch = None
    return out


def _head_sha():
    return _run(["git", "rev-parse", "HEAD"]).stdout.strip()


def _sha_exists(sha):
    try:
        r = _run(["git", "cat-file", "-e", sha], check=False)
        return r.returncode == 0
    except Exception:
        return False


def _gather_broken_sessions(appdata_claude_dir):
    """Return one dict per metadata entry whose cwd contains 'worktrees' and
    does not exist on disk."""
    rows = []
    for _acct, _org, meta_dir in find_metadata_directories(appdata_claude_dir):
        import glob as _glob
        for p in sorted(_glob.glob(os.path.join(meta_dir, "local_*.json"))):
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                continue
            cwd = data.get("cwd", "") or ""
            if "worktrees" not in cwd.lower():
                continue
            if os.path.isdir(cwd):
                continue
            branch = data.get("branch", "") or ""
            name = branch.replace("claude/", "") if branch.startswith("claude/") else ""
            rows.append({
                "meta_file": os.path.basename(p),
                "cwd": cwd,
                "branch": branch,
                "name": name,
                "title": (data.get("title", "") or "")[:60],
                "last": data.get("lastActivityAt", 0) or 0,
            })
    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--branch-list",
        metavar="FILE",
        required=True,
        dest="branch_list",
        help="Text file with one branch name per line (with or without claude/ prefix).",
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
        help="Actually run. Default is dry-run.",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=0,
        metavar="N",
        help="Cap the number of actions per phase (0 = no cap).",
    )
    ap.add_argument(
        "--repo-root",
        metavar="PATH",
        default=None,
        dest="repo_root",
        help="Git repository root (default: current working directory).",
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

    global REPO_ROOT
    if args.repo_root:
        REPO_ROOT = os.path.abspath(args.repo_root)

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
                print("REFUSED: " + message)
                sys.exit(3)
        except Exception:
            pass

    mode_label = "APPLY" if args.apply else "DRY-RUN"
    print("=== Recovery run: {} ===".format(mode_label))

    deleted = _load_branch_names(args.branch_list)
    print("Loaded {} branch name(s) from {}.".format(len(deleted), args.branch_list))

    sha_map = _load_merge_sha_map()
    print("Found {} branch(es) with reconstructable original SHAs.".format(len(sha_map)))

    head = _head_sha()
    print("HEAD = {}.".format(head[:10]))

    existing = _existing_branches()
    print("Existing branches: {}.".format(len(existing)))

    # ----- Phase 1: branch recovery -----
    print("--- Phase 1: branch recovery ---")
    phase1_actions = 0
    phase1_skips = 0
    phase1_original_sha = 0
    phase1_fallback = 0
    for name in deleted:
        ref = "claude/{}".format(name)
        if ref in existing:
            phase1_skips += 1
            continue
        sha = sha_map.get(name)
        sha_kind = "original-tip"
        if sha is None or not _sha_exists(sha):
            sha = head
            sha_kind = "HEAD-fallback"
        if sha_kind == "original-tip":
            phase1_original_sha += 1
        else:
            phase1_fallback += 1
        if args.apply:
            if args.limit and phase1_actions >= args.limit:
                print("  LIMIT REACHED at {} branch creation(s); stopping phase 1.".format(args.limit))
                break
            r = _run(["git", "branch", ref, sha], check=False)
            if r.returncode == 0:
                print("  CREATE {} -> {} ({})".format(ref, sha[:10], sha_kind))
            else:
                print("  FAIL   {} -> {}: {}".format(ref, sha[:10], r.stderr.strip()))
        else:
            print("  DRY-RUN: would create {} -> {} ({})".format(ref, sha[:10], sha_kind))
        phase1_actions += 1

    print("Phase 1: actions={} skipped_exists={} original_sha={} fallback={}".format(
        phase1_actions, phase1_skips, phase1_original_sha, phase1_fallback))

    # ----- Phase 2: worktree registration -----
    print("--- Phase 2: worktree stub registration ---")
    sessions = _gather_broken_sessions(appdata_claude_dir)
    print("Broken sessions referencing missing worktree cwds: {}.".format(len(sessions)))

    by_cwd: dict = {}
    for s in sessions:
        key = s["cwd"].replace("\\", "/").lower().rstrip("/")
        if not key:
            continue
        prev = by_cwd.get(key)
        if prev is None or s["last"] > prev["last"]:
            by_cwd[key] = s
    print("Unique broken cwd paths: {}.".format(len(by_cwd)))

    if args.apply:
        existing = _existing_branches()
    else:
        existing = existing | {"claude/{}".format(n) for n in deleted}
    registered = _registered_worktrees()

    phase2_actions = 0
    phase2_skip_no_branch = 0
    phase2_skip_path_exists = 0
    phase2_skip_registered = 0
    phase2_fail = 0
    for key, s in sorted(by_cwd.items(), key=lambda kv: -kv[1]["last"]):
        cwd = s["cwd"]
        name = s["name"]
        ref = "claude/{}".format(name) if name else ""

        if not ref or ref not in existing:
            phase2_skip_no_branch += 1
            print("  SKIP no branch for: {} (branch={})".format(cwd, ref))
            continue
        if key in registered:
            phase2_skip_registered += 1
            continue
        if os.path.isdir(cwd):
            phase2_skip_path_exists += 1
            print("  SKIP path exists: {}".format(cwd))
            continue

        if args.apply:
            if args.limit and phase2_actions >= args.limit:
                print("  LIMIT REACHED at {} worktree add(s); stopping phase 2.".format(args.limit))
                break
            r = _run(["git", "worktree", "add", "--no-checkout", cwd, ref], check=False)
            if r.returncode == 0:
                print("  ADD  {} -> {} ({!r})".format(cwd, ref, s["title"][:40]))
            else:
                phase2_fail += 1
                print("  FAIL {} -> {}: {}".format(cwd, ref, r.stderr.strip()))
        else:
            print("  DRY-RUN: would add {} -> {} ({!r})".format(cwd, ref, s["title"][:40]))
        phase2_actions += 1

    print("Phase 2: actions={} no_branch={} registered={} path_exists={} failures={}".format(
        phase2_actions, phase2_skip_no_branch, phase2_skip_registered,
        phase2_skip_path_exists, phase2_fail))

    print("=== Recovery run finished: {} ===".format(mode_label))


if __name__ == "__main__":
    main()
