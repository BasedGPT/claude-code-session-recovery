"""
NOTE: This script handles recovery stubs created outside the worktree_shrink.py
toolkit (e.g. stubs created manually or by earlier recovery scripts). For stubs
created by worktree_shrink.py, quietness is applied automatically at shrink time.

Quiet `git status` inside every bare Claude Code worktree stub by setting up
sparse-checkout (sparseCheckout=true, sparseCheckoutCone=false, empty pattern
file) and marking every index entry skip-worktree.

Bare stubs (a directory containing only a .git pointer file, no working tree)
report every file in HEAD as a staged deletion without this treatment. This
causes a "large number of uncommitted changes" banner in Claude Code at session
start. The script applies the same quietness mechanism as worktree_shrink.py.

Usage:
  python tools/worktrees/backfill_recovery_stubs.py            # dry-run
  python tools/worktrees/backfill_recovery_stubs.py --apply    # mutate
  python tools/worktrees/backfill_recovery_stubs.py --root /path/to/worktrees --apply

Behaviour:
  - Idempotent: re-running on an already-quieted stub is a no-op.
  - Per-stub atomic: a failure on one stub does not affect others.
"""
import argparse
import os
import sys
from pathlib import Path

from worktree_lifecycle import SENTINEL_FILE, quiet_stub, stub_is_quieted

# --- Configuration ---
# WORKTREES_DIR: directory containing your worktree stubs.
# Default: .claude/worktrees/ relative to the current working directory.
# Override with --root or set CLAUDE_REPO_ROOT in the environment.
_REPO_ROOT = Path(os.environ.get("CLAUDE_REPO_ROOT", os.getcwd()))
WORKTREES_DIR = _REPO_ROOT / ".claude" / "worktrees"

SENTINEL_FILENAME = SENTINEL_FILE


def _classify_stub(path):
    """Return ('apply', None) | ('skip', reason)."""
    if not path.is_dir():
        return "skip", "not-a-directory"
    try:
        entries = sorted(p.name for p in path.iterdir())
    except OSError as exc:
        return "skip", "entry-listing-failed: {}".format(exc)

    if not entries:
        return "skip", "empty-dir (no .git pointer)"
    if ".git" not in entries:
        return "skip", "no-.git-pointer"

    # If the shrink sentinel is present, this stub was created by worktree_shrink.py
    # and was already quieted at creation time.
    if SENTINEL_FILENAME in entries:
        return "skip", "shrink-toolkit-stub (sentinel present)"

    extras = [e for e in entries if e != ".git"]
    if extras:
        return "skip", "not-bare-stub (extras: {})".format(", ".join(extras[:5]))

    return "apply", None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Mutate (default is dry-run).",
    )
    parser.add_argument(
        "--root",
        default=None,
        metavar="PATH",
        help="Worktrees root directory (default: .claude/worktrees/ under CLAUDE_REPO_ROOT or cwd).",
    )
    args = parser.parse_args()

    root = Path(args.root) if args.root else WORKTREES_DIR

    if not root.is_dir():
        print("ERROR: worktrees root does not exist: {}".format(root))
        sys.exit(2)

    print("Backfill recovery-stub quietness ({})".format(
        "APPLY" if args.apply else "DRY-RUN"
    ))
    print("  worktrees root: {}".format(root))
    print()

    excluded_top = {".shrink-quarantine", "_archive"}

    candidates = []
    not_candidates = []

    for entry in sorted(root.iterdir()):
        if entry.name in excluded_top or entry.name.startswith("."):
            continue
        if not entry.is_dir():
            continue
        action, reason = _classify_stub(entry)
        if action == "apply":
            candidates.append(entry)
        else:
            not_candidates.append((entry.name, reason))

    print("Found {} bare-stub candidate(s) ready for quietness.".format(len(candidates)))
    print("       {} dir(s) not candidates (other shapes).".format(len(not_candidates)))
    print()

    if not_candidates:
        print("Non-candidates (first 10):")
        for name, reason in not_candidates[:10]:
            print("  - {}: {}".format(name, reason))
        if len(not_candidates) > 10:
            print("  ... and {} more".format(len(not_candidates) - 10))
        print()

    real_candidates = []
    already_quieted = []
    for entry in candidates:
        if stub_is_quieted(str(entry), repo_root=str(_REPO_ROOT)):
            already_quieted.append(entry.name)
        else:
            real_candidates.append(entry)

    print("Of {} bare stubs:".format(len(candidates)))
    print("  - {} already quieted (no-op)".format(len(already_quieted)))
    print("  - {} to be quieted".format(len(real_candidates)))
    print()

    if not args.apply:
        print("Dry-run: no changes made. Re-run with --apply.")
        if real_candidates:
            print("First 10 stubs that would be quieted:")
            for entry in real_candidates[:10]:
                print("  - {}".format(entry.name))
        return

    successes = []
    failures = []
    for i, entry in enumerate(real_candidates, 1):
        print("[{}/{}] quieting {} ...".format(i, len(real_candidates), entry.name))
        ok = quiet_stub(str(entry), repo_root=str(_REPO_ROOT))
        if ok:
            successes.append(entry.name)
            print("  OK")
        else:
            failures.append(entry.name)
            print("  FAILED")

    print()
    print("Summary:")
    print("  candidates: {}".format(len(candidates)))
    print("  already quieted (no-op): {}".format(len(already_quieted)))
    print("  quieted this run: {}".format(len(successes)))
    print("  failed this run: {}".format(len(failures)))
    print("  non-candidates (other shapes): {}".format(len(not_candidates)))

    if failures:
        print()
        print("Failures (first 20):")
        for name in failures[:20]:
            print("  - {}".format(name))
        sys.exit(1)


if __name__ == "__main__":
    main()
