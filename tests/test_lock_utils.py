"""Regression tests for the scheduled-tool lock protocol."""

import errno
import os
import sys
import tempfile
import unittest
from unittest import mock


TOOLS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools")
SESSIONS = os.path.join(TOOLS, "sessions")
if SESSIONS not in sys.path:
    sys.path.insert(0, SESSIONS)

import lock_utils  # noqa: E402


class LockUtilsTests(unittest.TestCase):
    def test_lock_is_atomic_and_owner_can_release(self):
        with tempfile.TemporaryDirectory() as root:
            lock_path = os.path.join(root, "job.lock")
            lock_utils.acquire_lock(lock_path, "first")
            self.assertTrue(os.path.isdir(lock_path))

            with self.assertRaises(SystemExit):
                lock_utils.acquire_lock(lock_path, "second")

            lock_utils.release_lock(lock_path)
            self.assertFalse(os.path.exists(lock_path))

    def test_nonempty_directory_collision_is_treated_as_existing_lock(self):
        with tempfile.TemporaryDirectory() as root:
            lock_path = os.path.join(root, "job.lock")
            lock_utils.acquire_lock(lock_path, "first")

            original_rename = lock_utils.os.rename

            def raise_directory_collision(source, destination):
                if destination == lock_path:
                    raise OSError(errno.ENOTEMPTY, "Directory not empty")
                return original_rename(source, destination)

            with mock.patch.object(
                lock_utils.os, "rename", side_effect=raise_directory_collision
            ):
                with self.assertRaises(SystemExit):
                    lock_utils.acquire_lock(lock_path, "second")

            self.assertTrue(os.path.isdir(lock_path))
            lock_utils.release_lock(lock_path)

    def test_stale_lock_directory_is_reclaimed(self):
        with tempfile.TemporaryDirectory() as root:
            lock_path = os.path.join(root, "job.lock")
            os.makedirs(lock_path)
            with open(os.path.join(lock_path, "pid"), "w", encoding="utf-8") as handle:
                handle.write("99999999")

            lock_utils.acquire_lock(lock_path, "replacement")

            with open(os.path.join(lock_path, "pid"), encoding="utf-8") as handle:
                self.assertEqual(handle.read(), str(os.getpid()))
            lock_utils.release_lock(lock_path)

    def test_unreadable_lock_is_not_reclaimed(self):
        with tempfile.TemporaryDirectory() as root:
            lock_path = os.path.join(root, "job.lock")
            os.makedirs(lock_path)

            with self.assertRaises(SystemExit):
                lock_utils.acquire_lock(lock_path, "replacement")

            self.assertTrue(os.path.isdir(lock_path))
            lock_utils._remove_lock_tree(lock_path)

    def test_unverifiable_owner_is_not_reclaimed(self):
        with tempfile.TemporaryDirectory() as root:
            lock_path = os.path.join(root, "job.lock")
            os.makedirs(lock_path)
            with open(os.path.join(lock_path, "pid"), "w", encoding="utf-8") as handle:
                handle.write("12345")

            with mock.patch.object(lock_utils, "_pid_running", return_value=None):
                with self.assertRaises(SystemExit):
                    lock_utils.acquire_lock(lock_path, "replacement")

            self.assertTrue(os.path.isdir(lock_path))
            lock_utils._remove_lock_tree(lock_path)

    def test_release_does_not_remove_another_owner(self):
        with tempfile.TemporaryDirectory() as root:
            lock_path = os.path.join(root, "job.lock")
            lock_utils.acquire_lock(lock_path, "first")
            with open(os.path.join(lock_path, "pid"), "w", encoding="utf-8") as handle:
                handle.write("99999999")

            lock_utils.release_lock(lock_path)
            self.assertTrue(os.path.isdir(lock_path))
            lock_utils._remove_lock_tree(lock_path)


if __name__ == "__main__":
    unittest.main()
