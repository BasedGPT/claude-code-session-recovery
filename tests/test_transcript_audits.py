"""Focused tests for bounded transcript audits and duplicate-path safety."""

import hashlib
import io
import json
import os
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOLS = str(ROOT / "tools")
SESSIONS = str(ROOT / "tools" / "sessions")
for path in (TOOLS, SESSIONS):
    if path not in sys.path:
        sys.path.insert(0, path)

import transcript_audit
import transcript_files
import audit_transcript_identity
import audit_transcript_integrity
import cleanup_synth_duplicates
import diagnose
import recover_deleted_branches_worktrees
import recover_vscode_sessions
import repair_session_metadata
import repoint_session_to_jsonl
import restore_from_vss
import rewrite_metadata_cwd
import session_metadata
import session_state
import sweep_junction_canonical_cwds
import synth_session_metadata


audit_identity = audit_transcript_identity.audit_identity
integrity_main = audit_transcript_integrity.main


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")
    return path


def _fingerprint(root):
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _record(uuid, parent=None, message_id=None):
    value = {"uuid": uuid}
    if parent is not None:
        value["parentUuid"] = parent
    if message_id is not None:
        value["messageId"] = message_id
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def test_integrity_counts_graph_facts_without_emitting_content(tmp_path):
    path = tmp_path / "projects" / "slug" / "one.jsonl"
    lines = [
        _record("u1", message_id="m1"),
        _record("u2", "u1", "m2"),
        _record("u3", "u1", "m2"),
        _record("u4", "missing-parent", "m4"),
        _record("u5", "u6", "m5"),
        _record("u6", "u5", "m6"),
        _record("u2", "u1", "m2"),
        b"not-json",
        b"[1]",
        b"\xff-invalid",
        b"{\x00}",
        b"   ",
    ]
    _write(path, b"\n".join(lines))

    result = transcript_audit.audit_transcript_paths([path])
    summary = result["summary"]

    assert result["status"] == "complete"
    assert summary["files_present"] == 1
    assert summary["physical_lines"] == 12
    assert summary["blank_lines"] == 1
    assert summary["malformed_json"] == 2
    assert summary["non_object_json"] == 1
    assert summary["invalid_utf8_lines"] == 1
    assert summary["nul_bytes"] == 1
    assert summary["nul_lines"] == 1
    assert summary["duplicate_uuid_values"] == 1
    assert summary["duplicate_message_id_values"] == 1
    assert summary["explicit_roots"] == 1
    assert summary["missing_parent_references"] == 1
    assert summary["fork_points"] == 1
    assert summary["leaves"] == 3
    assert summary["weak_components"] == 3
    assert summary["reachable_from_explicit_roots"] == 3
    assert summary["unrooted_nodes"] == 3
    assert summary["cycle_count"] == 1
    assert summary["cycle_node_count"] == 2
    assert "Private" not in json.dumps(result)


def test_integrity_counts_missing_empty_and_bounded_files(tmp_path):
    missing = tmp_path / "missing.jsonl"
    empty = _write(tmp_path / "empty.jsonl", b"")
    oversized = _write(tmp_path / "oversized.jsonl", b"{" + b"x" * 100 + b"}\n")

    result = transcript_audit.audit_transcript_paths(
        [missing, empty, oversized], max_line_bytes=8, max_nodes_per_file=10
    )

    assert result["status"] == "bounded"
    assert result["summary"]["files_missing"] == 1
    assert result["summary"]["files_empty"] == 1
    assert result["summary"]["bounded_files"] == 1
    assert result["summary"]["malformed_json"] == 0


def test_integrity_counts_integer_digit_limit_as_malformed_json(tmp_path):
    path = _write(
        tmp_path / "oversized-integer.jsonl",
        '{"oversized": ' + ("1" * 5000) + "}\n",
    )

    result = transcript_audit.audit_transcript_paths([path])

    assert result["files"][0]["malformed_json"] == 1
    assert result["findings"] == [{
        "kind": "malformed_json",
        "reference": "transcript-0001",
    }]


