"""Focused safety tests for the Desktop metadata backup restore companion."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import zipfile

import pytest


SESSIONS = Path(__file__).resolve().parents[1] / "tools" / "sessions"
TOOLS = SESSIONS.parent
sys.path.insert(0, str(SESSIONS))
sys.path.insert(0, str(TOOLS))

import restore_claude_metadata_backup as restore  # noqa: E402
import metadata_archive  # noqa: E402


ACCOUNT = "11111111-1111-1111-1111-111111111111"
ORGANISATION = "22222222-2222-2222-2222-222222222222"
BASE_ACCOUNT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
BASE_ORGANISATION = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    baseline = (
        state / "appdata" / "Claude" / "claude-code-sessions"
        / BASE_ACCOUNT / BASE_ORGANISATION / "local_existing.json"
    )
    baseline.parent.mkdir(parents=True)
    baseline.write_text(
        '{"sessionId":"existing","cwd":"C:\\\\fixture","title":"Existing"}\n',
        encoding="utf-8",
    )
    (state / "projects").mkdir()
    return state


def _token(state: Path) -> str:
    _snapshot, diagnosis_id = restore.current_snapshot_and_diagnosis_id(
        str(state / "appdata" / "Claude"),
        str(state / "projects"),
        fixture_mode=True,
    )
    return diagnosis_id


def _manifest_archive(
    path: Path,
    payloads: dict[str, bytes] | None = None,
    *,
    manifest_updates: dict | None = None,
    account: str = ACCOUNT,
    organisation: str = ORGANISATION,
) -> Path:
    if payloads is None:
        payloads = {"local_one.json": b'{"sessionId":"one"}\n'}
    root = f"{account}/{organisation}"
    files = []
    for name, content in payloads.items():
        files.append({
            "archive_path": f"{root}/{name}",
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "account_uuid": account,
            "organisation_uuid": organisation,
        })
    manifest = {
        "layout_version": 2,
        "source_layer": "desktop-metadata",
        "pairs": [{
            "account_uuid": account,
            "organisation_uuid": organisation,
            "archive_root": root,
            "file_count": len(files),
            "total_bytes": sum(len(value) for value in payloads.values()),
        }],
        "files": files,
    }
    if manifest_updates:
        manifest.update(manifest_updates)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in payloads.items():
            archive.writestr(f"{root}/{name}", content)
        archive.writestr("manifest.json", json.dumps(manifest))
    return path


def _run(state: Path, archive: Path, *extra: str) -> int:
    return restore.run([
        str(archive),
        "--state", str(state),
        "--diagnosis-id", _token(state),
        *extra,
    ])


def _tree_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*"), key=lambda value: value.as_posix()):
        digest.update(item.relative_to(path).as_posix().encode())
        digest.update(b"\0")
        if item.is_file():
            digest.update(item.read_bytes())
    return digest.hexdigest()


def _rewrite_manifest_raw(archive_path: Path, raw_manifest: bytes) -> None:
    with zipfile.ZipFile(archive_path, "r") as source:
        records = {name: source.read(name) for name in source.namelist()}
    records["manifest.json"] = raw_manifest
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as target:
        for name, content in records.items():
            target.writestr(name, content)


def test_v2_dry_run_and_apply_are_token_bound_and_privacy_safe(
    tmp_path, monkeypatch, capsys
):
    state = _state(tmp_path)
    archive = _manifest_archive(tmp_path / "desktop.zip")
    before = _tree_fingerprint(state)

    assert _run(state, archive) == 0
    assert _tree_fingerprint(state) == before
    output = capsys.readouterr().out
    assert "DRY RUN" in output
    assert "pair-01" in output
    assert ACCOUNT not in output and ORGANISATION not in output

    monkeypatch.setattr(restore, "desktop_process_running", lambda: False)
    assert _run(state, archive, "--apply") == 0
    target = (
        state / "appdata" / "Claude" / "claude-code-sessions"
        / ACCOUNT / ORGANISATION / "local_one.json"
    )
    assert target.read_bytes() == b'{"sessionId":"one"}\n'


def test_restore_uses_shared_archive_hard_caps():
    assert restore.MAX_ARCHIVE_FILES == metadata_archive.MAX_ARCHIVE_PAYLOAD_FILES
    assert (
        restore.MAX_UNCOMPRESSED_BYTES
        == metadata_archive.MAX_ARCHIVE_PAYLOAD_BYTES
    )
    assert restore.MAX_MANIFEST_BYTES == metadata_archive.MAX_MANIFEST_BYTES
    assert restore.MAX_METADATA_FILE_BYTES == metadata_archive.MAX_METADATA_FILE_BYTES


def test_include_paths_and_archive_name_never_reveal_pair_uuids(
    tmp_path, capsys
):
    state = _state(tmp_path)
    identity_file = "local_{}_{}.json".format(ACCOUNT, ORGANISATION)
    archive = _manifest_archive(
        tmp_path / "{}-{}-{}.zip".format(
            ACCOUNT, ACCOUNT.upper(), ORGANISATION.upper()
        ),
        {identity_file: b'{"sessionId":"one"}\n'},
    )

    assert _run(state, archive, "--include-paths") == 0
    output = capsys.readouterr().out
    assert ACCOUNT not in output
    assert ORGANISATION not in output
    assert ACCOUNT.upper() not in output
    assert ORGANISATION.upper() not in output
    assert "pair-01" in output
    assert "local_pair-01_pair-01.json" in output
    assert str(state / "appdata" / "Claude" / "claude-code-sessions") in output


def test_uncompressed_cap_counts_payload_only(tmp_path):
    state = _state(tmp_path)
    payload = b'{"sessionId":"one"}\n'
    archive = _manifest_archive(
        tmp_path / "desktop.zip", {"local_one.json": payload}
    )

    assert _run(
        state, archive, "--max-uncompressed-bytes", str(len(payload))
    ) == 0


def test_legacy_is_inspectable_with_explicit_pair_but_apply_is_refused(
    tmp_path, monkeypatch, capsys
):
    state = _state(tmp_path)
    archive = tmp_path / "legacy.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("local_legacy.json", b'{"sessionId":"legacy"}\n')

    assert _run(state, archive) == 3
    assert _run(
        state,
        archive,
        "--target-account-uuid", ACCOUNT,
        "--target-organisation-uuid", ORGANISATION,
        "--include-paths",
    ) == 0
    dry_output = capsys.readouterr().out
    assert "Layout        : legacy" in dry_output
    assert ACCOUNT not in dry_output and ORGANISATION not in dry_output
    assert "pair-01" in dry_output
    assert "Apply unavailable: mutation requires a layout v2" in dry_output
    assert "Re-run with --apply" not in dry_output
    monkeypatch.setattr(restore, "desktop_process_running", lambda: False)
    assert _run(
        state,
        archive,
        "--target-account-uuid", ACCOUNT,
        "--target-organisation-uuid", ORGANISATION,
        "--apply",
    ) == 3
    target = (
        state / "appdata" / "Claude" / "claude-code-sessions"
        / ACCOUNT / ORGANISATION / "local_legacy.json"
    )
    assert not target.exists()


def test_v2_refuses_legacy_target_override(tmp_path):
    state = _state(tmp_path)
    archive = _manifest_archive(tmp_path / "desktop.zip")
    assert _run(
        state,
        archive,
        "--target-account-uuid", ACCOUNT,
        "--target-organisation-uuid", ORGANISATION,
    ) == 3


def test_v2_refuses_zero_file_manifest(tmp_path):
    state = _state(tmp_path)
    archive = _manifest_archive(tmp_path / "empty.zip", {})
    assert _run(state, archive) == 3


@pytest.mark.parametrize(
    ("status_key", "status_value", "reason"),
    [
        ("schema_version", "unrecognised", "live state schema is unrecognised"),
        ("_metadata_inventory_complete", False, "live metadata inventory is incomplete"),
        ("_transcript_inventory_complete", False, "live transcript inventory is incomplete"),
    ],
)
def test_partial_or_unrecognised_state_never_emits_usable_apply_command(
    tmp_path, monkeypatch, capsys, status_key, status_value, reason
):
    state = _state(tmp_path)
    archive = _manifest_archive(tmp_path / "desktop.zip")
    original_build_snapshot = restore.build_snapshot

    def unsafe_snapshot(*args, **kwargs):
        snapshot = original_build_snapshot(*args, **kwargs)
        snapshot[status_key] = status_value
        return snapshot

    monkeypatch.setattr(restore, "build_snapshot", unsafe_snapshot)
    before = _tree_fingerprint(state)
    unsafe_token = restore.make_diagnosis_id(unsafe_snapshot(
        str(state / "appdata" / "Claude"),
        str(state / "projects"),
        fixture_mode=True,
        include_inventory_status=True,
    ))

    def run_unsafe(*extra):
        return restore.run([
            str(archive),
            "--state", str(state),
            "--diagnosis-id", unsafe_token,
            *extra,
        ])

    assert run_unsafe() == 0
    dry_output = capsys.readouterr().out
    assert "Apply unavailable: {}.".format(reason) in dry_output
    assert "Re-run with --apply" not in dry_output
    assert _tree_fingerprint(state) == before

    monkeypatch.setattr(restore, "desktop_process_running", lambda: False)
    assert run_unsafe("--apply") == 3
    assert reason in capsys.readouterr().out
    assert _tree_fingerprint(state) == before


@pytest.mark.parametrize("kind", ["bad-zip", "hash-mismatch"])
def test_corrupt_or_hash_mismatched_archive_is_refused(tmp_path, kind):
    state = _state(tmp_path)
    archive = tmp_path / "bad.zip"
    if kind == "bad-zip":
        archive.write_bytes(b"not a zip")
    else:
        _manifest_archive(archive)
        with zipfile.ZipFile(archive, "r") as source:
            records = {name: source.read(name) for name in source.namelist()}
        manifest = json.loads(records["manifest.json"])
        manifest["files"][0]["sha256"] = "0" * 64
        with zipfile.ZipFile(archive, "w") as target:
            for name, content in records.items():
                target.writestr(
                    name,
                    json.dumps(manifest) if name == "manifest.json" else content,
                )

    assert _run(state, archive) == 3


def test_zip_slip_duplicate_and_caps_are_refused(tmp_path):
    state = _state(tmp_path)
    unsafe = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(unsafe, "w") as archive:
        archive.writestr("../local.json", b"bad")
    assert _run(
        state, unsafe,
        "--target-account-uuid", ACCOUNT,
        "--target-organisation-uuid", ORGANISATION,
    ) == 3

    duplicate = tmp_path / "duplicate.zip"
    with pytest.warns(UserWarning):
        with zipfile.ZipFile(duplicate, "w") as archive:
            archive.writestr("local.json", b"one")
            archive.writestr("local.json", b"two")
    assert _run(
        state, duplicate,
        "--target-account-uuid", ACCOUNT,
        "--target-organisation-uuid", ORGANISATION,
    ) == 3

    capped = _manifest_archive(
        tmp_path / "capped.zip",
        {"local_one.json": b"1", "local_two.json": b"2"},
    )
    assert _run(state, capped, "--max-files", "1") == 3
    assert _run(state, capped, "--max-uncompressed-bytes", "1") == 3
    assert _run(state, capped, "--max-manifest-bytes", "1") == 3

    legacy_capped = tmp_path / "legacy-capped.zip"
    with zipfile.ZipFile(legacy_capped, "w") as archive:
        archive.writestr("local_one.json", b"1")
        archive.writestr("local_two.json", b"2")
    assert _run(
        state, legacy_capped,
        "--target-account-uuid", ACCOUNT,
        "--target-organisation-uuid", ORGANISATION,
        "--max-files", "1",
    ) == 3


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--max-files", str(restore.MAX_ARCHIVE_FILES + 1)),
        ("--max-uncompressed-bytes", str(restore.MAX_UNCOMPRESSED_BYTES + 1)),
        ("--max-manifest-bytes", str(restore.MAX_MANIFEST_BYTES + 1)),
        ("--max-files", "0"),
        ("--max-uncompressed-bytes", "0"),
        ("--max-manifest-bytes", "0"),
    ],
)
def test_cli_caps_can_only_lower_hard_limits(tmp_path, flag, value):
    state = _state(tmp_path)
    archive = _manifest_archive(tmp_path / "desktop.zip")
    assert _run(state, archive, flag, value) == 3


@pytest.mark.parametrize(
    "account",
    [
        "CON",
        "11111111-1111-1111-1111-11111111111Z",
        "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",
        "11111111111111111111111111111111",
    ],
)
def test_v2_requires_canonical_uuid_pair_segments(tmp_path, account):
    state = _state(tmp_path)
    archive = _manifest_archive(tmp_path / "desktop.zip", account=account)
    assert _run(state, archive) == 3


def test_windows_reserved_segments_are_rejected():
    for name in ("CON", "nul", "COM1", "lpt9.txt"):
        assert not restore._safe_segment(name)


@pytest.mark.parametrize(
    "payloads",
    [
        {"nested/local_one.json": b'{"sessionId":"one"}'},
        {"local_one.bin": b'{"sessionId":"one"}'},
        {"local_.json": b'{"sessionId":"one"}'},
    ],
)
def test_v2_rejects_nested_binary_or_noncanonical_metadata_names(
    tmp_path, payloads
):
    state = _state(tmp_path)
    archive = _manifest_archive(tmp_path / "desktop.zip", payloads)
    assert _run(state, archive) == 3


@pytest.mark.parametrize("payload", [b"not-json", b"\xff\xfe", b"[]", b"null"])
def test_v2_rejects_invalid_utf8_json_or_nonobject_metadata(tmp_path, payload):
    state = _state(tmp_path)
    archive = _manifest_archive(
        tmp_path / "desktop.zip", {"local_one.json": payload}
    )
    assert _run(state, archive) == 3


@pytest.mark.parametrize(
    "payload",
    [
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":-Infinity}',
        b'{"value":1e400}',
        ('{"value":' + ('9' * 5000) + '}').encode(),
        ('{"value":' + ('[' * 2000) + '0' + (']' * 2000) + '}').encode(),
    ],
)
def test_metadata_payload_rejects_nonfinite_huge_or_too_deep_json(
    tmp_path, payload
):
    state = _state(tmp_path)
    archive = _manifest_archive(
        tmp_path / "desktop.zip", {"local_one.json": payload}
    )
    assert _run(state, archive) == 3


@pytest.mark.parametrize(
    "replacement",
    [b"NaN", b"Infinity", b"-Infinity", b"1e400", b"9" * 5000],
)
def test_manifest_rejects_nonfinite_or_huge_json_number(tmp_path, replacement):
    state = _state(tmp_path)
    archive = _manifest_archive(tmp_path / "desktop.zip")
    with zipfile.ZipFile(archive, "r") as source:
        raw = source.read("manifest.json")
    raw = raw.replace(b'"layout_version": 2', b'"layout_version": ' + replacement)
    _rewrite_manifest_raw(archive, raw)
    assert _run(state, archive) == 3


def test_deeply_nested_live_metadata_never_escapes_as_traceback(
    tmp_path, monkeypatch, capsys
):
    state = _state(tmp_path)
    live = (
        state / "appdata" / "Claude" / "claude-code-sessions"
        / "33333333-3333-3333-3333-333333333333"
        / "44444444-4444-4444-4444-444444444444"
        / "local_deep.json"
    )
    live.parent.mkdir(parents=True)
    live.write_text('{"value":' + ('[' * 2000) + '0' + (']' * 2000) + '}')
    archive = _manifest_archive(tmp_path / "desktop.zip")

    monkeypatch.setattr(restore, "desktop_process_running", lambda: False)
    assert _run(state, archive, "--apply") == 3
    output = capsys.readouterr().out
    assert "live metadata inventory is incomplete" in output
    assert "Traceback" not in output


@pytest.mark.parametrize("payload", [b"not-json", b"\xff", b"[]"])
def test_legacy_rejects_invalid_utf8_json_or_nonobject_metadata(tmp_path, payload):
    state = _state(tmp_path)
    archive = tmp_path / "legacy.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("local_legacy.json", payload)
    assert _run(
        state,
        archive,
        "--target-account-uuid", ACCOUNT,
        "--target-organisation-uuid", ORGANISATION,
    ) == 3


def test_v2_rejects_manifest_schema_extensions(tmp_path):
    state = _state(tmp_path)
    archive = _manifest_archive(
        tmp_path / "desktop.zip", manifest_updates={"unexpected": True}
    )
    assert _run(state, archive) == 3


def test_stale_diagnosis_and_desktop_running_or_unknown_are_refused(
    tmp_path, monkeypatch
):
    state = _state(tmp_path)
    archive = _manifest_archive(tmp_path / "desktop.zip")
    assert restore.run([
        str(archive), "--state", str(state), "--diagnosis-id", "deadbeef"
    ]) == 3

    monkeypatch.setattr(restore, "desktop_process_running", lambda: True)
    assert _run(state, archive, "--apply") == 3
    target_root = state / "appdata" / "Claude" / "claude-code-sessions"
    assert list(target_root.rglob("local_one.json")) == []


def test_diagnosis_change_during_staging_causes_zero_publications(
    tmp_path, monkeypatch
):
    state = _state(tmp_path)
    archive = _manifest_archive(tmp_path / "desktop.zip")
    monkeypatch.setattr(restore, "desktop_process_running", lambda: False)
    original_revalidate = restore._revalidate_transaction_targets
    changed = False

    def change_diagnosis_after_staging(
        sessions_root, plan, anchors, staged_by_entry, created_entries
    ):
        nonlocal changed
        if not changed:
            assert list(state.rglob(".r-*"))
            transcript_dir = state / "projects" / "-changed-during-staging"
            transcript_dir.mkdir()
            (transcript_dir / "cccccccc-cccc-4ccc-8ccc-cccccccccccc.jsonl").write_text(
                "{}\n", encoding="utf-8"
            )
            changed = True
        return original_revalidate(
            sessions_root, plan, anchors, staged_by_entry, created_entries
        )

    publications = []
    monkeypatch.setattr(
        restore, "_revalidate_transaction_targets", change_diagnosis_after_staging
    )
    monkeypatch.setattr(
        restore.os,
        "link",
        lambda source, target: publications.append((source, target)),
    )

    assert _run(state, archive, "--apply") == 3
    assert publications == []
    assert list(state.rglob("local_one.json")) == []
    assert list(state.rglob(".r-*")) == []


def test_desktop_start_during_staging_causes_zero_publications(
    tmp_path, monkeypatch
):
    state = _state(tmp_path)
    archive = _manifest_archive(tmp_path / "desktop.zip")
    calls = 0

    def desktop_state():
        nonlocal calls
        calls += 1
        if calls == 1:
            return False
        sessions = state / "appdata" / "Claude" / "claude-code-sessions"
        assert list(state.rglob(".r-*"))
        return True

    publications = []
    monkeypatch.setattr(restore, "desktop_process_running", desktop_state)
    monkeypatch.setattr(
        restore.os,
        "link",
        lambda source, target: publications.append((source, target)),
    )

    assert _run(state, archive, "--apply") == 3
    assert calls == 2
    assert publications == []
    assert list(state.rglob("local_one.json")) == []
    assert list(state.rglob(".r-*")) == []


def test_identical_target_change_during_staging_causes_zero_publications(
    tmp_path, monkeypatch
):
    state = _state(tmp_path)
    archive = _manifest_archive(
        tmp_path / "desktop.zip",
        {
            "local_absent.json": b'{"sessionId":"absent"}',
            "local_identical.json": b'{"sessionId":"identical"}',
        },
    )
    pair = (
        state / "appdata" / "Claude" / "claude-code-sessions"
        / ACCOUNT / ORGANISATION
    )
    pair.mkdir(parents=True)
    identical = pair / "local_identical.json"
    identical.write_bytes(b'{"sessionId":"identical"}')
    monkeypatch.setattr(restore, "desktop_process_running", lambda: False)
    original_revalidate = restore._revalidate_transaction_targets
    changed = False

    def mutate_identical_after_staging(
        sessions_root, plan, anchors, staged_by_entry, created_entries
    ):
        nonlocal changed
        if not changed:
            assert list(state.rglob(".r-*"))
            identical.write_bytes(b'{"sessionId":"changed"}')
            changed = True
        return original_revalidate(
            sessions_root, plan, anchors, staged_by_entry, created_entries
        )

    publications = []
    monkeypatch.setattr(
        restore, "_revalidate_transaction_targets", mutate_identical_after_staging
    )
    monkeypatch.setattr(
        restore.os,
        "link",
        lambda source, target: publications.append((source, target)),
    )

    assert _run(state, archive, "--apply") == 3
    assert publications == []
    assert identical.read_bytes() == b'{"sessionId":"changed"}'
    assert not (pair / "local_absent.json").exists()
    assert list(pair.glob(".r-*")) == []


def test_absent_target_appearance_during_staging_causes_zero_publications(
    tmp_path, monkeypatch
):
    state = _state(tmp_path)
    archive = _manifest_archive(tmp_path / "desktop.zip")
    target = (
        state / "appdata" / "Claude" / "claude-code-sessions"
        / ACCOUNT / ORGANISATION / "local_one.json"
    )
    monkeypatch.setattr(restore, "desktop_process_running", lambda: False)
    original_revalidate = restore._revalidate_transaction_targets
    appeared = False

    def create_target_after_staging(
        sessions_root, plan, anchors, staged_by_entry, created_entries
    ):
        nonlocal appeared
        if not appeared:
            assert list(state.rglob(".r-*"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'{"sessionId":"external"}')
            appeared = True
        return original_revalidate(
            sessions_root, plan, anchors, staged_by_entry, created_entries
        )

    publications = []
    monkeypatch.setattr(
        restore, "_revalidate_transaction_targets", create_target_after_staging
    )
    monkeypatch.setattr(
        restore.os,
        "link",
        lambda source, destination: publications.append((source, destination)),
    )

    assert _run(state, archive, "--apply") == 3
    assert publications == []
    assert target.read_bytes() == b'{"sessionId":"external"}'
    assert list(state.rglob(".r-*")) == []


def test_desktop_start_during_parent_creation_causes_zero_publications(
    tmp_path, monkeypatch
):
    state = _state(tmp_path)
    archive = _manifest_archive(tmp_path / "desktop.zip")
    sessions = state / "appdata" / "Claude" / "claude-code-sessions"
    target = sessions / ACCOUNT / ORGANISATION / "local_one.json"
    running = False
    real_mkdir = os.mkdir

    def mkdir_and_start_desktop(path, *args, **kwargs):
        nonlocal running
        result = real_mkdir(path, *args, **kwargs)
        if str(path).startswith(str(sessions)):
            running = True
        return result

    monkeypatch.setattr(restore.os, "mkdir", mkdir_and_start_desktop)
    monkeypatch.setattr(restore, "desktop_process_running", lambda: running)
    publications = []
    monkeypatch.setattr(
        restore.os,
        "link",
        lambda source, destination: publications.append((source, destination)),
    )

    assert _run(state, archive, "--apply") == 3
    assert publications == []
    assert not target.exists()
    assert list(state.rglob(".r-*")) == []
    assert not (sessions / ACCOUNT).exists()


def test_identical_target_mutation_inside_atomic_create_rolls_back_created_file(
    tmp_path, monkeypatch
):
    state = _state(tmp_path)
    archive = _manifest_archive(
        tmp_path / "desktop.zip",
        {
            "local_create.json": b'{"sessionId":"create"}',
            "local_identical.json": b'{"sessionId":"identical"}',
        },
    )
    pair = (
        state / "appdata" / "Claude" / "claude-code-sessions"
        / ACCOUNT / ORGANISATION
    )
    pair.mkdir(parents=True)
    identical = pair / "local_identical.json"
    identical.write_bytes(b'{"sessionId":"identical"}')
    created = pair / "local_create.json"
    real_link = os.link

    def link_then_mutate_identical(source, destination):
        real_link(source, destination)
        identical.write_bytes(b'{"sessionId":"mutated-inside-link"}')

    monkeypatch.setattr(restore.os, "link", link_then_mutate_identical)
    monkeypatch.setattr(restore, "desktop_process_running", lambda: False)

    assert _run(state, archive, "--apply") == 3
    assert not created.exists()
    assert identical.read_bytes() == b'{"sessionId":"mutated-inside-link"}'
    assert list(pair.glob(".r-*")) == []


def test_desktop_start_inside_atomic_create_rolls_back_created_file(
    tmp_path, monkeypatch
):
    state = _state(tmp_path)
    archive = _manifest_archive(tmp_path / "desktop.zip")
    target = (
        state / "appdata" / "Claude" / "claude-code-sessions"
        / ACCOUNT / ORGANISATION / "local_one.json"
    )
    running = False
    real_link = os.link

    def link_then_start_desktop(source, destination):
        nonlocal running
        real_link(source, destination)
        running = True

    monkeypatch.setattr(restore.os, "link", link_then_start_desktop)
    monkeypatch.setattr(restore, "desktop_process_running", lambda: running)

    assert _run(state, archive, "--apply") == 3
    assert not target.exists()
    assert list(state.rglob(".r-*")) == []


def test_target_appearance_inside_atomic_create_is_not_overwritten(
    tmp_path, monkeypatch
):
    state = _state(tmp_path)
    archive = _manifest_archive(tmp_path / "desktop.zip")
    target = (
        state / "appdata" / "Claude" / "claude-code-sessions"
        / ACCOUNT / ORGANISATION / "local_one.json"
    )
    external = b'{"sessionId":"external-race"}'
    real_link = os.link

    def appear_then_link(source, destination):
        Path(destination).write_bytes(external)
        return real_link(source, destination)

    monkeypatch.setattr(restore.os, "link", appear_then_link)
    monkeypatch.setattr(restore, "desktop_process_running", lambda: False)

    assert _run(state, archive, "--apply") == 3
    assert target.read_bytes() == external
    assert list(state.rglob(".r-*")) == []


def test_rollback_retains_tampered_created_inode_and_reports_incomplete(
    tmp_path, monkeypatch, capsys
):
    state = _state(tmp_path)
    archive = _manifest_archive(tmp_path / "desktop.zip")
    target = (
        state / "appdata" / "Claude" / "claude-code-sessions"
        / ACCOUNT / ORGANISATION / "local_one.json"
    )
    tampered = b'{"sessionId":"tampered-after-create"}'
    real_link = os.link

    def link_then_tamper_created_inode(source, destination):
        real_link(source, destination)
        Path(destination).write_bytes(tampered)

    monkeypatch.setattr(restore.os, "link", link_then_tamper_created_inode)
    monkeypatch.setattr(restore, "desktop_process_running", lambda: False)

    assert _run(state, archive, "--apply") == 3
    assert target.read_bytes() == tampered
    assert "rollback incomplete" in capsys.readouterr().out
    assert list(state.rglob(".r-*")) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows target-handle semantics")
def test_rollback_target_name_swap_preserves_replacement(
    tmp_path, monkeypatch, capsys
):
    state = _state(tmp_path)
    archive = _manifest_archive(
        tmp_path / "desktop.zip",
        {
            "local_one.json": b'{"sessionId":"one"}',
            "local_two.json": b'{"sessionId":"two"}',
        },
    )
    replacement = tmp_path / "replacement.json"
    replacement_bytes = b'{"sessionId":"replacement-must-survive"}'
    replacement.write_bytes(replacement_bytes)
    real_link = os.link
    links = 0

    def fail_second_link(source, destination, *args, **kwargs):
        nonlocal links
        links += 1
        if links == 2:
            raise OSError("injected second-create failure")
        return real_link(source, destination, *args, **kwargs)

    original_delete_bound = restore._WindowsTargetLease.delete_bound
    swapped = False

    def swap_name_before_rollback(self, temporary, anchors, entry):
        nonlocal swapped
        if not swapped:
            target = Path(self.path)
            self.close()
            target.unlink()
            os.replace(replacement, target)
            swapped = True
        return original_delete_bound(self, temporary, anchors, entry)

    monkeypatch.setattr(restore.os, "link", fail_second_link)
    monkeypatch.setattr(
        restore._WindowsTargetLease, "delete_bound", swap_name_before_rollback
    )
    monkeypatch.setattr(restore, "desktop_process_running", lambda: False)

    assert _run(state, archive, "--apply") == 3
    target = (
        state / "appdata" / "Claude" / "claude-code-sessions"
        / ACCOUNT / ORGANISATION / "local_one.json"
    )
    assert swapped
    assert target.read_bytes() == replacement_bytes
    assert "rollback incomplete" in capsys.readouterr().out


@pytest.mark.skipif(os.name != "nt", reason="Windows target-handle semantics")
def test_windows_rollback_deletes_created_target_through_retained_handle(
    tmp_path, monkeypatch
):
    state = _state(tmp_path)
    archive = _manifest_archive(
        tmp_path / "desktop.zip",
        {
            "local_one.json": b'{"sessionId":"one"}',
            "local_two.json": b'{"sessionId":"two"}',
        },
    )
    real_link = os.link
    links = 0
    bound_deletes = 0

    def fail_second_link(source, destination, *args, **kwargs):
        nonlocal links
        links += 1
        if links == 2:
            raise OSError("injected second-create failure")
        return real_link(source, destination, *args, **kwargs)

    original_delete_bound = restore._WindowsTargetLease.delete_bound

    def count_bound_delete(self, temporary, anchors, entry):
        nonlocal bound_deletes
        bound_deletes += 1
        return original_delete_bound(self, temporary, anchors, entry)

    monkeypatch.setattr(restore.os, "link", fail_second_link)
    monkeypatch.setattr(
        restore._WindowsTargetLease, "delete_bound", count_bound_delete
    )
    monkeypatch.setattr(restore, "desktop_process_running", lambda: False)

    assert _run(state, archive, "--apply") == 3
    sessions = state / "appdata" / "Claude" / "claude-code-sessions"
    assert bound_deletes == 1
    assert not (sessions / ACCOUNT / ORGANISATION / "local_one.json").exists()
    assert not (sessions / ACCOUNT / ORGANISATION / "local_two.json").exists()
    assert (sessions / BASE_ACCOUNT / BASE_ORGANISATION / "local_existing.json").is_file()


@pytest.mark.skipif(os.name == "nt", reason="non-Windows portability contract")
def test_non_windows_rollback_retains_created_target_and_reports_incomplete(
    tmp_path, monkeypatch, capsys
):
    state = _state(tmp_path)
    archive = _manifest_archive(
        tmp_path / "desktop.zip",
        {
            "local_one.json": b'{"sessionId":"one"}',
            "local_two.json": b'{"sessionId":"two"}',
        },
    )
    real_link = os.link
    links = 0

    def fail_second_link(source, destination, *args, **kwargs):
        nonlocal links
        links += 1
        if links == 2:
            raise OSError("injected second-create failure")
        return real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(restore.os, "link", fail_second_link)
    monkeypatch.setattr(restore, "desktop_process_running", lambda: False)

    assert _run(state, archive, "--apply") == 3
    target = (
        state / "appdata" / "Claude" / "claude-code-sessions"
        / ACCOUNT / ORGANISATION / "local_one.json"
    )
    assert target.read_bytes() == b'{"sessionId":"one"}'
    assert "rollback incomplete" in capsys.readouterr().out


@pytest.mark.skipif(os.name != "nt", reason="Windows directory-handle semantics")
def test_created_directory_name_swap_at_cleanup_preserves_replacement(
    tmp_path, monkeypatch, capsys
):
    state = _state(tmp_path)
    archive = _manifest_archive(tmp_path / "desktop.zip")
    pair = (
        state / "appdata" / "Claude" / "claude-code-sessions"
        / ACCOUNT / ORGANISATION
    )
    marker_bytes = b"replacement-directory-must-survive"
    original_cleanup_one = restore._DirectoryAnchors._delete_created_directory_bound
    replaced = False

    def fail_first_link(_source, _destination, *args, **kwargs):
        raise OSError("injected pre-publication failure")

    def replace_directory_name(self, path):
        nonlocal replaced
        if not replaced and restore._normal_path(path) == restore._normal_path(str(pair)):
            key = restore._normal_path(path)
            handle, _record = self.records.pop(key)
            restore._close_windows_handle(handle)
            os.rmdir(path)
            os.mkdir(path)
            (Path(path) / "replacement.marker").write_bytes(marker_bytes)
            replaced = True
        return original_cleanup_one(self, path)

    monkeypatch.setattr(restore.os, "link", fail_first_link)
    monkeypatch.setattr(
        restore._DirectoryAnchors,
        "_delete_created_directory_bound",
        replace_directory_name,
    )
    monkeypatch.setattr(restore, "desktop_process_running", lambda: False)

    assert _run(state, archive, "--apply") == 3
    assert replaced
    assert (pair / "replacement.marker").read_bytes() == marker_bytes
    assert "rollback incomplete" in capsys.readouterr().out


@pytest.mark.skipif(os.name != "nt", reason="Windows directory-handle semantics")
def test_each_created_directory_is_anchored_immediately_after_mkdir(
    tmp_path, monkeypatch
):
    state = _state(tmp_path)
    archive = _manifest_archive(tmp_path / "desktop.zip")
    events = []
    real_mkdir = restore.os.mkdir
    real_record = restore._windows_directory_record

    def record_mkdir(path, *args, **kwargs):
        result = real_mkdir(path, *args, **kwargs)
        events.append(("mkdir", Path(path).name))
        return result

    def record_anchor(path, *, delete_access=False, share_delete=False):
        if delete_access:
            events.append(("anchor", Path(path).name))
        return real_record(
            path, delete_access=delete_access, share_delete=share_delete
        )

    monkeypatch.setattr(restore.os, "mkdir", record_mkdir)
    monkeypatch.setattr(restore, "_windows_directory_record", record_anchor)
    monkeypatch.setattr(restore, "desktop_process_running", lambda: False)

    assert _run(state, archive, "--apply") == 0
    pair_events = [event for event in events if event[1] in {ACCOUNT, ORGANISATION}]
    assert pair_events[:4] == [
        ("mkdir", ACCOUNT),
        ("anchor", ACCOUNT),
        ("mkdir", ORGANISATION),
        ("anchor", ORGANISATION),
    ]


def test_live_state_guard_refuses_before_materializing_over_cap_directory(
    tmp_path, monkeypatch
):
    root = tmp_path / "guarded"
    root.mkdir()
    (root / "one.jsonl").write_text("{}\n", encoding="utf-8")
    (root / "two.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(restore, "MAX_GUARD_ENTRIES_PER_DIRECTORY", 1)

    with pytest.raises(
        restore.RestoreRefusal,
        match="bounded directory entries",
    ):
        restore._guard_tree_records(
            str(root),
            excluded_files=set(),
            excluded_dirs=set(),
            transcript_only=True,
            budget={"files": 0, "bytes": 0},
        )


@pytest.mark.skipif(os.name == "nt", reason="non-Windows portability contract")
def test_non_windows_created_directories_are_left_on_rollback(
    tmp_path, monkeypatch, capsys
):
    state = _state(tmp_path)
    archive = _manifest_archive(tmp_path / "desktop.zip")
    pair = (
        state / "appdata" / "Claude" / "claude-code-sessions"
        / ACCOUNT / ORGANISATION
    )
    monkeypatch.setattr(
        restore.os, "link", lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("injected pre-publication failure")
        )
    )
    monkeypatch.setattr(restore, "desktop_process_running", lambda: False)

    assert _run(state, archive, "--apply") == 3
    assert pair.is_dir()
    assert "rollback incomplete" in capsys.readouterr().out


def test_post_final_desktop_check_rolls_back_before_success(tmp_path, monkeypatch):
    state = _state(tmp_path)
    archive = _manifest_archive(tmp_path / "desktop.zip")
    target = (
        state / "appdata" / "Claude" / "claude-code-sessions"
        / ACCOUNT / ORGANISATION / "local_one.json"
    )
    calls = 0

    def desktop_only_after_post_create_check():
        nonlocal calls
        calls += 1
        return calls >= 6

    monkeypatch.setattr(
        restore, "desktop_process_running", desktop_only_after_post_create_check
    )

    assert _run(state, archive, "--apply") == 3
    assert calls == 6
    assert not target.exists()
    assert list(state.rglob(".r-*")) == []


def test_collision_refuses_entire_restore_without_writes(tmp_path):
    state = _state(tmp_path)
    archive = _manifest_archive(
        tmp_path / "desktop.zip",
        {
            "local_one.json": b'{"sessionId":"one"}',
            "local_two.json": b'{"sessionId":"two"}',
        },
    )
    pair = (
        state / "appdata" / "Claude" / "claude-code-sessions"
        / ACCOUNT / ORGANISATION
    )
    pair.mkdir(parents=True)
    collision = pair / "local_two.json"
    collision.write_bytes(b'{"sessionId":"different"}')

    assert _run(state, archive) == 3
    assert collision.read_bytes() == b'{"sessionId":"different"}'
    assert not (pair / "local_one.json").exists()


def test_identical_existing_file_is_skipped(tmp_path, monkeypatch):
    state = _state(tmp_path)
    archive = _manifest_archive(tmp_path / "desktop.zip")
    pair = (
        state / "appdata" / "Claude" / "claude-code-sessions"
        / ACCOUNT / ORGANISATION
    )
    pair.mkdir(parents=True)
    target = pair / "local_one.json"
    target.write_bytes(b'{"sessionId":"one"}\n')
    before = target.stat().st_mtime_ns
    monkeypatch.setattr(restore, "desktop_process_running", lambda: False)

    assert _run(state, archive, "--apply") == 0
    assert target.stat().st_mtime_ns == before


def test_publication_failure_removes_every_file_created_by_this_run(
    tmp_path, monkeypatch
):
    state = _state(tmp_path)
    archive = _manifest_archive(
        tmp_path / "desktop.zip",
        {
            "local_one.json": b'{"sessionId":"one"}',
            "local_two.json": b'{"sessionId":"two"}',
        },
    )
    monkeypatch.setattr(restore, "desktop_process_running", lambda: False)
    real_link = os.link
    publications = 0

    def fail_second_publication(source, destination):
        nonlocal publications
        if os.path.basename(source).startswith(".r-"):
            publications += 1
            if publications == 2:
                raise OSError("injected publication failure")
        return real_link(source, destination)

    monkeypatch.setattr(restore.os, "link", fail_second_publication)
    assert _run(state, archive, "--apply") == 3
    sessions = state / "appdata" / "Claude" / "claude-code-sessions"
    assert not (sessions / ACCOUNT / ORGANISATION / "local_one.json").exists()
    assert not (sessions / ACCOUNT / ORGANISATION / "local_two.json").exists()
    assert (sessions / BASE_ACCOUNT / BASE_ORGANISATION / "local_existing.json").is_file()
    assert list(sessions.rglob(".r-*")) == []


def test_multiple_own_metadata_links_do_not_trip_normalized_diagnosis(
    tmp_path, monkeypatch
):
    state = _state(tmp_path)
    archive = _manifest_archive(
        tmp_path / "desktop.zip",
        {
            "local_one.json": b'{"sessionId":"one"}',
            "local_two.json": b'{"sessionId":"two"}',
        },
    )
    monkeypatch.setattr(restore, "desktop_process_running", lambda: False)

    assert _run(state, archive, "--apply") == 0
    pair = (
        state / "appdata" / "Claude" / "claude-code-sessions"
        / ACCOUNT / ORGANISATION
    )
    assert sorted(path.name for path in pair.glob("local_*.json")) == [
        "local_one.json", "local_two.json",
    ]
    assert list(pair.glob(".r-*")) == []


@pytest.mark.parametrize(
    ("surface", "operation"),
    [
        ("transcript", "add"),
        ("transcript", "remove"),
        ("transcript", "modify"),
        ("metadata", "add"),
        ("metadata", "remove"),
        ("metadata", "modify"),
    ],
)
def test_external_state_drift_inside_second_link_rolls_back_every_restore(
    tmp_path, monkeypatch, surface, operation
):
    state = _state(tmp_path)
    archive = _manifest_archive(
        tmp_path / "desktop.zip",
        {
            "local_one.json": b'{"sessionId":"one"}',
            "local_two.json": b'{"sessionId":"two"}',
        },
    )
    if surface == "transcript":
        external = (
            state / "projects" / "-external"
            / "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa.jsonl"
        )
        external.parent.mkdir()
        original = b'{"message":"aaaa"}\n'
        changed = b'{"message":"bbbb"}\n'
    else:
        external = (
            state / "appdata" / "Claude" / "claude-code-sessions"
            / "33333333-3333-3333-3333-333333333333"
            / "44444444-4444-4444-4444-444444444444"
            / "local_external.json"
        )
        external.parent.mkdir(parents=True)
        original = b'{"sessionId":"aaaa"}'
        changed = b'{"sessionId":"bbbb"}'
    if operation != "add":
        external.write_bytes(original)

    real_link = os.link
    calls = 0

    def link_then_drift(source, destination, *args, **kwargs):
        nonlocal calls
        result = real_link(source, destination, *args, **kwargs)
        calls += 1
        if calls == 2:
            if operation == "add":
                external.parent.mkdir(parents=True, exist_ok=True)
                external.write_bytes(changed)
            elif operation == "remove":
                external.unlink()
            else:
                external.write_bytes(changed)
        return result

    monkeypatch.setattr(restore.os, "link", link_then_drift)
    monkeypatch.setattr(restore, "desktop_process_running", lambda: False)

    assert _run(state, archive, "--apply") == 3
    pair = (
        state / "appdata" / "Claude" / "claude-code-sessions"
        / ACCOUNT / ORGANISATION
    )
    assert not (pair / "local_one.json").exists()
    assert not (pair / "local_two.json").exists()
    assert list(state.rglob(".r-*")) == []


@pytest.mark.parametrize("mutation", ["content", "replace"])
def test_archive_drift_inside_second_link_rolls_back_every_restore(
    tmp_path, monkeypatch, mutation
):
    state = _state(tmp_path)
    archive = _manifest_archive(
        tmp_path / "desktop.zip",
        {
            "local_one.json": b'{"sessionId":"one"}',
            "local_two.json": b'{"sessionId":"two"}',
        },
    )
    replacement = _manifest_archive(
        tmp_path / "replacement.zip",
        {
            "local_one.json": b'{"sessionId":"changed-one"}',
            "local_two.json": b'{"sessionId":"changed-two"}',
        },
    )
    real_link = os.link
    calls = 0

    def link_then_change_archive(source, destination, *args, **kwargs):
        nonlocal calls
        result = real_link(source, destination, *args, **kwargs)
        calls += 1
        if calls == 2:
            if mutation == "content":
                archive.write_bytes(replacement.read_bytes())
            else:
                os.replace(replacement, archive)
        return result

    monkeypatch.setattr(restore.os, "link", link_then_change_archive)
    monkeypatch.setattr(restore, "desktop_process_running", lambda: False)

    assert _run(state, archive, "--apply") != 0
    pair = (
        state / "appdata" / "Claude" / "claude-code-sessions"
        / ACCOUNT / ORGANISATION
    )
    assert not (pair / "local_one.json").exists()
    assert not (pair / "local_two.json").exists()
    assert list(state.rglob(".r-*")) == []


def test_destination_parent_swap_inside_create_cannot_escape_and_rolls_back(
    tmp_path, monkeypatch
):
    state = _state(tmp_path)
    archive = _manifest_archive(tmp_path / "desktop.zip")
    pair = (
        state / "appdata" / "Claude" / "claude-code-sessions"
        / ACCOUNT / ORGANISATION
    )
    pair.mkdir(parents=True)
    displaced = tmp_path / "displaced-pair"
    outside = tmp_path / "outside"
    outside.mkdir()
    real_link = os.link

    def swap_parent_inside_create(source, destination, *args, **kwargs):
        if os.name == "nt":
            try:
                os.rename(pair, displaced)
            except OSError as exc:
                # A retained no-FILE_SHARE_DELETE directory handle must block
                # the first step required to install a junction replacement.
                raise OSError("destination parent swap was blocked") from exc
            raise AssertionError("Windows directory anchor allowed parent rename")
        os.rename(pair, displaced)
        os.symlink(outside, pair, target_is_directory=True)
        return real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(restore.os, "link", swap_parent_inside_create)
    monkeypatch.setattr(restore, "desktop_process_running", lambda: False)

    assert _run(state, archive, "--apply") != 0
    assert not (outside / "local_one.json").exists()
    assert not (displaced / "local_one.json").exists()
    assert not (pair / "local_one.json").exists()
    assert list(state.rglob(".r-*")) == []
