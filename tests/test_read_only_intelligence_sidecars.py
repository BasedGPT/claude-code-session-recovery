"""Focused tests for bounded, read-only intelligence sidecars."""

import hashlib
import json
import os
import shutil
import sqlite3
import sys

import pytest

SESSIONS = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "tools", "sessions"
)
if SESSIONS not in sys.path:
    sys.path.insert(0, SESSIONS)

import audit_session_lineage as lineage  # noqa: E402
import audit_vscode_session_surfaces as vscode  # noqa: E402
import inventory_local_agent_sessions as local_agents  # noqa: E402
import sidecar_common  # noqa: E402
from sidecar_common import ScanState, iter_bounded_jsonl  # noqa: E402


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def _lineage_roots(tmp_path):
    appdata = tmp_path / "Private User" / "appdata" / "Claude"
    projects = tmp_path / "Private User" / "projects"
    metadata = appdata / "claude-code-sessions" / "account-secret" / "org-secret"
    return appdata, projects, metadata


def test_lineage_uses_only_allowed_evidence_and_opaque_defaults(tmp_path):
    appdata, projects, metadata = _lineage_roots(tmp_path)
    s1 = "11111111-1111-4111-8111-111111111111"
    s2 = "22222222-2222-4222-8222-222222222222"
    s3 = "33333333-3333-4333-8333-333333333333"
    s4 = "44444444-4444-4444-8444-444444444444"
    s5 = "55555555-5555-4555-8555-555555555555"
    shared = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    private_title = "Private Acquisition Plan"
    _write_json(metadata / "local_one.json", {
        "cliSessionId": s1, "createdAt": 1000, "title": "First title"
    })
    _write_json(metadata / "local_two.json", {
        "cliSessionId": s2, "createdAt": 2000, "title": "Second title"
    })
    _write_json(metadata / "local_three.json", {
        "cliSessionId": s3, "createdAt": 3000, "title": private_title
    })
    _write_json(metadata / "local_four.json", {
        "cliSessionId": s4, "createdAt": 4000, "title": private_title,
        "forkedFrom": s1,
    })
    _write_json(metadata / "local_five.json", {
        "cliSessionId": s5, "createdAt": 5000, "title": "Unique title"
    })
    _write_jsonl(projects / "secret-slug" / f"{s1}.jsonl", [
        {"uuid": shared, "timestamp": "2026-01-01T00:00:00Z",
         "message": {"content": "private prompt one"}},
    ])
    _write_jsonl(projects / "secret-slug" / f"{s2}.jsonl", [
        {"uuid": shared, "timestamp": "2026-01-02T00:00:00Z",
         "message": {"content": "private prompt two"}},
    ])

    result = lineage.audit_lineage(str(appdata), str(projects))

    assert result["status"] == "complete"
    assert result["classification_counts"] == {
        "explicit_lineage": 2,
        "shared_history_candidate": 1,
        "title_only_ambiguous": 1,
        "insufficient_evidence": 1,
    }
    rendered = json.dumps(result)
    for private_value in (
        str(tmp_path), "Private User", "secret-slug", private_title,
        "private prompt one", s1, s2, s3, s4, s5, shared,
    ):
        assert private_value not in rendered
    assert all(
        finding["classification"] in lineage.CLASSIFICATIONS
        for finding in result["findings"]
    )
    assert all("paths" not in finding for finding in result["findings"])


def test_lineage_include_paths_is_explicit_and_temp_index_is_removed(tmp_path):
    appdata, projects, metadata = _lineage_roots(tmp_path)
    session_id = "11111111-1111-4111-8111-111111111111"
    _write_json(metadata / "local_one.json", {"cliSessionId": session_id})
    transcript = projects / "slug" / f"{session_id}.jsonl"
    _write_jsonl(transcript, [{"uuid": session_id}])
    temp_parent = tmp_path / "sqlite-temp"
    temp_parent.mkdir()

    result = lineage.audit_lineage(
        str(appdata), str(projects), include_paths=True,
        temp_parent=str(temp_parent),
    )

    paths = result["findings"][0]["paths"]
    assert str(transcript) in paths
    assert list(temp_parent.iterdir()) == []


