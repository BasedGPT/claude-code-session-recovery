"""Regression tests for recoverable cross-platform backup pruning."""

import hashlib
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


SESSIONS = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "tools", "sessions"
)
if SESSIONS not in sys.path:
    sys.path.insert(0, SESSIONS)

import backup_claude_state  # noqa: E402


class BackupPortabilityTests(unittest.TestCase):
    def test_metadata_pair_discovery_handles_zero_pairs(self):
        with tempfile.TemporaryDirectory() as root:
            destination = os.path.join(root, "metadata.zip")
            valid_sessions = os.path.join(root, "valid-sessions")
            valid_metadata = os.path.join(valid_sessions, "account-a", "organisation-a")
            os.makedirs(valid_metadata)
            with open(os.path.join(valid_metadata, "local_one.json"), "wb") as handle:
                handle.write(b"valid\n")
            with mock.patch.object(backup_claude_state, "SESSIONS_BASE", valid_sessions):
                valid_pairs = backup_claude_state._discover_meta_pairs()
                backup_claude_state._backup_zip(
                    valid_sessions,
                    destination,
                    lambda _message: None,
                    False,
                    pairs=valid_pairs,
                    source_layer="desktop-metadata",
                )
            with open(destination, "rb") as handle:
                prior_backup = handle.read()

            empty_sessions = os.path.join(root, "empty-sessions")
            os.makedirs(empty_sessions)
            with mock.patch.object(backup_claude_state, "SESSIONS_BASE", empty_sessions):
                pairs = backup_claude_state._discover_meta_pairs()
                self.assertEqual(pairs, [])
                with self.assertRaisesRegex(RuntimeError, "No account/organisation"):
                    backup_claude_state._backup_zip(
                        empty_sessions,
                        destination,
                        lambda _message: None,
                        False,
                        pairs=pairs,
                        source_layer="desktop-metadata",
                    )

            with open(destination, "rb") as handle:
                self.assertEqual(handle.read(), prior_backup)
            self.assertFalse(os.path.exists(destination + ".tmp"))

    def test_metadata_pair_discovery_preserves_single_pair_compatibility(self):
        with tempfile.TemporaryDirectory() as root:
            sessions = os.path.join(root, "claude-code-sessions")
            metadata = os.path.join(sessions, "account-a", "organisation-a")
            os.makedirs(metadata)
            with mock.patch.object(backup_claude_state, "SESSIONS_BASE", sessions):
                pairs = backup_claude_state._discover_meta_pairs()
                self.assertEqual(len(pairs), 1)
                self.assertEqual(backup_claude_state._discover_meta_dir(), metadata)

    def test_metadata_backup_manifest_keeps_multiple_pairs_and_hashes_files(self):
        with tempfile.TemporaryDirectory() as root:
            sessions = os.path.join(root, "claude-code-sessions")
            for account, organisation, content in (
                ("account-a", "organisation-a", b"old\n"),
                ("account-b", "organisation-b", b"new\n"),
            ):
                metadata = os.path.join(sessions, account, organisation)
                os.makedirs(metadata)
                with open(os.path.join(metadata, "local_same.json"), "wb") as handle:
                    handle.write(content)

            destination = os.path.join(root, "metadata.zip")
            messages = []
            with mock.patch.object(backup_claude_state, "SESSIONS_BASE", sessions):
                pairs = backup_claude_state._discover_meta_pairs()
                source_bytes = backup_claude_state._backup_zip(
                    sessions,
                    destination,
                    messages.append,
                    False,
                    pairs=pairs,
                    source_layer="desktop-metadata",
                )

            self.assertEqual(source_bytes, len(b"old\n") + len(b"new\n"))
            with zipfile.ZipFile(destination) as archive:
                self.assertEqual(
                    set(archive.namelist()),
                    {
                        "account-a/organisation-a/local_same.json",
                        "account-b/organisation-b/local_same.json",
                        "manifest.json",
                    },
                )
                manifest = json.loads(archive.read("manifest.json"))

            self.assertEqual(manifest["layout_version"], 2)
            self.assertEqual(manifest["source_layer"], "desktop-metadata")
            self.assertEqual(
                [(p["account_uuid"], p["organisation_uuid"]) for p in manifest["pairs"]],
                [("account-a", "organisation-a"), ("account-b", "organisation-b")],
            )
            self.assertEqual(len(manifest["files"]), 2)
            for entry in manifest["files"]:
                with zipfile.ZipFile(destination) as archive:
                    content = archive.read(entry["archive_path"])
                self.assertEqual(entry["size"], len(content))
                self.assertEqual(
                    entry["sha256"], hashlib.sha256(content).hexdigest()
                )

    def test_verification_failure_preserves_existing_final(self):
        with tempfile.TemporaryDirectory() as root:
            sessions = os.path.join(root, "claude-code-sessions")
            metadata = os.path.join(sessions, "account-a", "organisation-a")
            os.makedirs(metadata)
            source = os.path.join(metadata, "local_one.json")
            with open(source, "wb") as handle:
                handle.write(b"first\n")
            destination = os.path.join(root, "metadata.zip")
            with mock.patch.object(backup_claude_state, "SESSIONS_BASE", sessions):
                pairs = backup_claude_state._discover_meta_pairs()
                backup_claude_state._backup_zip(
                    sessions, destination, lambda _message: None, False,
                    pairs=pairs, source_layer="desktop-metadata",
                )
                with open(destination, "rb") as handle:
                    prior_backup = handle.read()
                with open(source, "wb") as handle:
                    handle.write(b"second\n")
                with mock.patch.object(
                    backup_claude_state,
                    "_verify_backup_zip",
                    side_effect=RuntimeError("injected verification failure"),
                ):
                    with self.assertRaisesRegex(RuntimeError, "injected verification failure"):
                        backup_claude_state._backup_zip(
                            sessions, destination, lambda _message: None, False,
                            pairs=pairs, source_layer="desktop-metadata",
                        )

            with open(destination, "rb") as handle:
                self.assertEqual(handle.read(), prior_backup)
            self.assertFalse(os.path.exists(destination + ".tmp"))

    def test_verifier_rejects_declared_sha256_mismatch(self):
        with tempfile.TemporaryDirectory() as root:
            archive_path = os.path.join(root, "tampered.zip")
            content = b"intact CRC but wrong declared hash\n"
            manifest = {
                "layout_version": backup_claude_state.BACKUP_LAYOUT_VERSION,
                "source_layer": "jsonl-projects",
                "pairs": [],
                "files": [
                    {
                        "archive_path": "one.jsonl",
                        "size": len(content),
                        "sha256": "0" * 64,
                    }
                ],
            }
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("one.jsonl", content)
                archive.writestr("manifest.json", json.dumps(manifest))

            with self.assertRaisesRegex(RuntimeError, "sha256 mismatch"):
                backup_claude_state._verify_backup_zip(archive_path)

    def test_replacement_failure_preserves_existing_final(self):
        with tempfile.TemporaryDirectory() as root:
            sessions = os.path.join(root, "claude-code-sessions")
            metadata = os.path.join(sessions, "account-a", "organisation-a")
            os.makedirs(metadata)
            source = os.path.join(metadata, "local_one.json")
            with open(source, "wb") as handle:
                handle.write(b"first\n")
            destination = os.path.join(root, "metadata.zip")
            with mock.patch.object(backup_claude_state, "SESSIONS_BASE", sessions):
                pairs = backup_claude_state._discover_meta_pairs()
                backup_claude_state._backup_zip(
                    sessions, destination, lambda _message: None, False,
                    pairs=pairs, source_layer="desktop-metadata",
                )
                with open(destination, "rb") as handle:
                    prior_backup = handle.read()
                with open(source, "wb") as handle:
                    handle.write(b"second\n")
                with mock.patch.object(
                    backup_claude_state.os,
                    "replace",
                    side_effect=PermissionError("injected replacement failure"),
                ):
                    with self.assertRaisesRegex(PermissionError, "injected replacement failure"):
                        backup_claude_state._backup_zip(
                            sessions, destination, lambda _message: None, False,
                            pairs=pairs, source_layer="desktop-metadata",
                        )

            with open(destination, "rb") as handle:
                self.assertEqual(handle.read(), prior_backup)
            self.assertFalse(os.path.exists(destination + ".tmp"))

    def test_reserved_manifest_source_path_fails_without_remapping(self):
        with tempfile.TemporaryDirectory() as root:
            source_root = os.path.join(root, "projects")
            os.makedirs(source_root)
            with open(os.path.join(source_root, "one.jsonl"), "wb") as handle:
                handle.write(b"one\n")
            destination = os.path.join(root, "projects.zip")
            backup_claude_state._backup_zip(
                source_root, destination, lambda _message: None, False,
                source_layer="jsonl-projects",
            )
            with open(destination, "rb") as handle:
                prior_backup = handle.read()
            with open(os.path.join(source_root, "manifest.json"), "wb") as handle:
                handle.write(b"source manifest\n")

            with self.assertRaisesRegex(RuntimeError, "reserved archive control path"):
                backup_claude_state._backup_zip(
                    source_root, destination, lambda _message: None, False,
                    source_layer="jsonl-projects",
                )

            with open(destination, "rb") as handle:
                self.assertEqual(handle.read(), prior_backup)
            self.assertFalse(os.path.exists(destination + ".tmp"))

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
