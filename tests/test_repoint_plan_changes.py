"""Unit tests for repoint_session_to_jsonl._plan_changes.

The critical invariant: _plan_changes must not write a new cwd that
encodes to a slug other than the directory the JSONL actually lives in.
The JSONL's first record can contain a stale cwd (e.g. a junction path
that has since been removed) -- naively trusting it produces metadata
that still points at the wrong slug after the fix.
"""
import os
import sys
import unittest

# Add tools/sessions to path for imports
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools"))
sys.path.insert(0, os.path.join(REPO, "tools", "sessions"))

from repoint_session_to_jsonl import _plan_changes  # noqa: E402


class TestPlanChanges(unittest.TestCase):
    def test_skip_when_jsonl_cwd_is_none(self):
        meta = {"cwd": "C:\\old"}
        new_meta, log = _plan_changes(meta, "C:\\old", None, "C--new")
        self.assertIsNone(new_meta)
        self.assertTrue(any("no cwd field" in line for line in log))

    def test_skip_when_jsonl_cwd_encodes_to_wrong_slug(self):
        """Pattern B regression: JSONL's recorded cwd is itself stale."""
        # JSONL lives at C--Users-Foo-Sync-Cowork, but the JSONL's first
        # record contains a since-removed junction path that encodes to a
        # different slug. The tool must skip with WARN, not write the
        # stale junction path.
        meta = {
            "cwd": "C:\\Users\\Foo\\Sync\\Cowork\\.claude\\worktrees\\name",
        }
        old_cwd = meta["cwd"]
        new_cwd = "C:\\Users\\Foo\\Sync\\Claude Cowork\\.claude\\worktrees\\name"
        actual_slug_dir = "C--Users-Foo-Sync-Cowork"
        new_meta, log = _plan_changes(meta, old_cwd, new_cwd, actual_slug_dir)
        self.assertIsNone(new_meta)
        self.assertTrue(any("WARN" in line for line in log))
        self.assertTrue(any("stale" in line.lower() for line in log))

    def test_apply_when_jsonl_cwd_encodes_to_actual_slug(self):
        """Healthy case: JSONL's cwd encodes to where the JSONL lives."""
        meta = {
            "cwd": "C:\\Users\\Foo\\Sync\\old-name",
            "originCwd": "C:\\Users\\Foo\\Sync\\old-name",
        }
        old_cwd = meta["cwd"]
        new_cwd = "C:\\Users\\Foo\\Sync\\new-name"
        actual_slug_dir = "C--Users-Foo-Sync-new-name"
        new_meta, log = _plan_changes(meta, old_cwd, new_cwd, actual_slug_dir)
        self.assertIsNotNone(new_meta)
        self.assertEqual(new_meta["cwd"], new_cwd)
        self.assertEqual(new_meta["originCwd"], new_cwd)

    def test_worktreepath_updated_only_when_matched_old_cwd(self):
        meta = {
            "cwd": "C:\\Users\\Foo\\Sync\\old-name",
            "worktreePath": "C:\\Users\\Foo\\Sync\\unrelated",
        }
        old_cwd = meta["cwd"]
        new_cwd = "C:\\Users\\Foo\\Sync\\new-name"
        actual_slug_dir = "C--Users-Foo-Sync-new-name"
        new_meta, log = _plan_changes(meta, old_cwd, new_cwd, actual_slug_dir)
        self.assertIsNotNone(new_meta)
        # worktreePath should NOT change because it didn't match old_cwd
        self.assertEqual(new_meta["worktreePath"], "C:\\Users\\Foo\\Sync\\unrelated")


def main():
    suite = unittest.TestLoader().loadTestsFromTestCase(TestPlanChanges)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