def test_lineage_preserves_distinct_metadata_sessions_with_shared_cli_id(tmp_path):
    appdata, projects, metadata = _lineage_roots(tmp_path)
    cli_session_id = "11111111-1111-4111-8111-111111111111"
    shared_node = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    _write_json(metadata / "local_one.json", {
        "sessionId": "desktop-session-one",
        "cliSessionId": cli_session_id,
    })
    _write_json(metadata / "local_two.json", {
        "sessionId": "desktop-session-two",
        "cliSessionId": cli_session_id,
    })
    _write_jsonl(
        projects / "slug" / f"{cli_session_id}.jsonl",
        [{"uuid": shared_node}],
    )

    result = lineage.audit_lineage(str(appdata), str(projects))

    assert result["status"] == "complete"
    assert result["session_count"] == 2
    assert result["reported_session_count"] == 2
    assert len({finding["session"] for finding in result["findings"]}) == 2
    assert result["classification_counts"] == {
        "explicit_lineage": 0,
        "shared_history_candidate": 2,
        "title_only_ambiguous": 0,
        "insufficient_evidence": 0,
    }


def test_bounded_jsonl_drains_oversized_line_without_retaining_content(tmp_path):
    path = tmp_path / "oversized.jsonl"
    path.write_bytes(b'{"private":"' + b"x" * 100000 + b'"}\n{"uuid":"ok"}\n')
    state = ScanState()

    records = list(iter_bounded_jsonl(
        str(path), state, subject="opaque-file", max_lines=10,
        max_line_bytes=64, max_file_bytes=200000,
    ))

    assert records == [{"uuid": "ok"}]
    assert state.partial is True
    assert state.errors == [{"code": "line_byte_cap_reached", "subject": "opaque-file"}]


def test_lineage_max_files_same_title_suppresses_all_inferences(tmp_path):
    appdata, projects, metadata = _lineage_roots(tmp_path)
    private_title = "Private shared title"
    _write_json(metadata / "local_one.json", {
        "sessionId": "desktop-one", "title": private_title,
    })
    _write_json(metadata / "local_two.json", {
        "sessionId": "desktop-two", "title": private_title,
    })

    result = lineage.audit_lineage(
        str(appdata), str(projects), max_files=1
    )

    assert result["status"] == "partial"
    assert result["errors"] == [{"code": "file_cap_reached"}]
    assert result["files_scanned"] == 1
    assert result["transcripts_scanned"] == 0
    assert result["records_scanned"] == 0
    assert result["session_count"] is None
    assert result["relationship_results_suppressed"] is True
    assert result["relationship_pair_operations"] is None
    assert result["shared_relationship_count"] is None
    assert result["reported_session_count"] is None
    assert result["classification_counts"] is None
    assert result["findings"] == []
    assert private_title not in json.dumps(result)


def test_lineage_parse_error_suppresses_all_inferences(tmp_path):
    appdata, projects, metadata = _lineage_roots(tmp_path)
    private_title = "Private shared title"
    _write_json(metadata / "local_one.json", {
        "sessionId": "desktop-one", "title": private_title,
    })
    _write_json(metadata / "local_two.json", {
        "sessionId": "desktop-two", "title": private_title,
    })
    invalid = metadata / "local_three.json"
    invalid.write_text("{private invalid metadata", encoding="utf-8")

    result = lineage.audit_lineage(str(appdata), str(projects))

    assert result["status"] == "partial"
    assert result["errors"][0]["code"] == "record_invalid"
    assert result["files_scanned"] == 3
    assert result["transcripts_scanned"] == 0
    assert result["records_scanned"] == 0
    assert result["bytes_scanned"] > 0
    assert result["session_count"] is None
    assert result["relationship_results_suppressed"] is True
    assert result["relationship_pair_operations"] is None
    assert result["shared_relationship_count"] is None
    assert result["reported_session_count"] is None
    assert result["classification_counts"] is None
    assert result["findings"] == []
    rendered = json.dumps(result)
    assert private_title not in rendered
    assert "private invalid metadata" not in rendered


def test_lineage_bounds_total_bytes_and_relationship_materialisation(tmp_path):
    appdata, projects, metadata = _lineage_roots(tmp_path)
    sessions = [
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "33333333-3333-4333-8333-333333333333",
    ]
    shared = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    for index, session_id in enumerate(sessions):
        _write_json(metadata / f"local_{index}.json", {"cliSessionId": session_id})
        _write_jsonl(projects / "slug" / f"{session_id}.jsonl", [{"uuid": shared}])

    relationship_result = lineage.audit_lineage(
        str(appdata), str(projects), max_relationships=1
    )
    byte_result = lineage.audit_lineage(
        str(appdata), str(projects), max_total_bytes=1
    )

    assert relationship_result["status"] == "partial"
    assert any(
        error["code"] == "relationship_cap_reached"
        for error in relationship_result["errors"]
    )
    assert relationship_result["relationship_results_suppressed"] is True
    assert relationship_result["session_count"] is None
    assert relationship_result["relationship_pair_operations"] is None
    assert relationship_result["shared_relationship_count"] is None
    assert relationship_result["reported_session_count"] is None
    assert relationship_result["classification_counts"] is None
    assert relationship_result["findings"] == []
    assert byte_result["status"] == "partial"
    assert any(
        error["code"] == "total_byte_cap_reached"
        for error in byte_result["errors"]
    )
    assert byte_result["session_count"] is None
    assert byte_result["relationship_results_suppressed"] is True
    assert byte_result["relationship_pair_operations"] is None
    assert byte_result["shared_relationship_count"] is None
    assert byte_result["reported_session_count"] is None
    assert byte_result["classification_counts"] is None
    assert byte_result["findings"] == []