def test_bounded_reader_discards_multi_megabyte_remainder_in_fixed_chunks():
    class ChunkOnlyBytesIO(io.BytesIO):
        def __init__(self, value):
            super().__init__(value)
            self.max_requested = 0

        def read(self, size=-1):
            self.max_requested = max(self.max_requested, size)
            assert 0 < size <= transcript_audit._READ_CHUNK_BYTES
            return super().read(size)

        def readline(self, *_args, **_kwargs):
            raise AssertionError("bounded reader must not use accumulating readline")

    oversized_bytes = 5 * 1024 * 1024
    handle = ChunkOnlyBytesIO(b"x" * oversized_bytes + b"\x00\n{}\n")

    lines = list(transcript_audit.iter_bounded_binary_lines(handle, 1024))

    assert len(lines) == 2
    assert len(lines[0].prefix) == 1024
    assert lines[0].byte_count == oversized_bytes + 2
    assert lines[0].truncated is True
    assert lines[0].nul_count == 1
    assert lines[1].prefix == b"{}\n"
    assert handle.max_requested == transcript_audit._READ_CHUNK_BYTES


def test_iterative_cycle_scan_handles_ten_thousand_node_chain():
    node_count = 10_000
    uuid_values = {
        "node-{:05d}".format(index): 1 for index in range(node_count)
    }
    parent_edges = [
        ("node-{:05d}".format(index - 1), "node-{:05d}".format(index))
        for index in range(1, node_count)
    ]

    facts = transcript_audit._graph_facts(
        uuid_values,
        parent_edges,
        {"node-00000"},
        parent_reference_count=len(parent_edges),
    )

    assert facts["cycle_count"] == 0
    assert facts["cycle_node_count"] == 0
    assert facts["weak_components"] == 1
    assert facts["reachable_from_explicit_roots"] == node_count
    assert facts["unrooted_nodes"] == 0


def test_missing_parent_count_is_per_edge_occurrence():
    facts = transcript_audit._graph_facts(
        {"u1": 1, "u2": 1},
        [("missing", "u1"), ("missing", "u2"), ("missing", "u2")],
        set(),
        parent_reference_count=3,
    )

    assert facts["missing_parent_references"] == 3


def test_parent_reference_storage_is_globally_bounded(tmp_path):
    path = tmp_path / "parent-cap.jsonl"
    _write(
        path,
        b"\n".join(_record("u1", "missing") for _index in range(5)),
    )

    result = transcript_audit.audit_transcript_paths(
        [path], max_nodes_per_file=2
    )
    file_result = result["files"][0]

    assert result["status"] == "bounded"
    assert file_result["parent_reference_count"] == 5
    assert file_result["parent_references_retained"] == 2
    assert file_result["parent_references_truncated"] == 3
    assert file_result["missing_parent_references"] == 2
    assert {finding["kind"] for finding in result["findings"]} >= {
        "parent_references_truncated"
    }


def test_first_record_field_uses_bounded_reader_and_continues(tmp_path):
    path = _write(
        tmp_path / "identity.jsonl",
        b"x" * (2 * 1024 * 1024) + b"\n{\"cwd\":\"safe-value\"}\n",
    )

    read = transcript_audit.read_first_record_field(
        path, "cwd", max_line_bytes=1024
    )

    assert read == {"value": "safe-value", "bounded": True, "error": None}
    assert transcript_audit.first_record_field(
        path, "cwd", max_line_bytes=1024
    ) == "safe-value"


def test_first_record_field_treats_integer_digit_limit_as_partial_parse_anomaly(
    tmp_path,
):
    path = _write(
        tmp_path / "projects" / "slug" / "identity.jsonl",
        '{"oversized": ' + ("1" * 5000) + "}\n",
    )

    read = transcript_audit.read_first_record_field(path, "cwd")
    result = audit_identity(str(tmp_path / "projects"))

    assert read == {"value": None, "bounded": False, "error": "parse_failed"}
    assert result["status"] == "partial"
    assert result["findings"] == []
    assert result["errors"] == [{
        "code": "transcript_read_failed",
        "reference": "transcript-0001",
    }]


