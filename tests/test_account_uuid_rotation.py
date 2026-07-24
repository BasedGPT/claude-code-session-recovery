"""Tests for Desktop account/organisation UUID rotation diagnostics."""

import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DIAGNOSE = REPO_ROOT / "tools" / "diagnose.py"


def _write_metadata(state, account, organisation, filename, cli_session_id):
    metadata_dir = (
        state
        / "appdata"
        / "Claude"
        / "claude-code-sessions"
        / account
        / organisation
    )
    metadata_dir.mkdir(parents=True, exist_ok=True)
    (metadata_dir / filename).write_text(
        json.dumps(
            {
                "sessionId": filename.removesuffix(".json"),
                "cliSessionId": cli_session_id,
                "cwd": r"C:\Users\fixture-user\project",
                "createdAt": 1,
                "updatedAt": 1,
                "lastActivityAt": 1,
            }
        ),
        encoding="utf-8",
    )

    jsonl_dir = state / "projects" / "C--Users-fixture-user-project"
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    (jsonl_dir / f"{cli_session_id}.jsonl").write_text("{}\n", encoding="utf-8")


def _run_diagnose(state, *extra_args):
    return subprocess.run(
        [sys.executable, str(DIAGNOSE), "--state", str(state), *extra_args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )


def test_diagnose_reports_multiple_pairs_and_uuid_rotation_split(tmp_path):
    state = tmp_path / "state"
    _write_metadata(
        state,
        "old-account",
        "old-organisation",
        "local_old.json",
        "11111111-1111-1111-1111-111111111111",
    )
    _write_metadata(
        state,
        "other-account",
        "other-organisation",
        "local_other.json",
        "22222222-2222-2222-2222-222222222222",
    )
    new_pair = (
        state
        / "appdata"
        / "Claude"
        / "claude-code-sessions"
        / "new-account"
        / "new-organisation"
    )
    new_pair.mkdir(parents=True)

    json_result = _run_diagnose(state, "--json")
    payload = json.loads(json_result.stdout)

    assert payload["snapshot"]["desktop_session_pairs"] == [
        {
            "account_uuid": "new-account",
            "organisation_uuid": "new-organisation",
            "local_metadata_count": 0,
        },
        {
            "account_uuid": "old-account",
            "organisation_uuid": "old-organisation",
            "local_metadata_count": 1,
        },
        {
            "account_uuid": "other-account",
            "organisation_uuid": "other-organisation",
            "local_metadata_count": 1,
        },
    ]
    assert {
        problem["id"] for problem in payload["matched_problems"]
    } >= {"account-uuid-rotation"}

    human_result = _run_diagnose(state)
    assert "Desktop pairs : 3" in human_result.stdout
    assert "account=new-account organisation=new-organisation local_*.json=0" in human_result.stdout
    assert "account=old-account organisation=old-organisation local_*.json=1" in human_result.stdout
    assert "Logout/login rotated the active account/organisation UUID pair" in human_result.stdout