def test_lineage_many_sessions_one_uuid_stops_before_cross_product(
    tmp_path, monkeypatch
):
    appdata, projects, _metadata = _lineage_roots(tmp_path)
    shared = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    for index in range(80):
        session_id = "00000000-0000-4000-8000-{:012d}".format(index)
        _write_jsonl(projects / "slug" / f"{session_id}.jsonl", [{"uuid": shared}])

    statements = []
    real_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(lineage.sqlite3, "connect", traced_connect)
    result = lineage.audit_lineage(
        str(appdata), str(projects), max_relationships=7
    )

    assert result["status"] == "partial"
    assert result["relationship_results_suppressed"] is True
    assert result["session_count"] is None
    assert result["relationship_pair_operations"] is None
    assert result["shared_relationship_count"] is None
    assert result["reported_session_count"] is None
    assert result["classification_counts"] is None
    assert result["findings"] == []
    assert any(
        error["code"] == "relationship_cap_reached"
        for error in result["errors"]
    )
    relationship_inserts = [
        statement for statement in statements
        if statement.startswith("INSERT INTO relationships(")
    ]
    assert len(relationship_inserts) == 7
    assert not any("JOIN nodes" in statement for statement in statements)


def test_lineage_connect_failure_is_structured_partial_and_exit_two(
    tmp_path, monkeypatch, capsys
):
    appdata, projects, _metadata = _lineage_roots(tmp_path)
    appdata.mkdir(parents=True)
    projects.mkdir(parents=True)
    temp_parent = tmp_path / "lineage-temp"
    temp_parent.mkdir()

    def fail_connect(*_args, **_kwargs):
        raise sqlite3.OperationalError("private connect failure detail")

    monkeypatch.setattr(lineage.sqlite3, "connect", fail_connect)
    result = lineage.audit_lineage(
        str(appdata),
        str(projects),
        temp_parent=str(temp_parent),
    )

    assert result["status"] == "partial"
    assert result["errors"] == [{"code": "temporary_index_error"}]
    assert result["files_scanned"] == 0
    assert result["transcripts_scanned"] == 0
    assert result["records_scanned"] == 0
    assert result["bytes_scanned"] == 0
    assert result["session_count"] is None
    assert result["relationship_results_suppressed"] is True
    assert result["relationship_pair_operations"] is None
    assert result["shared_relationship_count"] is None
    assert result["reported_session_count"] is None
    assert result["classification_counts"] is None
    assert result["findings"] == []
    assert "private connect failure detail" not in json.dumps(result)
    assert list(temp_parent.iterdir()) == []

    exit_code = lineage.main([
        "--state", str(tmp_path / "cli-state"), "--json",
    ])
    captured = capsys.readouterr()
    cli_result = json.loads(captured.out)
    assert exit_code == 2
    assert captured.err == ""
    assert cli_result["status"] == "partial"
    assert cli_result["relationship_results_suppressed"] is True
    assert cli_result["classification_counts"] is None


def test_lineage_build_findings_database_failure_suppresses_relationship_output(
    tmp_path, monkeypatch
):
    appdata, projects, metadata = _lineage_roots(tmp_path)
    _write_json(metadata / "local_one.json", {
        "sessionId": "desktop-session-one",
        "cliSessionId": "11111111-1111-4111-8111-111111111111",
    })
    projects.mkdir(parents=True)
    temp_parent = tmp_path / "lineage-temp"
    temp_parent.mkdir()

    def fail_build_findings(*_args, **_kwargs):
        raise sqlite3.DatabaseError("private build failure detail")

    monkeypatch.setattr(lineage, "_build_findings", fail_build_findings)
    result = lineage.audit_lineage(
        str(appdata),
        str(projects),
        temp_parent=str(temp_parent),
    )

    assert result["status"] == "partial"
    assert result["errors"] == [{"code": "temporary_index_error"}]
    assert result["files_scanned"] == 1
    assert result["transcripts_scanned"] == 0
    assert result["records_scanned"] == 0
    assert result["session_count"] is None
    assert result["relationship_results_suppressed"] is True
    assert result["relationship_pair_operations"] is None
    assert result["shared_relationship_count"] is None
    assert result["reported_session_count"] is None
    assert result["classification_counts"] is None
    assert result["findings"] == []
    assert "private build failure detail" not in json.dumps(result)
    assert list(temp_parent.iterdir()) == []


