"""Focused tests for the read-only global VS Code hidden-session audit."""

import json
import os
import sqlite3
import sys


SESSIONS = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "tools", "sessions"
)
if SESSIONS not in sys.path:
    sys.path.insert(0, SESSIONS)

import audit_vscode_session_surfaces as audit  # noqa: E402


_MISSING = object()


def _create_database(path, value=_MISSING):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
        if value is not _MISSING:
            connection.execute(
                "INSERT INTO ItemTable(key, value) VALUES (?, ?)",
                (audit.GLOBAL_STATE_KEY, value),
            )
        connection.commit()
    finally:
        connection.close()


def _source_snapshot(path):
    metadata = path.stat()
    return (
        path.read_bytes(),
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _audit_roots(tmp_path, hidden_value, transcript_names=()):
    projects = tmp_path / "projects"
    slug = projects / "encoded-project"
    for name in transcript_names:
        transcript = slug / (name + ".jsonl")
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text("{}\n", encoding="utf-8")
    workspace = tmp_path / "workspaceStorage"
    global_db = tmp_path / "globalStorage" / "state.vscdb"
    _create_database(global_db, json.dumps(hidden_value))
    return projects, workspace, global_db


def test_global_hidden_ids_overlap_is_opaque_and_read_only(tmp_path):
    projects, workspace, global_db = _audit_roots(
        tmp_path,
        {"hiddenSessionIds": ["hidden-id", "missing-id", "hidden-id"]},
        transcript_names=("hidden-id", "visible-id"),
    )
    workspace_db = workspace / "workspace-one" / "state.vscdb"
    _create_database(workspace_db, "workspace cache payload")
    before = _source_snapshot(global_db)

    result = audit.audit_surfaces(
        str(projects), str(workspace), global_state_db=str(global_db)
    )

    hidden = result["global_state_vscdb"]["hidden_session_ids"]
    assert result["status"] == "complete"
    assert result["state_vscdb"]["database_count"] == 1
    assert result["global_state_vscdb"]["database_count"] == 1
    assert result["global_state_vscdb"]["keys_checked"] == [
        "Anthropic.claude-code"
    ]
    assert hidden["key_status"] == "present"
    assert hidden["value_status"] == "valid"
    assert hidden["hidden_id_count"] == 3
    assert hidden["unique_hidden_id_count"] == 2
    assert hidden["transcript_overlap_count"] == 1
    assert hidden["hidden_ids_without_transcript_match_count"] == 1
    assert hidden["listing_interpretation"] == "hidden_transcripts_on_disk"
    assert result["global_state_vscdb"]["value_parsed_structurally"] is True
    assert _source_snapshot(global_db) == before
    assert not global_db.with_name("state.vscdb-wal").exists()
    assert not global_db.with_name("state.vscdb-shm").exists()

    rendered = json.dumps(result)
    assert "hidden-id" not in rendered
    assert "missing-id" not in rendered
    assert str(tmp_path) not in rendered


def test_global_hidden_ids_without_overlap_are_not_called_missing_indexing(
    tmp_path,
):
    projects, workspace, global_db = _audit_roots(
        tmp_path,
        {"hiddenSessionIds": ["stale-hidden-id"]},
        transcript_names=("visible-id",),
    )

    result = audit.audit_surfaces(
        str(projects), str(workspace), global_state_db=str(global_db)
    )

    hidden = result["global_state_vscdb"]["hidden_session_ids"]
    assert result["status"] == "complete"
    assert hidden["transcript_overlap_count"] == 0
    assert hidden["hidden_ids_without_transcript_match_count"] == 1
    assert hidden["listing_interpretation"] == (
        "hidden_ids_without_transcript_match"
    )
    assert result["sessions_index"]["content_parsed"] is False


def test_global_state_missing_is_complete_and_distinct_from_missing_key(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    workspace = tmp_path / "workspaceStorage"
    missing_global = tmp_path / "missing-global" / "state.vscdb"

    missing = audit.audit_surfaces(
        str(projects), str(workspace), global_state_db=str(missing_global)
    )
    assert missing["status"] == "complete"
    assert missing["global_state_vscdb"]["database_status"] == "absent"
    assert missing["global_state_vscdb"]["hidden_session_ids"]["key_status"] == (
        "absent"
    )
    assert missing["global_state_vscdb"]["hidden_session_ids"][
        "listing_interpretation"
    ] == "global_database_absent"

    global_db = tmp_path / "globalStorage" / "state.vscdb"
    _create_database(global_db)
    no_key = audit.audit_surfaces(
        str(projects), str(workspace), global_state_db=str(global_db)
    )
    assert no_key["status"] == "complete"
    assert no_key["global_state_vscdb"]["database_status"] == "present"
    assert no_key["global_state_vscdb"]["hidden_session_ids"]["key_status"] == (
        "absent"
    )
    assert no_key["global_state_vscdb"]["hidden_session_ids"][
        "listing_interpretation"
    ] == "global_key_absent"


def test_global_hidden_ids_malformed_value_is_partial_without_payload(tmp_path):
    projects, workspace, global_db = _audit_roots(
        tmp_path,
        {"hiddenSessionIds": {"private": "payload"}},
        transcript_names=("private",),
    )

    result = audit.audit_surfaces(
        str(projects), str(workspace), global_state_db=str(global_db)
    )

    hidden = result["global_state_vscdb"]["hidden_session_ids"]
    assert result["status"] == "partial"
    assert hidden["value_status"] == "malformed"
    assert hidden["hidden_id_count"] is None
    assert hidden["transcript_overlap_count"] is None
    assert hidden["conclusive"] is False
    assert any(
        error["code"] == "hidden_session_ids_malformed"
        for error in result["errors"]
    )
    assert "private" not in json.dumps(result)


def test_global_hidden_ids_count_cap_is_partial_and_does_not_query_live_db(
    tmp_path,
):
    projects, workspace, global_db = _audit_roots(
        tmp_path,
        {"hiddenSessionIds": ["one", "two"]},
        transcript_names=("one", "two"),
    )
    before = _source_snapshot(global_db)

    result = audit.audit_surfaces(
        str(projects),
        str(workspace),
        global_state_db=str(global_db),
        max_hidden_session_ids=1,
    )

    hidden = result["global_state_vscdb"]["hidden_session_ids"]
    assert result["status"] == "partial"
    assert hidden["value_status"] == "oversize"
    assert hidden["hidden_id_count"] is None
    assert hidden["transcript_overlap_count"] is None
    assert any(
        error["code"] == "hidden_session_ids_count_cap_reached"
        for error in result["errors"]
    )
    assert _source_snapshot(global_db) == before


def test_global_hidden_ids_byte_cap_is_partial(tmp_path):
    projects, workspace, global_db = _audit_roots(
        tmp_path,
        {"hiddenSessionIds": ["one"]},
        transcript_names=("one",),
    )

    result = audit.audit_surfaces(
        str(projects),
        str(workspace),
        global_state_db=str(global_db),
        max_hidden_session_bytes=1,
    )

    hidden = result["global_state_vscdb"]["hidden_session_ids"]
    assert result["status"] == "partial"
    assert hidden["value_status"] == "oversize"
    assert any(
        error["code"] == "hidden_session_ids_byte_cap_reached"
        for error in result["errors"]
    )


def test_global_hidden_ids_source_drift_discards_overlap(tmp_path, monkeypatch):
    projects, workspace, global_db = _audit_roots(
        tmp_path,
        {"hiddenSessionIds": ["hidden-id"]},
        transcript_names=("hidden-id",),
    )
    real_copy = audit._copy_snapshot_file
    changed = False

    def copy_then_touch(source, destination, copied_bytes, max_database_bytes):
        nonlocal changed
        result = real_copy(source, destination, copied_bytes, max_database_bytes)
        if not changed and os.path.normcase(source) == os.path.normcase(str(global_db)):
            changed = True
            metadata = os.stat(source)
            os.utime(
                source,
                ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000_000),
            )
        return result

    monkeypatch.setattr(audit, "_copy_snapshot_file", copy_then_touch)
    result = audit.audit_surfaces(
        str(projects), str(workspace), global_state_db=str(global_db)
    )

    hidden = result["global_state_vscdb"]["hidden_session_ids"]
    assert result["status"] == "partial"
    assert hidden["transcript_overlap_count"] is None
    assert hidden["conclusive"] is False
    assert any(
        error["code"] == "database_source_changed"
        for error in result["errors"]
    )


def test_human_output_does_not_expose_global_database_path_or_ids(tmp_path, capsys):
    projects, workspace, global_db = _audit_roots(
        tmp_path,
        {"hiddenSessionIds": ["private-hidden-session"]},
        transcript_names=("private-hidden-session",),
    )
    assert audit.main([
        "--projects-dir", str(projects),
        "--workspace-dir", str(workspace),
        "--global-state-db", str(global_db),
    ]) == 0
    rendered = capsys.readouterr().out
    assert str(global_db) not in rendered
    assert "private-hidden-session" not in rendered
    assert "Global hiddenSessionIds overlap with transcripts: 1" in rendered
