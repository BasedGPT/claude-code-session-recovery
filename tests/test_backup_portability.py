"""Regression tests for recoverable cross-platform backup pruning."""

import os
import sys
import tempfile
import unittest
from unittest import mock


SESSIONS = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "tools", "sessions"
)
if SESSIONS not in sys.path:
    sys.path.insert(0, SESSIONS)

import backup_claude_state  # noqa: E402


class BackupPortabilityTests(unittest.TestCase):
    def test_darwin_pruning_moves_old_snapshot_to_user_trash(self):
        with tempfile.TemporaryDirectory() as root:
            snapshot = os.path.join(root, "2026-01-01.zip")
            trash = os.path.join(root, "Trash")
            with open(snapshot, "wb") as handle:
                handle.write(b"snapshot")

            with mock.patch.object(backup_claude_state.platform, "system", return_value="Darwin"), \
                    mock.patch.object(
                        backup_claude_state.os.path,
                        "expanduser",
                        return_value=trash,
                    ):
                backup_claude_state._recycle(snapshot)

            self.assertFalse(os.path.exists(snapshot))
            self.assertTrue(os.path.exists(os.path.join(trash, "2026-01-01.zip")))


if __name__ == "__main__":
    unittest.main()