def test_explicit_integrity_cli_is_private_and_deterministic(tmp_path, capsys, monkeypatch):
    private_root = tmp_path / "PrivateName"
    path = _write(private_root / "projects" / "slug" / "id.jsonl", b"{}\n")
    monkeypatch.setenv("USERPROFILE", str(private_root))

    assert integrity_main(["--transcript", str(path), "--json"]) == 0
    first = capsys.readouterr().out
    assert str(path) not in first
    assert "PrivateName" not in first

    assert integrity_main([
        "--transcript", str(path), "--json", "--include-paths", "--details"
    ]) == 0
    second = capsys.readouterr().out
    assert "PrivateName" not in second
    assert "%USERPROFILE%" in second

    assert integrity_main([
        "--transcript", str(path), "--json", "--include-paths", "--details"
    ]) == 0
    third = capsys.readouterr().out
    assert second == third


def test_identity_reports_duplicate_paths_slug_collision_and_metadata_ambiguity(tmp_path):
    state = tmp_path / "state"
    sid = "11111111-1111-1111-1111-111111111111"
    _write(
        state / "projects" / "first" / f"{sid}.jsonl",
        json.dumps({"cwd": "C:/project.a"}) + "\n",
    )
    _write(
        state / "projects" / "second" / f"{sid}.jsonl",
        json.dumps({"cwd": "C:/project-a"}) + "\n",
    )
    _write(
        state / "appdata" / "Claude" / "claude-code-sessions" / "account" / "org" / "local_1.json",
        json.dumps({"cliSessionId": sid, "cwd": "C:/unrelated"}),
    )

    result = audit_identity(
        str(state / "projects"),
        str(state / "appdata" / "Claude"),
        ["first"],
    )
    summary = result["summary"]

    assert summary["physical_transcript_count"] == 2
    assert summary["unique_session_id_count"] == 1
    assert summary["duplicate_session_id_group_count"] == 1
    assert summary["observed_slug_collision_group_count"] == 1
    assert summary["metadata_ambiguous_transcript_count"] == 1
    assert summary["cwd_slug_mismatch_count"] == 1
    assert summary["explicit_cwd_count"] == 1
    assert summary["explicit_cwd_mismatch_count"] == 0
    assert all(sid not in json.dumps(item) for item in result["findings"])