def test_lineage_finding_cap_suppresses_relationship_output(tmp_path):
    appdata, projects, metadata = _lineage_roots(tmp_path)
    _write_json(metadata / "local_one.json", {"sessionId": "desktop-one"})
    _write_json(metadata / "local_two.json", {"sessionId": "desktop-two"})
    projects.mkdir(parents=True)

    result = lineage.audit_lineage(
        str(appdata), str(projects), max_findings=1
    )

    assert result["status"] == "partial"
    assert result["errors"] == [{"code": "finding_cap_reached"}]
    assert result["files_scanned"] == 2
    assert result["session_count"] is None
    assert result["relationship_results_suppressed"] is True
    assert result["relationship_pair_operations"] is None
    assert result["shared_relationship_count"] is None
    assert result["reported_session_count"] is None
    assert result["classification_counts"] is None
    assert result["findings"] == []


def test_local_agent_inventory_is_aggregate_and_does_not_parse_leveldb(tmp_path):
    root = tmp_path / "Private Agent Root"
    owner = root / "account-private" / "org-private"
    outputs = owner / "local_secret-session" / "outputs"
    outputs.mkdir(parents=True)
    (outputs / "result-private.txt").write_text("private output", encoding="utf-8")
    leveldb = owner / "leveldb"
    leveldb.mkdir()
    (leveldb / "CURRENT").write_text("private leveldb", encoding="utf-8")

    result = local_agents.inventory(str(root))

    assert result["status"] == "complete"
    assert result["buckets"] == {
        "root": {"status": "present", "count": 1},
        "owner": {"count": 1},
        "local_*": {"count": 1},
        "outputs": {"directory_count": 1, "entry_count": 1},
    }
    assert "owners" not in result
    assert "local_sessions" not in result
    rendered = json.dumps(result)
    for private_value in (
        str(tmp_path), "account-private", "org-private", "secret-session",
        "result-private", "private output", "leveldb", "Cowork",
    ):
        assert private_value not in rendered


def test_local_agent_inventory_reports_a_bounded_partial_result(tmp_path):
    root = tmp_path / "agents"
    owner = root / "account" / "org"
    for index in range(3):
        (owner / f"local_{index}").mkdir(parents=True)

    result = local_agents.inventory(str(root), max_sessions=1)

    assert result["status"] == "partial"
    assert result["buckets"]["local_*"]["count"] == 1
    assert any(error["code"] == "session_cap_reached" for error in result["errors"])


def test_local_agent_output_cap_is_not_triggered_until_exceeded(tmp_path):
    root = tmp_path / "agents"
    outputs = root / "account" / "org" / "local_one" / "outputs"
    outputs.mkdir(parents=True)
    (outputs / "one").write_text("x", encoding="utf-8")

    exact = local_agents.inventory(str(root), max_output_entries=1)
    (outputs / "two").write_text("x", encoding="utf-8")
    exceeded = local_agents.inventory(str(root), max_output_entries=1)

    assert exact["status"] == "complete"
    assert exact["buckets"]["outputs"]["entry_count"] == 1
    assert exceeded["status"] == "partial"
    assert exceeded["buckets"]["outputs"]["entry_count"] == 1
    assert any(
        error["code"] == "output_entry_cap_reached"
        for error in exceeded["errors"]
    )


