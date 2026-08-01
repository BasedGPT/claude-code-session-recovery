"""Regression tests for worktree base-branch and process portability."""

import os
import sys
import unittest
from unittest import mock


WORKTREES = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "tools", "worktrees"
)
if WORKTREES not in sys.path:
    sys.path.insert(0, WORKTREES)

import worktree_shrink  # noqa: E402


class WorktreePortabilityTests(unittest.TestCase):
    def test_branch_checks_use_origin_default_branch(self):
        calls = []

        def fake_git(*args, **_kwargs):
            calls.append(args)
            if args[:3] == ("symbolic-ref", "--quiet", "--short"):
                return 0, "origin/main\n", ""
            if args[0] == "branch":
                return 0, "  feature\n", ""
            if args[0] == "cherry":
                return 0, "", ""
            if args[0] == "log":
                return 0, "", ""
            return 1, "", "unsupported probe"

        with mock.patch.object(worktree_shrink, "git", side_effect=fake_git):
            self.assertTrue(worktree_shrink.branch_is_merged("feature"))
            self.assertTrue(worktree_shrink.branch_squash_equivalent("feature"))
            self.assertEqual(worktree_shrink.unmerged_commit_subjects("feature"), [])

        self.assertIn(("branch", "--merged", "origin/main", "--list", "feature"), calls)
        self.assertIn(("cherry", "origin/main", "feature"), calls)
        self.assertIn(("log", "--oneline", "origin/main..feature"), calls)


if __name__ == "__main__":
    unittest.main()
