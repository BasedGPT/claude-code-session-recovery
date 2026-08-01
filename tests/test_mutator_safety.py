"""Adversarial checks for the shared mutation safety mechanics."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


TOOLS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools")
REPO_ROOT = os.path.dirname(os.path.dirname(__file__))
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import mutator_safety  # noqa: E402


class MutatorSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = self.temp_dir.name

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_diagnosis_mode_requires_token_and_rejects_force_apply(self):
        self.assertEqual(mutator_safety.diagnosis_mode(None, None, False), (False, "missing"))
        self.assertEqual(
            mutator_safety.diagnosis_mode(None, "audit-only", True),
            (True, "force_apply"),
        )
        self.assertEqual(
            mutator_safety.diagnosis_mode(None, "audit-only", False),
            (True, None),
        )

    def test_repair_cli_refuses_an_injected_stale_token(self):
        result = subprocess.run(
            [
                sys.executable,
                os.path.join(TOOLS, "sessions", "repair_session_metadata.py"),
                "--state",
                os.path.join(REPO_ROOT, "fixtures", "02-blank-pane-missing-cli", "state"),
                "--diagnosis-id",
                "deadbeef",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("ERROR: Diagnosis token mismatch.", result.stdout)
        self.assertIn("Supplied : deadbeef", result.stdout)

    def test_repair_cli_refuses_injected_force_apply_combination(self):
        result = subprocess.run(
            [
                sys.executable,
                os.path.join(TOOLS, "sessions", "repair_session_metadata.py"),
                "--state",
                os.path.join(REPO_ROOT, "fixtures", "02-blank-pane-missing-cli", "state"),
                "--force-with-diagnosis-id",
                "audit-only",
                "--apply",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(
            result.stdout,
            "ERROR: --apply cannot be combined with --force-with-diagnosis-id=audit-only.\n",
        )

    def test_fixture_state_paths_override_live_paths(self):
        appdata, projects = mutator_safety.resolve_state_paths(
            self.root, "live-appdata", "live-projects"
        )
        self.assertEqual(appdata, os.path.join(os.path.abspath(self.root), "appdata", "Claude"))
        self.assertEqual(projects, os.path.join(os.path.abspath(self.root), "projects"))

    def test_metadata_backup_path_preserves_account_and_organisation(self):
        appdata = os.path.join(self.root, "appdata", "Claude")
        source = os.path.join(
            appdata,
            "claude-code-sessions",
            "account-a",
            "organisation-a",
            "local_same-name.json",
        )
        backup_dir = os.path.join(self.root, "repair-backup")

        self.assertEqual(
            mutator_safety.metadata_backup_path(source, appdata, backup_dir),
            os.path.join(
                backup_dir,
                "account-a",
                "organisation-a",
                "local_same-name.json",
            ),
        )

    def test_verified_backup_refuses_copy_failure_before_any_write(self):
        source = os.path.join(self.root, "source.json")
        backup = os.path.join(self.root, "backup.json")
        with open(source, "wb") as handle:
            handle.write(b"original")

        with mock.patch.object(mutator_safety.shutil, "copy2", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                mutator_safety.verified_backup(source, backup)

        with open(source, "rb") as handle:
            self.assertEqual(handle.read(), b"original")
        self.assertFalse(os.path.exists(backup))
        self.assertEqual(
            [name for name in os.listdir(self.root) if name.endswith(".tmp")], []
        )

    def test_verified_backup_rejects_a_partial_copy(self):
        source = os.path.join(self.root, "source.json")
        backup = os.path.join(self.root, "backup.json")
        with open(source, "wb") as handle:
            handle.write(b"original")

        def partial_copy(_source, destination):
            with open(destination, "wb") as handle:
                handle.write(b"partial")
            return destination

        with mock.patch.object(mutator_safety.shutil, "copy2", side_effect=partial_copy):
            with self.assertRaisesRegex(OSError, "backup verification failed"):
                mutator_safety.verified_backup(source, backup)

        self.assertFalse(os.path.exists(backup))
        self.assertEqual(
            [name for name in os.listdir(self.root) if name.endswith(".tmp")], []
        )

    def test_verified_backup_publishes_only_after_bytes_are_verified(self):
        source = os.path.join(self.root, "source.json")
        backup = os.path.join(self.root, "backup.json")
        with open(source, "wb") as handle:
            handle.write(b"original")

        result = mutator_safety.verified_backup(source, backup)

        self.assertEqual(result, backup)
        with open(backup, "rb") as handle:
            self.assertEqual(handle.read(), b"original")
        self.assertEqual(
            [name for name in os.listdir(self.root) if name.endswith(".tmp")], []
        )

    def test_verified_backup_keeps_existing_final_when_publish_fails(self):
        source = os.path.join(self.root, "source.json")
        backup = os.path.join(self.root, "backup.json")
        with open(source, "wb") as handle:
            handle.write(b"original")
        with open(backup, "wb") as handle:
            handle.write(b"previous")

        with mock.patch.object(mutator_safety.os, "replace", side_effect=OSError("locked")):
            with self.assertRaisesRegex(OSError, "locked"):
                mutator_safety.verified_backup(source, backup)

        with open(backup, "rb") as handle:
            self.assertEqual(handle.read(), b"previous")
        self.assertEqual(
            [name for name in os.listdir(self.root) if name.endswith(".tmp")], []
        )

    def test_in_place_json_write_preserves_existing_file_identity(self):
        destination = os.path.join(self.root, "metadata.json")
        with open(destination, "wb") as handle:
            handle.write(b'{"old": true}')
        before = os.stat(destination)

        mutator_safety.write_json_in_place(destination, {"new": True})

        after = os.stat(destination)
        self.assertEqual(after.st_ino, before.st_ino)
        with open(destination, "rb") as handle:
            expected = ("{\n  \"new\": true\n}").replace("\n", os.linesep).encode("utf-8")
            self.assertEqual(handle.read(), expected)

    def test_atomic_json_write_keeps_original_when_replace_fails(self):
        destination = os.path.join(self.root, "metadata.json")
        with open(destination, "wb") as handle:
            handle.write(b'{"old": true}')

        with mock.patch.object(mutator_safety.os, "replace", side_effect=OSError("locked")):
            with self.assertRaisesRegex(OSError, "locked"):
                mutator_safety.atomic_write_json(destination, {"new": True})

        with open(destination, "rb") as handle:
            self.assertEqual(handle.read(), b'{"old": true}')
        self.assertEqual(
            [name for name in os.listdir(self.root) if name.endswith(".tmp")], []
        )

    def test_atomic_copy_keeps_original_when_replace_fails(self):
        source = os.path.join(self.root, "source.json")
        destination = os.path.join(self.root, "metadata.json")
        with open(source, "wb") as handle:
            handle.write(json.dumps({"new": True}).encode("utf-8"))
        with open(destination, "wb") as handle:
            handle.write(b'{"old": true}')

        with mock.patch.object(mutator_safety.os, "replace", side_effect=OSError("locked")):
            with self.assertRaisesRegex(OSError, "locked"):
                mutator_safety.atomic_copy_file(source, destination)

        with open(destination, "rb") as handle:
            self.assertEqual(handle.read(), b'{"old": true}')
        self.assertEqual(
            [name for name in os.listdir(self.root) if name.endswith(".tmp")], []
        )

    def test_desktop_probe_fails_closed_when_windows_probe_is_unavailable(self):
        with mock.patch("platform_support.platform.system", return_value="Windows"), \
                mock.patch("platform_support.subprocess.run", side_effect=FileNotFoundError()):
            self.assertTrue(mutator_safety.desktop_process_running())


if __name__ == "__main__":
    unittest.main()
