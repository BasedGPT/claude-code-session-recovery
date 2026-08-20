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


def test_inventory_status_is_opt_in_and_does_not_change_diagnosis_token(tmp_path):
    _write_state(tmp_path)
    appdata = tmp_path / "appdata" / "Claude"
    projects = tmp_path / "projects"
    public_snapshot = session_state.build_snapshot(
        str(appdata), str(projects), fixture_mode=True
    )
    guarded_snapshot = session_state.build_snapshot(
        str(appdata),
        str(projects),
        fixture_mode=True,
        include_inventory_status=True,
    )

    assert "_metadata_inventory_complete" not in public_snapshot
    assert "_transcript_inventory_complete" not in public_snapshot
    assert guarded_snapshot["_metadata_inventory_complete"] is True
    assert guarded_snapshot["_transcript_inventory_complete"] is True
    assert session_state.make_diagnosis_id(guarded_snapshot) == session_state.make_diagnosis_id(
        public_snapshot
    )


def test_broad_audit_fields_do_not_change_diagnosis_token(tmp_path):
    _write_state(tmp_path)
    appdata = tmp_path / "appdata" / "Claude"
    projects = tmp_path / "projects"

    snapshot = session_state.build_snapshot(str(appdata), str(projects), fixture_mode=True)
    audited = dict(snapshot)
    audited["transcript_graph_audit"] = {
        "reachable_count": 1,
        "unreachable_count": 0,
    }

    assert session_state.make_diagnosis_id(audited) == session_state.make_diagnosis_id(snapshot)


def test_replacing_the_sole_pair_invalidates_the_diagnosis_token(tmp_path):
    _write_state(tmp_path)
    appdata = tmp_path / "appdata" / "Claude"
    projects = tmp_path / "projects"
    sessions_root = appdata / "claude-code-sessions"

    before = session_state.build_snapshot(str(appdata), str(projects), fixture_mode=True)
    before_id = session_state.make_diagnosis_id(before)
    (sessions_root / "account-a" / "organisation-a").rename(
        sessions_root / "account-a" / "organisation-b"
    )
    (sessions_root / "account-a").rename(sessions_root / "account-b")
    after = session_state.build_snapshot(str(appdata), str(projects), fixture_mode=True)
    after_id = session_state.make_diagnosis_id(after)

    assert "desktop_session_pairs" not in before
    assert "desktop_session_pairs" not in after
    assert before["_desktop_session_pair_identities"] == [
        {"account_uuid": "account-a", "organisation_uuid": "organisation-a"}
    ]
    assert after["_desktop_session_pair_identities"] == [
        {"account_uuid": "account-b", "organisation_uuid": "organisation-b"}
    ]
    assert before_id != after_id


def test_transaction_exclusions_preserve_the_prepublication_diagnosis(tmp_path):
    _write_state(tmp_path)
    appdata = tmp_path / "appdata" / "Claude"
    projects = tmp_path / "projects"
    sessions_root = appdata / "claude-code-sessions"

    before = session_state.build_snapshot(
        str(appdata), str(projects), fixture_mode=True
    )
    restored = sessions_root / "account-b" / "organisation-b" / "local_new.json"
    restored.parent.mkdir(parents=True)
    restored.write_text('{"sessionId":"new"}', encoding="utf-8")

    normalized = session_state.build_snapshot(
        str(appdata),
        str(projects),
        fixture_mode=True,
        excluded_metadata_paths={str(restored)},
        excluded_metadata_pairs={("account-b", "organisation-b")},
    )

    assert session_state.make_diagnosis_id(normalized) == session_state.make_diagnosis_id(before)


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
