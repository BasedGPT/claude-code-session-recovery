"""Focused interface checks for shared session-state inspection."""

import json
import os
import sys


TOOLS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools")
sys.path.insert(0, TOOLS)

import session_state


SESSION_ID = "11111111-1111-1111-1111-111111111111"


def _write_state(tmp_path):
    metadata_dir = (
        tmp_path
        / "appdata"
        / "Claude"
        / "claude-code-sessions"
        / "account-a"
        / "organisation-a"
    )
    metadata_dir.mkdir(parents=True)
    (metadata_dir / "local_one.json").write_text(
        json.dumps(
            {
                "sessionId": "local_one",
                "cliSessionId": SESSION_ID,
                "cwd": r"C:\fixture\project",
                "createdAt": 1704067200000,
                "model": "claude-test",
                "title": "Fixture session",
            }
        ),
        encoding="utf-8",
    )
    transcript_dir = tmp_path / "projects" / "C--fixture-project"
    transcript_dir.mkdir(parents=True)
    (transcript_dir / f"{SESSION_ID}.jsonl").write_text("{}\n", encoding="utf-8")


def test_snapshot_preserves_metadata_transcript_link_counts(tmp_path):
    _write_state(tmp_path)
    appdata = tmp_path / "appdata" / "Claude"
    projects = tmp_path / "projects"

    snapshot = session_state.build_snapshot(str(appdata), str(projects), fixture_mode=True)

    assert snapshot["total_metadata_count"] == 1
    assert snapshot["metadata_with_cli_count"] == 1
    assert snapshot["metadata_dangling_cli_count"] == 0
    assert snapshot["jsonl_count"] == 1
    assert snapshot["jsonl_orphan_count"] == 0
    assert snapshot["schema_version"] == "recognised"
    assert session_state.make_diagnosis_id(snapshot) == session_state.make_diagnosis_id(snapshot)


def test_metadata_directories_are_sorted_and_ignore_non_directories(tmp_path):
    _write_state(tmp_path)
    sessions_root = tmp_path / "appdata" / "Claude" / "claude-code-sessions"
    (sessions_root / "not-an-account.txt").write_text("ignored", encoding="utf-8")
    second = sessions_root / "account-b" / "organisation-b"
    second.mkdir(parents=True)

    directories = list(session_state.find_metadata_directories(str(tmp_path / "appdata" / "Claude")))

    assert [(account, organisation) for account, organisation, _path in directories] == [
        ("account-a", "organisation-a"),
        ("account-b", "organisation-b"),
    ]
