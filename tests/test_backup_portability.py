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
import metadata_archive  # noqa: E402


ACCOUNT_A = "11111111-1111-1111-1111-111111111111"
ORGANISATION_A = "22222222-2222-2222-2222-222222222222"
ACCOUNT_B = "33333333-3333-3333-3333-333333333333"
ORGANISATION_B = "44444444-4444-4444-4444-444444444444"


class BackupPortabilityTests(unittest.TestCase):
    def test_backup_uses_shared_archive_hard_caps(self):
        self.assertEqual(
            backup_claude_state.MAX_ARCHIVE_FILES,
            metadata_archive.MAX_ARCHIVE_PAYLOAD_FILES,
        )
        self.assertEqual(
            backup_claude_state.MAX_UNCOMPRESSED_BYTES,
            metadata_archive.MAX_ARCHIVE_PAYLOAD_BYTES,
        )
        self.assertEqual(
            backup_claude_state.MAX_MANIFEST_BYTES,
            metadata_archive.MAX_MANIFEST_BYTES,
        )
        self.assertEqual(
            backup_claude_state.MAX_METADATA_FILE_BYTES,
            metadata_archive.MAX_METADATA_FILE_BYTES,
        )

    def test_metadata_pair_discovery_handles_zero_pairs(self):
        with tempfile.TemporaryDirectory() as root:
            destination = os.path.join(root, "metadata.zip")
            valid_sessions = os.path.join(root, "valid-sessions")
            valid_metadata = os.path.join(valid_sessions, ACCOUNT_A, ORGANISATION_A)
            os.makedirs(valid_metadata)
            with open(os.path.join(valid_metadata, "local_one.json"), "wb") as handle:
                handle.write(b'{"sessionId":"valid"}\n')
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
            metadata = os.path.join(sessions, ACCOUNT_A, ORGANISATION_A)
            os.makedirs(metadata)
            with mock.patch.object(backup_claude_state, "SESSIONS_BASE", sessions):
                pairs = backup_claude_state._discover_meta_pairs()
                self.assertEqual(len(pairs), 1)
                self.assertEqual(backup_claude_state._discover_meta_dir(), metadata)

    def test_metadata_backup_manifest_keeps_multiple_pairs_and_hashes_files(self):
        with tempfile.TemporaryDirectory() as root:
            sessions = os.path.join(root, "claude-code-sessions")
            for account, organisation, content in (
                (ACCOUNT_A, ORGANISATION_A, b'{"sessionId":"old"}\n'),
                (ACCOUNT_B, ORGANISATION_B, b'{"sessionId":"new"}\n'),
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

            self.assertEqual(
                source_bytes,
                len(b'{"sessionId":"old"}\n') + len(b'{"sessionId":"new"}\n'),
            )
            with zipfile.ZipFile(destination) as archive:
                self.assertEqual(
                    set(archive.namelist()),
                    {
                        f"{ACCOUNT_A}/{ORGANISATION_A}/local_same.json",
                        f"{ACCOUNT_B}/{ORGANISATION_B}/local_same.json",
                        "manifest.json",
                    },
                )
                manifest = json.loads(archive.read("manifest.json"))

            self.assertEqual(manifest["layout_version"], 2)
            self.assertEqual(manifest["source_layer"], "desktop-metadata")
            self.assertEqual(
                [(p["account_uuid"], p["organisation_uuid"]) for p in manifest["pairs"]],
                [(ACCOUNT_A, ORGANISATION_A), (ACCOUNT_B, ORGANISATION_B)],
            )
            self.assertEqual(len(manifest["files"]), 2)
            for entry in manifest["files"]:
                with zipfile.ZipFile(destination) as archive:
                    content = archive.read(entry["archive_path"])
                self.assertEqual(entry["size"], len(content))
                self.assertEqual(
                    entry["sha256"], hashlib.sha256(content).hexdigest()
                )

    def test_metadata_backup_archives_only_direct_restore_compatible_files(self):
        with tempfile.TemporaryDirectory() as root:
            sessions = os.path.join(root, "claude-code-sessions")
            metadata = os.path.join(sessions, ACCOUNT_A, ORGANISATION_A)
            nested = os.path.join(metadata, "nested")
            os.makedirs(nested)
            eligible = os.path.join(metadata, "local_one.json")
            with open(eligible, "wb") as handle:
                handle.write(b'{"sessionId":"one"}\n')
            for path, content in (
                (os.path.join(sessions, "local_root.json"), b'{"root":true}'),
                (os.path.join(metadata, "notes.txt"), b"auxiliary"),
                (os.path.join(metadata, "local_one.json.tmp"), b"temporary"),
                (os.path.join(nested, "local_nested.json"), b'{"nested":true}'),
            ):
                with open(path, "wb") as handle:
                    handle.write(content)

            destination = os.path.join(root, "metadata.zip")
            with mock.patch.object(backup_claude_state, "SESSIONS_BASE", sessions):
                pairs = backup_claude_state._discover_meta_pairs()
                backup_claude_state._backup_zip(
                    sessions, destination, lambda _message: None, False,
                    pairs=pairs, source_layer="desktop-metadata",
                )

            with zipfile.ZipFile(destination) as archive:
                self.assertEqual(
                    set(archive.namelist()),
                    {f"{ACCOUNT_A}/{ORGANISATION_A}/local_one.json", "manifest.json"},
                )

    def test_zero_eligible_or_invalid_metadata_preserves_existing_final(self):
        with tempfile.TemporaryDirectory() as root:
            sessions = os.path.join(root, "claude-code-sessions")
            metadata = os.path.join(sessions, ACCOUNT_A, ORGANISATION_A)
            os.makedirs(metadata)
            source = os.path.join(metadata, "local_one.json")
            with open(source, "wb") as handle:
                handle.write(b'{"sessionId":"one"}\n')
            destination = os.path.join(root, "metadata.zip")
            with mock.patch.object(backup_claude_state, "SESSIONS_BASE", sessions):
                pairs = backup_claude_state._discover_meta_pairs()
                backup_claude_state._backup_zip(
                    sessions, destination, lambda _message: None, False,
                    pairs=pairs, source_layer="desktop-metadata",
                )
                with open(destination, "rb") as handle:
                    prior = handle.read()

                os.unlink(source)
                with open(os.path.join(metadata, "local_one.json.tmp"), "wb") as handle:
                    handle.write(b"temporary")
                pairs = backup_claude_state._discover_meta_pairs()
                with self.assertRaisesRegex(RuntimeError, "No eligible direct metadata"):
                    backup_claude_state._backup_zip(
                        sessions, destination, lambda _message: None, False,
                        pairs=pairs, source_layer="desktop-metadata",
                    )

                with open(source, "wb") as handle:
                    handle.write(b'{"value":NaN}')
                pairs = backup_claude_state._discover_meta_pairs()
                with self.assertRaisesRegex(RuntimeError, "metadata payload"):
                    backup_claude_state._backup_zip(
                        sessions, destination, lambda _message: None, False,
                        pairs=pairs, source_layer="desktop-metadata",
                    )

            with open(destination, "rb") as handle:
                self.assertEqual(handle.read(), prior)
            self.assertFalse(os.path.exists(destination + ".tmp"))

    def test_noncanonical_pair_refuses_before_publication(self):
        with tempfile.TemporaryDirectory() as root:
            sessions = os.path.join(root, "claude-code-sessions")
            metadata = os.path.join(sessions, "not-a-uuid", ORGANISATION_A)
            os.makedirs(metadata)
            with open(os.path.join(metadata, "local_one.json"), "wb") as handle:
                handle.write(b'{"sessionId":"one"}\n')
            with mock.patch.object(backup_claude_state, "SESSIONS_BASE", sessions):
                with self.assertRaisesRegex(RuntimeError, "canonical UUID"):
                    backup_claude_state._discover_meta_pairs()

    def test_verification_failure_preserves_existing_final(self):
        with tempfile.TemporaryDirectory() as root:
            sessions = os.path.join(root, "claude-code-sessions")
            metadata = os.path.join(sessions, ACCOUNT_A, ORGANISATION_A)
            os.makedirs(metadata)
            source = os.path.join(metadata, "local_one.json")
            with open(source, "wb") as handle:
                handle.write(b'{"sessionId":"first"}\n')
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
                    handle.write(b'{"sessionId":"second"}\n')
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
            metadata = os.path.join(sessions, ACCOUNT_A, ORGANISATION_A)
            os.makedirs(metadata)
            source = os.path.join(metadata, "local_one.json")
            with open(source, "wb") as handle:
                handle.write(b'{"sessionId":"first"}\n')
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
                    handle.write(b'{"sessionId":"second"}\n')
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

    def test_low_discovery_caps_refuse_metadata_enumeration(self):
        with tempfile.TemporaryDirectory() as root:
            sessions = os.path.join(root, "claude-code-sessions")
            account = os.path.join(sessions, ACCOUNT_A)
            for organisation in (ORGANISATION_A, ORGANISATION_B):
                metadata = os.path.join(account, organisation)
                os.makedirs(metadata)
                with open(os.path.join(metadata, "local_one.json"), "wb") as handle:
                    handle.write(b'{"sessionId":"one"}\n')
            with open(os.path.join(sessions, "auxiliary.txt"), "wb") as handle:
                handle.write(b"ignored auxiliary")

            cases = {
                "per-directory": metadata_archive.ArchiveDiscoveryLimits(
                    max_entries_per_directory=1
                ),
                "total traversal": metadata_archive.ArchiveDiscoveryLimits(
                    max_total_entries=2
                ),
                "directory traversal": metadata_archive.ArchiveDiscoveryLimits(
                    max_directories=2
                ),
                "retained paths": metadata_archive.ArchiveDiscoveryLimits(
                    max_retained_paths=1
                ),
                "metadata pairs": metadata_archive.ArchiveDiscoveryLimits(
                    max_metadata_pairs=1
                ),
            }
            with mock.patch.object(backup_claude_state, "SESSIONS_BASE", sessions):
                for label, limits in cases.items():
                    with self.subTest(label=label):
                        with self.assertRaises(
                            metadata_archive.MetadataArchiveFormatError
                        ):
                            backup_claude_state._discover_meta_pairs(
                                discovery_limits=limits
                            )

    def test_inaccessible_discovery_refuses_safely(self):
        with tempfile.TemporaryDirectory() as root:
            sessions = os.path.join(root, "claude-code-sessions")
            os.makedirs(sessions)
            with mock.patch.object(backup_claude_state, "SESSIONS_BASE", sessions), \
                    mock.patch.object(
                        backup_claude_state.os,
                        "scandir",
                        side_effect=PermissionError("injected inaccessible directory"),
                    ):
                with self.assertRaisesRegex(
                    metadata_archive.MetadataArchiveFormatError,
                    "cannot be enumerated safely",
                ):
                    backup_claude_state._discover_meta_pairs()

    def test_low_payload_and_manifest_caps_preserve_existing_final(self):
        with tempfile.TemporaryDirectory() as root:
            sessions = os.path.join(root, "claude-code-sessions")
            metadata = os.path.join(sessions, ACCOUNT_A, ORGANISATION_A)
            os.makedirs(metadata)
            with open(os.path.join(metadata, "local_one.json"), "wb") as handle:
                handle.write(b'{"sessionId":"one"}\n')
            destination = os.path.join(root, "metadata.zip")
            with mock.patch.object(backup_claude_state, "SESSIONS_BASE", sessions):
                pairs = backup_claude_state._discover_meta_pairs()
                backup_claude_state._backup_zip(
                    sessions,
                    destination,
                    lambda _message: None,
                    False,
                    pairs=pairs,
                    source_layer="desktop-metadata",
                )
                with open(destination, "rb") as handle:
                    prior = handle.read()

                with open(os.path.join(metadata, "local_two.json"), "wb") as handle:
                    handle.write(b'{"sessionId":"two"}\n')
                pairs = backup_claude_state._discover_meta_pairs()
                limits_to_refuse = (
                    metadata_archive.ArchiveDiscoveryLimits(max_payload_files=1),
                    metadata_archive.ArchiveDiscoveryLimits(max_payload_bytes=1),
                    metadata_archive.ArchiveDiscoveryLimits(
                        max_metadata_file_bytes=1
                    ),
                )
                for limits in limits_to_refuse:
                    with self.subTest(limits=limits):
                        with self.assertRaises(
                            metadata_archive.MetadataArchiveFormatError
                        ):
                            backup_claude_state._backup_zip(
                                sessions,
                                destination,
                                lambda _message: None,
                                False,
                                pairs=pairs,
                                source_layer="desktop-metadata",
                                discovery_limits=limits,
                            )
                        with open(destination, "rb") as handle:
                            self.assertEqual(handle.read(), prior)
                        self.assertFalse(os.path.exists(destination + ".tmp"))

                with mock.patch.object(
                    backup_claude_state, "MAX_MANIFEST_BYTES", 1
                ):
                    with self.assertRaisesRegex(
                        RuntimeError, "manifest exceeds the bounded size limit"
                    ):
                        backup_claude_state._backup_zip(
                            sessions,
                            destination,
                            lambda _message: None,
                            False,
                            pairs=pairs,
                            source_layer="desktop-metadata",
                        )
                with open(destination, "rb") as handle:
                    self.assertEqual(handle.read(), prior)
                self.assertFalse(os.path.exists(destination + ".tmp"))

    def test_generic_walk_is_bounded_and_preserves_existing_final(self):
        with tempfile.TemporaryDirectory() as root:
            source_root = os.path.join(root, "projects")
            os.makedirs(source_root)
            with open(os.path.join(source_root, "one.jsonl"), "wb") as handle:
                handle.write(b"one\n")
            destination = os.path.join(root, "projects.zip")
            backup_claude_state._backup_zip(
                source_root,
                destination,
                lambda _message: None,
                False,
                source_layer="jsonl-projects",
            )
            with open(destination, "rb") as handle:
                prior = handle.read()

            nested = os.path.join(source_root, "nested")
            os.makedirs(nested)
            with open(os.path.join(nested, "two.jsonl"), "wb") as handle:
                handle.write(b"two\n")
            limits = metadata_archive.ArchiveDiscoveryLimits(max_payload_files=1)
            with self.assertRaises(metadata_archive.MetadataArchiveFormatError):
                backup_claude_state._backup_zip(
                    source_root,
                    destination,
                    lambda _message: None,
                    False,
                    source_layer="jsonl-projects",
                    discovery_limits=limits,
                )
            with open(destination, "rb") as handle:
                self.assertEqual(handle.read(), prior)
            self.assertFalse(os.path.exists(destination + ".tmp"))

    def test_metadata_file_collection_stops_at_the_configured_cap(self):
        with tempfile.TemporaryDirectory() as root:
            sessions = os.path.join(root, "claude-code-sessions")
            metadata = os.path.join(sessions, ACCOUNT_A, ORGANISATION_A)
            os.makedirs(metadata)
            paths = []
            for name in ("local_one.json", "local_two.json"):
                path = os.path.join(metadata, name)
                with open(path, "wb") as handle:
                    handle.write(b'{"sessionId":"one"}\n')
                paths.append(path)

            def hostile_listing():
                yield paths[0]
                yield paths[1]
                raise AssertionError("collector read beyond its file cap")

            pairs = [{
                "account_uuid": ACCOUNT_A,
                "organisation_uuid": ORGANISATION_A,
                "path": metadata,
                "files": hostile_listing(),
            }]
            limits = metadata_archive.ArchiveDiscoveryLimits(max_payload_files=1)
            with self.assertRaises(metadata_archive.MetadataArchiveFormatError):
                backup_claude_state._backup_zip(
                    sessions,
                    os.path.join(root, "metadata.zip"),
                    lambda _message: None,
                    False,
                    pairs=pairs,
                    source_layer="desktop-metadata",
                    discovery_limits=limits,
                )

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
