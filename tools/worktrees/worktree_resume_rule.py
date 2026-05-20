"""
SessionStart hook: clear the .shrink-when-safe marker when a real session opens
the worktree that was queued for shrink.

Implements the maintainer's chosen resume rule. Not required by Claude Code.
Adopt only if this workflow matches what you want.

The rule: any session that opens a worktree flagged with .shrink-when-safe
removes the marker, unless it is explicitly running the shrink processor.
This makes "continue after done" a first-class path -- the human session wins
the race and the shrink queue processor skips this worktree on its next run.

Files read:
  - <cwd>/.shrink-when-safe  (read to confirm existence; deleted if found)

Files written:
  - Nothing if no marker is found.
  - <cwd>/.shrink-when-safe is deleted when found.

Usage (as a SessionStart hook in Claude Code settings):
  hooks:
    SessionStart:
      - python /path/to/worktree_resume_rule.py
"""
import os
import sys

MARKER = ".shrink-when-safe"


def main():
    cwd = os.getcwd()
    marker_path = os.path.join(cwd, MARKER)
    if not os.path.isfile(marker_path):
        return 0
    try:
        os.remove(marker_path)
        sys.stderr.write(
            "[worktree-resume-rule] Removed {} -- this session opened a worktree "
            "that was queued for shrink. Marker cleared; the shrink processor will "
            "skip it on its next run.\n".format(MARKER)
        )
    except OSError as e:
        sys.stderr.write("[worktree-resume-rule] Could not remove {}: {}\n".format(
            marker_path, e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
