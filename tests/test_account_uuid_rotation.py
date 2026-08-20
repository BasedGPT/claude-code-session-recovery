"""Tests for Desktop account/organisation UUID rotation diagnostics."""

import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS = REPO_ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import diagnose as diagnose_module  # noqa: E402

DIAGNOSE = REPO_ROOT / "tools" / "diagnose.py"
SYNTH = REPO_ROOT / "tools" / "sessions" / "synth_session_metadata.py"


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

    assert "_desktop_session_pair_identities" not in payload["snapshot"]
    assert payload["snapshot"]["desktop_session_pairs"] == [
        {
            "pair_label": "pair-01",
            "local_metadata_count": 0,
        },
        {
            "pair_label": "pair-02",
            "local_metadata_count": 1,
        },
        {
            "pair_label": "pair-03",
            "local_metadata_count": 1,
        },
    ]
    assert {
        problem["id"] for problem in payload["matched_problems"]
    } >= {"account-uuid-rotation"}

    human_result = _run_diagnose(state)
    assert "Desktop pairs : 3" in human_result.stdout
    assert "pair-01 local_*.json=0" in human_result.stdout
    assert "pair-02 local_*.json=1" in human_result.stdout
    assert "pair-03 local_*.json=1" in human_result.stdout
    assert "Logout/login rotated the active account/organisation UUID pair" in human_result.stdout

    for raw_identifier in (
        "new-account",
        "new-organisation",
        "old-account",
        "old-organisation",
        "other-account",
        "other-organisation",
    ):
        assert raw_identifier not in json_result.stdout
        assert raw_identifier not in human_result.stdout


def test_opaque_pair_labels_are_deterministic_by_hidden_identity_order(tmp_path):
    state = tmp_path / "state"
    # Create the lexically-later pair first to prove creation order does not
    # control labels. Give the pairs different counts so ordering is visible.
    _write_metadata(
        state,
        "z-account",
        "z-organisation",
        "local_z.json",
        "22222222-2222-2222-2222-222222222222",
    )
    (state / "appdata" / "Claude" / "claude-code-sessions" / "a-account" / "a-organisation").mkdir(
        parents=True
    )

    first = json.loads(_run_diagnose(state, "--json").stdout)
    second = json.loads(_run_diagnose(state, "--json").stdout)

    expected = [
        {"pair_label": "pair-01", "local_metadata_count": 0},
        {"pair_label": "pair-02", "local_metadata_count": 1},
    ]
    assert first["snapshot"]["desktop_session_pairs"] == expected
    assert second["snapshot"]["desktop_session_pairs"] == expected
    assert first["diagnosis_id"] == second["diagnosis_id"]
    assert "a-account" not in json.dumps(first)
    assert "a-organisation" not in json.dumps(first)
    assert "z-account" not in json.dumps(first)
    assert "z-organisation" not in json.dumps(first)


def test_multiple_populated_pairs_are_ambiguous_rotation(tmp_path):
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

    payload = json.loads(_run_diagnose(state, "--json").stdout)

    assert payload["snapshot"]["account_uuid_rotation_count"] == 1
    assert {
        problem["id"] for problem in payload["matched_problems"]
    } >= {"account-uuid-rotation"}


def test_synthesis_refuses_ambiguous_metadata_destination(tmp_path):
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
    empty_pair = (
        state
        / "appdata"
        / "Claude"
        / "claude-code-sessions"
        / "new-account"
        / "new-organisation"
    )
    empty_pair.mkdir(parents=True)
    orphan_dir = state / "projects" / "C--Users-fixture-user-project"
    orphan_dir.mkdir(parents=True, exist_ok=True)
    (orphan_dir / "33333333-3333-3333-3333-333333333333.jsonl").write_text(
        "{}\n", encoding="utf-8"
    )

    diagnosis = json.loads(_run_diagnose(state, "--json").stdout)
    result = subprocess.run(
        [
            sys.executable,
            str(SYNTH),
            "--state",
            str(state),
            "--diagnosis-id",
            diagnosis["diagnosis_id"],
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert result.returncode == 3
    assert "Multiple Claude Desktop account/organisation metadata pairs" in result.stdout
    assert "SYNTH" not in result.stdout
    problems = {problem["id"]: problem for problem in diagnosis["matched_problems"]}
    assert set(problems) >= {"account-uuid-rotation", "orphan-jsonl-no-metadata"}
    assert problems["orphan-jsonl-no-metadata"]["mutator"] is None
    assert problems["orphan-jsonl-no-metadata"]["next_command"] is None


def test_synthesis_route_suppression_does_not_mutate_loaded_row():
    source_row = {
        "id": "orphan-jsonl-no-metadata",
        "mutator": "tools/sessions/synth_session_metadata.py",
        "safety": "fixture safety",
    }

    filtered = diagnose_module._suppress_ambiguous_synthesis_routes(
        [source_row],
        {"desktop_session_pairs": [{}, {}]},
    )

    assert source_row["mutator"] == "tools/sessions/synth_session_metadata.py"
    assert "next_command" not in source_row
    assert filtered[0] is not source_row
    assert filtered[0]["mutator"] is None
    assert filtered[0]["next_command"] is None