def test_inaccessible_slug_discovery_is_partial_and_exit_zero(
    tmp_path, capsys, monkeypatch
):
    projects = tmp_path / "projects"
    blocked = projects / "blocked"
    blocked.mkdir(parents=True)
    real_scandir = os.scandir

    def guarded_scandir(path):
        if os.path.abspath(os.fspath(path)) == os.path.abspath(blocked):
            raise PermissionError("fixture access denied")
        return real_scandir(path)

    monkeypatch.setattr(audit_transcript_integrity.os, "scandir", guarded_scandir)

    assert integrity_main(["--projects-dir", str(projects), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "partial"
    assert payload["summary"]["scan_error_count"] == 1
    assert payload["errors"] == [
        {"code": "slug_list_failed", "reference": "scan-entry-0001"}
    ]
    assert "blocked" not in json.dumps(payload)


def test_identity_discovery_reports_inaccessible_slug_as_partial(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    blocked = projects / "blocked"
    blocked.mkdir(parents=True)
    real_scandir = os.scandir

    def guarded_scandir(path):
        if os.path.abspath(os.fspath(path)) == os.path.abspath(blocked):
            raise PermissionError("fixture access denied")
        return real_scandir(path)

    monkeypatch.setattr(audit_transcript_identity.os, "scandir", guarded_scandir)

    result = audit_identity(str(projects))

    assert result["status"] == "partial"
    assert result["summary"]["scan_error_count"] == 1
    assert result["errors"] == [
        {"code": "slug_list_failed", "reference": "scan-entry-0001"}
    ]


def test_identity_partial_project_probe_emits_only_envelope_errors_and_counts(
    tmp_path, monkeypatch, capsys
):
    state = tmp_path / "private-state"
    sid = "55555555-5555-5555-5555-555555555555"
    _write(
        state / "projects" / "visible" / f"{sid}.jsonl",
        json.dumps({"cwd": "C:/actual"}) + "\n",
    )
    blocked = state / "projects" / "blocked"
    _write(
        blocked / f"{sid}.jsonl",
        json.dumps({"cwd": "C:/wrong/.claude/worktrees/feature"}) + "\n",
    )
    _write(
        state / "appdata" / "Claude" / "claude-code-sessions"
        / "account" / "organisation" / "local_one.json",
        json.dumps({
            "cliSessionId": sid,
            "cwd": "C:/wrong/.claude/worktrees/feature",
        }),
    )
    real_scandir = os.scandir

    def guarded_scandir(path):
        if os.path.abspath(os.fspath(path)) == os.path.abspath(blocked):
            raise PermissionError("fixture access denied")
        return real_scandir(path)

    monkeypatch.setattr(audit_transcript_identity.os, "scandir", guarded_scandir)

    assert audit_transcript_identity.main([
        "--state", str(state),
        "--cwd", "C:/unmatched",
        "--json", "--details", "--include-paths",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload == {
        "errors": [{
            "code": "slug_list_failed",
            "reference": "scan-entry-0001",
        }],
        "read_only": True,
        "schema_version": "transcript-identity-audit-v1",
        "status": "partial",
        "summary": {
            "bounded_field_read_count": 0,
            "cwd_slug_mismatch_count": 0,
            "duplicate_session_id_group_count": 0,
            "explicit_cwd_count": 1,
            "explicit_cwd_mismatch_count": 0,
            "metadata_ambiguous_transcript_count": 0,
            "observed_slug_collision_group_count": 0,
            "physical_transcript_count": 1,
            "resolved_path_split_group_count": 0,
            "scan_error_count": 1,
            "unique_session_id_count": 1,
            "worktree_key_mismatch_candidate_count": 0,
        },
    }
    assert "private-state" not in json.dumps(payload)


@pytest.mark.parametrize(
    ("blocked_level", "expected_code"),
    [
        ("account", "metadata_account_list_failed"),
        ("organisation", "metadata_organisation_list_failed"),
    ],
)
def test_identity_metadata_discovery_reports_inaccessible_directories_as_partial(
    tmp_path, monkeypatch, blocked_level, expected_code
):
    projects = tmp_path / "projects"
    _write(projects / "slug" / "identity.jsonl", b"{}\n")
    account = (
        tmp_path / "appdata" / "Claude" / "claude-code-sessions"
        / "private-account"
    )
    organisation = account / "private-organisation"
    organisation.mkdir(parents=True)
    blocked = account if blocked_level == "account" else organisation
    real_scandir = os.scandir

    def guarded_scandir(path):
        if os.path.abspath(os.fspath(path)) == os.path.abspath(blocked):
            raise PermissionError("fixture access denied")
        return real_scandir(path)

    monkeypatch.setattr(audit_transcript_identity.os, "scandir", guarded_scandir)

    result = audit_identity(
        str(projects), str(tmp_path / "appdata" / "Claude")
    )

    assert result["status"] == "partial"
    assert result["summary"]["scan_error_count"] == 1
    assert result["errors"] == [{
        "code": expected_code,
        "reference": "metadata-scan-entry-0001",
    }]
    assert "private-account" not in json.dumps(result["errors"])
    assert "private-organisation" not in json.dumps(result["errors"])


def test_unavailable_projects_root_preserves_exit_two(tmp_path, capsys):
    missing = tmp_path / "missing-projects"

    assert integrity_main(["--projects-dir", str(missing), "--json"]) == 2

    captured = capsys.readouterr()
    assert "projects scan root is unavailable" in captured.err
    assert captured.out == ""


def test_sweep_reports_mismatched_slug_without_name_error(
    tmp_path, capsys, monkeypatch
):
    state = tmp_path / "state"
    sid = "33333333-3333-3333-3333-333333333333"
    cwd = r"C:\repo\.claude\worktrees\feature"
    _write(
        state / "projects" / "different-slug" / f"{sid}.jsonl",
        b"{}\n",
    )
    _write(
        state / "appdata" / "Claude" / "claude-code-sessions"
        / "account" / "org" / "local_1.json",
        json.dumps({
            "sessionId": "local_1",
            "cliSessionId": sid,
            "cwd": cwd,
            "createdAt": 1,
            "model": "fixture",
            "title": "fixture",
        }),
    )
    monkeypatch.setattr(
        sys, "argv", ["sweep_junction_canonical_cwds.py", "--state", str(state)]
    )

    assert sweep_junction_canonical_cwds.main() == 0
    output = capsys.readouterr().out

    assert "JSONL at a different slug:             1" in output
    assert "JSONL_AT_OTHER_SLUG" in output
    assert "actual=different-slug" in output


def test_audits_do_not_change_fixture_fingerprint(tmp_path):
    state = tmp_path / "state"
    _write(state / "projects" / "slug" / "id.jsonl", b"{}\n")
    before = _fingerprint(state)

    assert integrity_main(["--state", str(state), "--json"]) == 0
    identity = audit_identity(str(state / "projects"), str(state / "appdata" / "Claude"))
    assert identity["read_only"] is True

    assert before == _fingerprint(state)


def test_duplicate_index_fails_closed_and_inventory_is_lossless(tmp_path):
    sid = "22222222-2222-2222-2222-222222222222"
    first = _write(tmp_path / "a" / f"{sid}.jsonl", b"{}\n")
    second = _write(tmp_path / "b" / f"{sid}.jsonl", b"{}\n")

    inventory = transcript_files.build_transcript_path_inventory(str(tmp_path))
    assert inventory.physical_count == 2
    assert inventory.by_session_id[sid] == tuple(sorted((str(first), str(second))))
    with pytest.raises(transcript_files.DuplicateTranscriptIdError):
        transcript_files.build_transcript_index(str(tmp_path))


def test_absent_projects_directory_is_a_complete_empty_inventory(tmp_path):
    inventory = transcript_files.build_transcript_path_inventory(
        str(tmp_path / "projects")
    )

    assert inventory.status == "complete"
    assert inventory.physical_count == 0
    assert inventory.by_session_id == {}
    assert inventory.errors == ()


def test_absent_optional_metadata_root_is_a_complete_empty_inventory(tmp_path):
    appdata = tmp_path / "appdata" / "Claude"

    inventory = session_metadata.build_metadata_path_inventory(str(appdata))

    assert inventory.status == "complete"
    assert inventory.physical_file_count == 0
    assert inventory.records == ()
    assert inventory.directories == ()
    assert inventory.errors == ()
    assert repair_session_metadata.index_metadata(str(appdata)) == ({}, [])
    with pytest.raises(synth_session_metadata.MetadataDestinationError) as exc_info:
        synth_session_metadata._find_meta_dir(str(appdata), inventory)
    assert exc_info.value.directory_count == 0


def test_synth_destination_requires_exactly_one_complete_metadata_root(tmp_path):
    appdata = tmp_path / "appdata" / "Claude"
    first = appdata / "claude-code-sessions" / "account-a" / "org-a"
    second = appdata / "claude-code-sessions" / "account-b" / "org-b"
    first.mkdir(parents=True)

    one = session_metadata.build_metadata_path_inventory(str(appdata))
    assert synth_session_metadata._find_meta_dir(str(appdata), one) == str(first)

    second.mkdir(parents=True)
    multiple = session_metadata.build_metadata_path_inventory(str(appdata))
    assert multiple.status == "complete"
    with pytest.raises(synth_session_metadata.MetadataDestinationError) as exc_info:
        synth_session_metadata._find_meta_dir(str(appdata), multiple)
    assert exc_info.value.directory_count == 2


def test_compatibility_iterator_raises_before_yielding_partial_inventory(
    tmp_path, monkeypatch
):
    sid = "66666666-6666-6666-6666-666666666666"
    projects = tmp_path / "projects"
    _write(projects / "visible" / f"{sid}.jsonl", b"{}\n")
    blocked = projects / "blocked"
    _write(blocked / f"{sid}.jsonl", b"{}\n")
    real_scandir = os.scandir

    def guarded_scandir(path):
        if os.path.abspath(os.fspath(path)) == os.path.abspath(blocked):
            raise PermissionError("fixture access denied")
        return real_scandir(path)

    monkeypatch.setattr(transcript_files.os, "scandir", guarded_scandir)

    iterator = transcript_files.iter_transcript_paths(str(projects))
    with pytest.raises(transcript_files.IncompleteTranscriptInventoryError):
        next(iterator)


@pytest.mark.parametrize(
    ("blocked_boundary", "expected_code"),
    [
        ("account", "metadata_account_list_failed"),
        ("organisation", "metadata_organisation_list_failed"),
        ("file", "metadata_file_read_failed"),
    ],
)
def test_metadata_partial_inventory_blocks_every_metadata_mutator_selector(
    tmp_path, monkeypatch, blocked_boundary, expected_code
):
    state = tmp_path / "state"
    appdata = state / "appdata" / "Claude"
    sessions_root = appdata / "claude-code-sessions"
    visible_file = sessions_root / "visible-account" / "visible-org" / "local_visible.json"
    target_sid = "77777777-7777-7777-7777-777777777777"
    _write(visible_file, json.dumps({
        "sessionId": "visible",
        "cliSessionId": "88888888-8888-8888-8888-888888888888",
        "cwd": "C:/visible",
    }))

    if blocked_boundary == "account":
        blocked = sessions_root / "blocked-account"
        hidden_file = blocked / "hidden-org" / "local_hidden.json"
    else:
        blocked = sessions_root / "visible-account" / "blocked-org"
        hidden_file = blocked / "local_hidden.json"
    _write(hidden_file, json.dumps({
        "sessionId": "hidden",
        "cliSessionId": target_sid,
        "cwd": "C:/hidden/.claude/worktrees/feature",
        "branch": "claude/feature",
    }))
    projects = state / "projects"
    _write(projects / "slug" / f"{target_sid}.jsonl", b"{}\n")
    before = _fingerprint(state)

    if blocked_boundary == "file":
        real_open = open

        def guarded_open(path, *args, **kwargs):
            if os.path.abspath(os.fspath(path)) == os.path.abspath(hidden_file):
                raise PermissionError("fixture access denied")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(session_metadata, "open", guarded_open, raising=False)
    else:
        real_scandir = os.scandir

        def guarded_scandir(path):
            if os.path.abspath(os.fspath(path)) == os.path.abspath(blocked):
                raise PermissionError("fixture access denied")
            return real_scandir(path)

        monkeypatch.setattr(session_metadata.os, "scandir", guarded_scandir)

    inventory = session_metadata.build_metadata_path_inventory(str(appdata))
    assert inventory.status == "partial"
    assert [record.path for record in inventory.records] == [str(visible_file)]
    assert inventory.errors == (
        session_metadata.MetadataInventoryError(
            reference="metadata-scan-entry-0001", code=expected_code
        ),
    )

    selectors = (
        lambda: repair_session_metadata.index_metadata(str(appdata)),
        lambda: synth_session_metadata._find_orphan_jsonls(
            str(appdata), str(projects)
        ),
        lambda: repoint_session_to_jsonl._find_mismatches(
            str(appdata), str(projects)
        ),
        lambda: cleanup_synth_duplicates.index_metadata(str(appdata)),
        lambda: rewrite_metadata_cwd._find_targets(str(appdata), "c:/hidden"),
        lambda: recover_deleted_branches_worktrees._gather_broken_sessions(
            str(appdata)
        ),
        lambda: restore_from_vss._fx_scan_sessions(str(state)),
    )
    for selector in selectors:
        with pytest.raises(session_metadata.IncompleteMetadataInventoryError):
            selector()

    if blocked_boundary == "file":
        monkeypatch.setattr(session_metadata, "open", real_open, raising=False)
    else:
        monkeypatch.setattr(session_metadata.os, "scandir", real_scandir)
    assert _fingerprint(state) == before


def test_metadata_integer_digit_limit_is_opaque_partial_and_blocks_selectors(
    tmp_path,
):
    state = tmp_path / "state"
    appdata = state / "appdata" / "Claude"
    metadata = (
        appdata / "claude-code-sessions" / "account" / "organisation"
        / "local_oversized.json"
    )
    projects = state / "projects"
    sid = "99999999-9999-9999-9999-999999999999"
    _write(metadata, '{"oversized": ' + ("1" * 5000) + "}")
    _write(projects / "slug" / f"{sid}.jsonl", b"{}\n")
    before = _fingerprint(state)

    inventory = session_metadata.build_metadata_path_inventory(str(appdata))

    assert inventory.status == "partial"
    assert inventory.records == ()
    assert inventory.errors == (
        session_metadata.MetadataInventoryError(
            reference="metadata-scan-entry-0001",
            code="metadata_file_parse_failed",
        ),
    )
    selectors = (
        lambda: repair_session_metadata.index_metadata(str(appdata)),
        lambda: synth_session_metadata._find_orphan_jsonls(
            str(appdata), str(projects)
        ),
        lambda: repoint_session_to_jsonl._find_mismatches(
            str(appdata), str(projects)
        ),
        lambda: cleanup_synth_duplicates.index_metadata(str(appdata)),
        lambda: rewrite_metadata_cwd._find_targets(str(appdata), "c:/hidden"),
        lambda: recover_deleted_branches_worktrees._gather_broken_sessions(
            str(appdata)
        ),
        lambda: restore_from_vss._fx_scan_sessions(str(state)),
    )
    for selector in selectors:
        with pytest.raises(session_metadata.IncompleteMetadataInventoryError):
            selector()
    assert _fingerprint(state) == before


def test_partial_metadata_suppresses_cross_store_diagnosis_and_routing(
    tmp_path, monkeypatch, capsys
):
    state = tmp_path / "state"
    appdata = state / "appdata" / "Claude"
    sessions_root = appdata / "claude-code-sessions" / "account"
    visible = sessions_root / "visible" / "local_visible.json"
    blocked = sessions_root / "blocked"
    dangling_sid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    orphan_sid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    _write(visible, json.dumps({
        "sessionId": "local_visible",
        "cliSessionId": dangling_sid,
        "cwd": "C:/visible",
        "createdAt": 1,
        "model": "fixture",
        "title": "fixture",
    }))
    _write(blocked / "local_hidden.json", b"{}")
    _write(state / "projects" / "orphan" / f"{orphan_sid}.jsonl", b"{}\n")
    real_scandir = os.scandir

    def guarded_scandir(path):
        if os.path.abspath(os.fspath(path)) == os.path.abspath(blocked):
            raise PermissionError("fixture access denied")
        return real_scandir(path)

    monkeypatch.setattr(session_metadata.os, "scandir", guarded_scandir)

    snapshot = session_state.build_snapshot(
        str(appdata), str(state / "projects"), fixture_mode=True
    )

    assert "metadata_inventory_status" not in snapshot
    assert snapshot["schema_version"] == "unrecognised"
    assert snapshot["metadata_dangling_cli_count"] == 0
    assert snapshot["jsonl_orphan_count"] == 0
    assert snapshot["cwd_junction_mismatch_count"] == 0
    assert snapshot["cwd_slug_mismatch_count"] == 0
    assert snapshot["truncated_jsonl_count"] == 0
    assert "desktop_session_pairs" not in snapshot
    assert "account_uuid_rotation_count" not in snapshot

    monkeypatch.setattr(sys, "argv", [
        diagnose.__file__, "--state", str(state), "--json",
    ])
    diagnose.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_mismatch"] is True
    assert payload["matched_problems"] == []


@pytest.mark.parametrize(
    ("module", "refusal"),
    [
        (
            repair_session_metadata,
            "REFUSED: Metadata inventory is partial; no repair matches were selected.",
        ),
        (
            synth_session_metadata,
            "REFUSED: Metadata inventory is partial; no orphan inference was made.",
        ),
    ],
)
def test_named_metadata_mutator_clis_refuse_before_dry_run_inference(
    tmp_path, monkeypatch, capsys, module, refusal
):
    state = tmp_path / "state"
    blocked = (
        state / "appdata" / "Claude" / "claude-code-sessions"
        / "blocked-account"
    )
    _write(blocked / "organisation" / "local_hidden.json", b"{}")
    before = _fingerprint(state)
    real_scandir = os.scandir

    def guarded_scandir(path):
        if os.path.abspath(os.fspath(path)) == os.path.abspath(blocked):
            raise PermissionError("fixture access denied")
        return real_scandir(path)

    monkeypatch.setattr(session_metadata.os, "scandir", guarded_scandir)
    monkeypatch.setattr(sys, "argv", [
        module.__file__,
        "--state", str(state),
        "--force-with-diagnosis-id", "audit-only",
    ])

    with pytest.raises(SystemExit) as exit_info:
        module.main()

    assert exit_info.value.code == 3
    output = capsys.readouterr().out
    assert refusal in output
    assert "metadata_account_list_failed" in output
    assert "Metadata files:" not in output
    assert "Orphan JSONLs" not in output
    monkeypatch.setattr(session_metadata.os, "scandir", real_scandir)
    assert _fingerprint(state) == before


def test_partial_inventory_prevents_hidden_duplicate_from_becoming_unique(
    tmp_path, monkeypatch, capsys
):
    sid = "44444444-4444-4444-4444-444444444444"
    projects = tmp_path / "projects"
    visible = _write(projects / "visible" / f"{sid}.jsonl", b"{}\n")
    hidden = _write(projects / "hidden" / f"{sid}.jsonl", b"{}\n")
    metadata = (
        tmp_path / "appdata" / "Claude" / "claude-code-sessions"
        / "account" / "organisation" / "local_one.json"
    )
    _write(metadata, json.dumps({
        "sessionId": "local_one",
        "cliSessionId": sid,
        "cwd": "visible",
        "createdAt": 1,
        "model": "fixture",
        "title": "fixture",
    }))
    before = _fingerprint(tmp_path)

    complete_snapshot = session_state.build_snapshot(
        str(tmp_path / "appdata" / "Claude"), str(projects), fixture_mode=True
    )
    real_scandir = os.scandir

    def guarded_scandir(path):
        if os.path.abspath(os.fspath(path)) == os.path.abspath(hidden.parent):
            raise PermissionError("fixture access denied")
        return real_scandir(path)

    monkeypatch.setattr(transcript_files.os, "scandir", guarded_scandir)

    inventory = transcript_files.build_transcript_path_inventory(str(projects))
    assert inventory.status == "partial"
    assert inventory.is_complete is False
    assert inventory.by_session_id[sid] == (str(visible),)
    assert inventory.errors == (
        transcript_files.TranscriptInventoryError(
            reference="inventory-entry-0001", code="slug_list_failed"
        ),
    )

    with pytest.raises(transcript_files.IncompleteTranscriptInventoryError):
        transcript_files.build_transcript_index(str(projects))
    with pytest.raises(transcript_files.IncompleteTranscriptInventoryError):
        list(transcript_files.iter_transcript_paths(str(projects)))
    with pytest.raises(transcript_files.IncompleteTranscriptInventoryError):
        repair_session_metadata.index_jsonls(str(projects))
    with pytest.raises(transcript_files.IncompleteTranscriptInventoryError):
        repoint_session_to_jsonl._find_mismatches(
            str(tmp_path / "appdata" / "Claude"), str(projects)
        )
    with pytest.raises(transcript_files.IncompleteTranscriptInventoryError):
        synth_session_metadata._find_orphan_jsonls(
            str(tmp_path / "appdata" / "Claude"), str(projects)
        )

    partial_snapshot = session_state.build_snapshot(
        str(tmp_path / "appdata" / "Claude"), str(projects), fixture_mode=True
    )
    assert set(partial_snapshot) == set(complete_snapshot)
    assert "transcript_inventory_status" not in partial_snapshot
    assert partial_snapshot["metadata_dangling_cli_count"] == 0
    assert partial_snapshot["cwd_slug_mismatch_count"] == 0

    monkeypatch.setattr(
        recover_vscode_sessions,
        "_find_claude_dbs",
        lambda _workspace: [("fixture.vscdb", "workspace", [])],
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "recover_vscode_sessions.py",
            "--projects-dir", str(projects),
            "--workspace-dir", str(tmp_path / "workspace"),
        ],
    )
    assert recover_vscode_sessions.main() == 3
    assert "REFUSED: Transcript inventory is partial" in capsys.readouterr().out
    monkeypatch.setattr(transcript_files.os, "scandir", real_scandir)
    assert _fingerprint(tmp_path) == before