def _create_state_database(path, *, with_key):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
        if with_key:
            conn.execute(
                "INSERT INTO ItemTable(key, value) VALUES (?, ?)",
                ("agentSessions.model.cache", "private cache payload"),
            )
        conn.commit()
    finally:
        conn.close()


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _file_bytes_and_metadata(path):
    metadata = path.stat()
    return (
        path.read_bytes(),
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _open_wal_database(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.execute(
        "INSERT INTO ItemTable(key, value) VALUES (?, ?)",
        ("agentSessions.model.cache", "private WAL cache payload"),
    )
    conn.commit()
    assert path.with_name(path.name + "-wal").is_file()
    assert path.with_name(path.name + "-shm").is_file()
    return conn


@pytest.mark.parametrize(
    ("has_index", "has_key", "expected"),
    [
        (True, False, "index_only"),
        (False, True, "db_only"),
        (True, True, "both"),
        (False, False, "neither"),
    ],
)
def test_vscode_surface_combinations_are_observational(
    tmp_path, has_index, has_key, expected
):
    projects = tmp_path / "Private Projects"
    slug = projects / "private-slug"
    slug.mkdir(parents=True)
    (slug / "session.jsonl").write_text("private transcript", encoding="utf-8")
    index = slug / "sessions-index.json"
    if has_index:
        index.write_text("private unparseable index", encoding="utf-8")
    workspace = tmp_path / "Private Workspace"
    database = workspace / "workspace-private" / "state.vscdb"
    _create_state_database(database, with_key=has_key)
    before_db = _sha256(database)
    before_index = _sha256(index) if has_index else None

    result = vscode.audit_surfaces(str(projects), str(workspace))

    assert result["status"] == "complete"
    assert result["transcript_bearing_slug_count"] == 1
    assert result["surface_combination"] == expected
    assert result["sessions_index"]["content_parsed"] is False
    assert result["state_vscdb"]["cache_values_parsed"] is False
    assert result["state_vscdb"]["read_only"] is True
    assert _sha256(database) == before_db
    if has_index:
        assert _sha256(index) == before_index
    rendered = json.dumps(result)
    assert "private cache payload" not in rendered
    assert "private unparseable index" not in rendered
    assert str(tmp_path) not in rendered


def test_vscode_cli_uses_shared_defaults_and_reports_resolved_paths(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        vscode,
        "default_claude_sessions_index_dir",
        lambda: "shared-claude-projects",
    )
    monkeypatch.setattr(
        vscode,
        "default_vscode_workspace_storage_dir",
        lambda: "shared-vscode-workspaceStorage",
    )

    assert vscode.main([]) == 0
    rendered = capsys.readouterr().out

    assert "Claude sessions-index path: shared-claude-projects" in rendered
    assert "VS Code workspaceStorage path: shared-vscode-workspaceStorage" in rendered


def test_vscode_cli_fixture_root_still_overrides_path_arguments(tmp_path, capsys):
    state = tmp_path / "fixture-state"
    index = state / "projects" / "fixture-slug" / "sessions-index.json"
    index.parent.mkdir(parents=True)
    index.write_text("fixture index", encoding="utf-8")

    assert vscode.main([
        "--state", str(state),
        "--projects-dir", str(tmp_path / "ignored-projects"),
        "--workspace-dir", str(tmp_path / "ignored-workspace"),
    ]) == 0
    rendered = capsys.readouterr().out

    assert "sessions-index.json files: 1 (13 bytes)" in rendered
    assert "Claude sessions-index path: {}".format(state / "projects") in rendered
    assert "ignored-projects" not in rendered
    assert "ignored-workspace" not in rendered


def test_vscode_bad_database_is_explicitly_partial(tmp_path):
    projects = tmp_path / "projects"
    index = projects / "slug" / "sessions-index.json"
    index.parent.mkdir(parents=True)
    index.write_text("private index payload", encoding="utf-8")
    workspace = tmp_path / "workspace"
    database = workspace / "one" / "state.vscdb"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"not sqlite")

    result = vscode.audit_surfaces(str(projects), str(workspace))

    assert result["status"] == "partial"
    assert result["surface_combination"] is None
    assert result["sessions_index"]["present_count"] == 1
    assert result["sessions_index"]["conclusive"] is False
    assert result["state_vscdb"]["conclusive"] is False
    assert result["errors"][0]["code"] == "database_unreadable"


def test_vscode_database_byte_cap_prevents_sqlite_open(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    projects.mkdir()
    workspace = tmp_path / "workspace"
    database = workspace / "one" / "state.vscdb"
    _create_state_database(database, with_key=True)

    def forbidden_open(*_args, **_kwargs):
        raise AssertionError("oversized database was opened")

    monkeypatch.setattr(vscode, "_has_cache_key", forbidden_open)
    result = vscode.audit_surfaces(
        str(projects), str(workspace), max_database_bytes=1
    )

    assert result["status"] == "partial"
    assert result["surface_combination"] is None
    assert result["state_vscdb"]["database_count"] == 1
    assert result["state_vscdb"]["inspected_database_count"] == 0
    assert any(
        error["code"] == "database_byte_cap_reached"
        for error in result["errors"]
    )


def test_vscode_opcode_budget_interrupts_unindexed_scan(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    workspace = tmp_path / "workspace"
    database = workspace / "one" / "state.vscdb"
    database.parent.mkdir(parents=True)
    conn = sqlite3.connect(database)
    try:
        conn.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
        conn.executemany(
            "INSERT INTO ItemTable(key, value) VALUES (?, ?)",
            ((f"unrelated-{index}", "private") for index in range(5000)),
        )
        conn.commit()
    finally:
        conn.close()

    result = vscode.audit_surfaces(
        str(projects), str(workspace), max_database_opcodes=10
    )

    assert result["status"] == "partial"
    assert result["surface_combination"] is None
    assert result["state_vscdb"]["inspected_database_count"] == 1
    assert result["state_vscdb"]["claude_cache_key_database_count"] == 0
    assert result["state_vscdb"]["sqlite_opcode_ticks"] == 10
    assert any(
        error["code"] == "database_opcode_cap_reached"
        for error in result["errors"]
    )


def test_vscode_wal_snapshot_detects_cache_without_touching_live_files(
    tmp_path, monkeypatch
):
    projects = tmp_path / "projects"
    projects.mkdir()
    workspace = tmp_path / "workspace"
    database = workspace / "one" / "state.vscdb"
    writer = _open_wal_database(database)
    wal = database.with_name(database.name + "-wal")
    shm = database.with_name(database.name + "-shm")
    temp_parent = tmp_path / "snapshots"
    temp_parent.mkdir()
    live_before = {
        path.name: _file_bytes_and_metadata(path)
        for path in (database, wal, shm)
    }
    opened = []
    real_connect = sqlite3.connect

    def traced_connect(target, *args, **kwargs):
        opened.append(str(target))
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(vscode.sqlite3, "connect", traced_connect)
    try:
        result = vscode.audit_surfaces(
            str(projects),
            str(workspace),
            snapshot_temp_parent=str(temp_parent),
        )
        live_after = {
            path.name: _file_bytes_and_metadata(path)
            for path in (database, wal, shm)
            if path.exists()
        }
    finally:
        writer.close()

    assert result["status"] == "complete"
    assert result["state_vscdb"]["claude_cache_key_database_count"] == 1
    assert result["state_vscdb"]["live_sqlite_opened"] is False
    assert result["state_vscdb"]["snapshot_only"] is True
    assert opened
    assert all("vscode-state-snapshot-" in target for target in opened)
    assert live_after == live_before
    assert list(temp_parent.iterdir()) == []


def test_vscode_wal_snapshot_does_not_create_missing_live_shm(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    staging_database = tmp_path / "staging" / "state.vscdb"
    writer = _open_wal_database(staging_database)
    staging_wal = staging_database.with_name(staging_database.name + "-wal")
    workspace = tmp_path / "workspace"
    database = workspace / "one" / "state.vscdb"
    database.parent.mkdir(parents=True)
    wal = database.with_name(database.name + "-wal")
    shm = database.with_name(database.name + "-shm")
    shutil.copyfile(staging_database, database)
    shutil.copyfile(staging_wal, wal)
    source_before = {
        path.name: _file_bytes_and_metadata(path) for path in (database, wal)
    }
    try:
        result = vscode.audit_surfaces(str(projects), str(workspace))
    finally:
        writer.close()

    assert result["status"] == "complete"
    assert result["state_vscdb"]["claude_cache_key_database_count"] == 1
    assert not shm.exists()
    assert {
        path.name: _file_bytes_and_metadata(path) for path in (database, wal)
    } == source_before


def test_vscode_source_growth_past_cap_discards_snapshot(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    projects.mkdir()
    workspace = tmp_path / "workspace"
    database = workspace / "one" / "state.vscdb"
    _create_state_database(database, with_key=True)
    original_size = database.stat().st_size
    temp_parent = tmp_path / "snapshots"
    temp_parent.mkdir()
    real_copy = vscode._copy_snapshot_file
    changed = False

    def growing_copy(source, destination, copied_bytes, max_database_bytes):
        nonlocal changed
        if not changed and source == str(database):
            changed = True
            with open(source, "ab") as handle:
                handle.write(b"growth")
        return real_copy(
            source, destination, copied_bytes, max_database_bytes
        )

    monkeypatch.setattr(vscode, "_copy_snapshot_file", growing_copy)
    result = vscode.audit_surfaces(
        str(projects),
        str(workspace),
        max_database_bytes=original_size + 1,
        snapshot_temp_parent=str(temp_parent),
    )

    assert result["status"] == "partial"
    assert result["surface_combination"] is None
    assert result["state_vscdb"]["inspected_database_count"] == 0
    assert result["state_vscdb"]["claude_cache_key_database_count"] == 0
    assert "fingerprint" not in json.dumps(result)
    assert any(
        error["code"] == "database_byte_cap_reached"
        for error in result["errors"]
    )
    assert list(temp_parent.iterdir()) == []


def test_vscode_source_mtime_change_discards_snapshot(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    projects.mkdir()
    workspace = tmp_path / "workspace"
    database = workspace / "one" / "state.vscdb"
    _create_state_database(database, with_key=True)
    real_copy = vscode._copy_snapshot_file
    changed = False

    def touching_copy(source, destination, copied_bytes, max_database_bytes):
        nonlocal changed
        result = real_copy(
            source, destination, copied_bytes, max_database_bytes
        )
        if not changed and source == str(database):
            changed = True
            metadata = os.stat(source)
            os.utime(
                source,
                ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000_000),
            )
        return result

    monkeypatch.setattr(vscode, "_copy_snapshot_file", touching_copy)
    result = vscode.audit_surfaces(str(projects), str(workspace))

    assert result["status"] == "partial"
    assert result["surface_combination"] is None
    assert result["state_vscdb"]["inspected_database_count"] == 0
    assert result["state_vscdb"]["claude_cache_key_database_count"] == 0
    assert any(
        error["code"] == "database_source_changed"
        for error in result["errors"]
    )


def test_vscode_post_query_live_db_mtime_drift_suppresses_cache_result(
    tmp_path, monkeypatch
):
    projects = tmp_path / "projects"
    projects.mkdir()
    workspace = tmp_path / "workspace"
    database = workspace / "one" / "state.vscdb"
    _create_state_database(database, with_key=True)
    real_query = vscode._has_cache_key

    def query_then_touch(snapshot_database, max_opcodes):
        query_result = real_query(snapshot_database, max_opcodes)
        metadata = os.stat(database)
        os.utime(
            database,
            ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000_000),
        )
        return query_result

    monkeypatch.setattr(vscode, "_has_cache_key", query_then_touch)
    result = vscode.audit_surfaces(str(projects), str(workspace))

    assert result["status"] == "partial"
    assert result["surface_combination"] is None
    assert result["state_vscdb"]["inspected_database_count"] == 1
    assert result["state_vscdb"]["claude_cache_key_database_count"] == 0
    assert any(
        error["code"] == "database_source_changed"
        for error in result["errors"]
    )


def test_vscode_post_query_live_wal_membership_drift_suppresses_cache_result(
    tmp_path, monkeypatch
):
    projects = tmp_path / "projects"
    projects.mkdir()
    workspace = tmp_path / "workspace"
    database = workspace / "one" / "state.vscdb"
    _create_state_database(database, with_key=True)
    live_wal = database.with_name(database.name + "-wal")
    assert not live_wal.exists()
    real_query = vscode._has_cache_key

    def query_then_add_live_wal(snapshot_database, max_opcodes):
        query_result = real_query(snapshot_database, max_opcodes)
        live_wal.write_bytes(b"new live WAL membership")
        return query_result

    monkeypatch.setattr(vscode, "_has_cache_key", query_then_add_live_wal)
    result = vscode.audit_surfaces(str(projects), str(workspace))

    assert result["status"] == "partial"
    assert result["surface_combination"] is None
    assert result["state_vscdb"]["inspected_database_count"] == 1
    assert result["state_vscdb"]["claude_cache_key_database_count"] == 0
    assert any(
        error["code"] == "database_source_changed"
        for error in result["errors"]
    )


def test_vscode_same_stat_content_rewrite_during_query_is_partial(
    tmp_path, monkeypatch
):
    projects = tmp_path / "projects"
    projects.mkdir()
    workspace = tmp_path / "workspace"
    database = workspace / "one" / "state.vscdb"
    _create_state_database(database, with_key=True)
    original = os.stat(database)
    real_query = vscode._has_cache_key

    def query_then_rewrite_live_database(snapshot_database, max_opcodes):
        query_result = real_query(snapshot_database, max_opcodes)
        with open(database, "r+b") as handle:
            handle.seek(-1, os.SEEK_END)
            final_byte = handle.read(1)
            handle.seek(-1, os.SEEK_END)
            handle.write(bytes([final_byte[0] ^ 1]))
        os.utime(
            database,
            ns=(original.st_atime_ns, original.st_mtime_ns),
        )
        rewritten = os.stat(database)
        assert rewritten.st_size == original.st_size
        assert rewritten.st_mtime_ns == original.st_mtime_ns
        return query_result

    monkeypatch.setattr(vscode, "_has_cache_key", query_then_rewrite_live_database)
    result = vscode.audit_surfaces(str(projects), str(workspace))

    assert result["status"] == "partial"
    assert result["surface_combination"] is None
    assert result["state_vscdb"]["conclusive"] is False
    assert result["state_vscdb"]["claude_cache_key_database_count"] == 0
    assert any(
        error["code"] == "database_source_changed"
        for error in result["errors"]
    )


def test_inventory_root_stat_access_error_is_not_reported_absent(
    tmp_path, monkeypatch
):
    root = tmp_path / "agent-root"
    root.mkdir()
    real_stat = sidecar_common._stat_path

    def denied(path, *, follow_symlinks=True):
        if os.path.normcase(os.fspath(path)) == os.path.normcase(str(root)):
            raise PermissionError("denied")
        return real_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(sidecar_common, "_stat_path", denied)
    result = local_agents.inventory(str(root))

    assert result["status"] == "partial"
    assert result["buckets"]["root"] == {"status": "error", "count": 0}
    assert result["errors"][0]["code"] == "path_access_error"
    assert str(root) not in json.dumps(result)


def test_missing_inventory_root_is_complete_and_distinct_from_access_error(tmp_path):
    result = local_agents.inventory(str(tmp_path / "missing-agent-root"))

    assert result["status"] == "complete"
    assert result["error_count"] == 0
    assert result["buckets"]["root"] == {"status": "absent", "count": 0}


def test_inventory_scandir_and_outputs_access_errors_are_partial(
    tmp_path, monkeypatch
):
    root = tmp_path / "agent-root"
    outputs = root / "account" / "org" / "local_one" / "outputs"
    outputs.mkdir(parents=True)
    real_scandir = sidecar_common._scandir_path

    def denied_scandir(path):
        if os.path.normcase(os.fspath(path)) == os.path.normcase(str(root)):
            raise PermissionError("denied")
        return real_scandir(path)

    monkeypatch.setattr(sidecar_common, "_scandir_path", denied_scandir)
    root_result = local_agents.inventory(str(root))
    assert root_result["status"] == "partial"
    assert root_result["errors"][0]["code"] == "directory_unreadable"

    monkeypatch.setattr(sidecar_common, "_scandir_path", real_scandir)
    real_stat = sidecar_common._stat_path

    def denied_outputs(path, *, follow_symlinks=True):
        if os.path.normcase(os.fspath(path)) == os.path.normcase(str(outputs)):
            raise PermissionError("denied")
        return real_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(sidecar_common, "_stat_path", denied_outputs)
    outputs_result = local_agents.inventory(str(root))
    assert outputs_result["status"] == "partial"
    assert outputs_result["buckets"]["local_*"]["count"] == 1
    assert outputs_result["buckets"]["outputs"]["directory_count"] == 0
    assert any(
        error["code"] == "path_access_error"
        for error in outputs_result["errors"]
    )


def test_vscode_database_stat_access_error_is_partial(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    projects.mkdir()
    workspace = tmp_path / "workspace"
    database = workspace / "one" / "state.vscdb"
    _create_state_database(database, with_key=True)
    real_stat = sidecar_common._stat_path

    def denied(path, *, follow_symlinks=True):
        if os.path.normcase(os.fspath(path)) == os.path.normcase(str(database)):
            raise PermissionError("denied")
        return real_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(sidecar_common, "_stat_path", denied)
    result = vscode.audit_surfaces(str(projects), str(workspace))

    assert result["status"] == "partial"
    assert result["surface_combination"] is None
    assert result["state_vscdb"]["database_count"] == 0
    assert result["errors"][0]["code"] == "path_access_error"


def test_lineage_and_vscode_store_access_errors_are_partial(tmp_path, monkeypatch):
    appdata = tmp_path / "appdata" / "Claude"
    projects = tmp_path / "projects"
    workspace = tmp_path / "workspace"
    projects.mkdir(parents=True)
    workspace.mkdir()
    denied_paths = {os.path.normcase(str(projects)), os.path.normcase(str(workspace))}
    real_stat = sidecar_common._stat_path

    def denied(path, *, follow_symlinks=True):
        if os.path.normcase(os.fspath(path)) in denied_paths:
            raise PermissionError("denied")
        return real_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(sidecar_common, "_stat_path", denied)
    lineage_result = lineage.audit_lineage(str(appdata), str(projects))
    vscode_result = vscode.audit_surfaces(str(projects), str(workspace))

    assert lineage_result["status"] == "partial"
    assert vscode_result["status"] == "partial"
    assert vscode_result["surface_combination"] is None
    assert all(
        any(error["code"] == "path_access_error" for error in result["errors"])
        for result in (lineage_result, vscode_result)
    )
