"""Focused tests for the explicit, read-only Windows path-alias check."""

import json
from pathlib import Path
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS = REPO_ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import diagnose  # noqa: E402


ORIGINAL_CWD = r"d:\CustomTools\dev\claude"
RESOLVED_CWD = r"C:\D-Data\CustomTools\dev\claude"
ORIGINAL_SLUG = "d--CustomTools-dev-claude"
RESOLVED_SLUG = "C--D-Data-CustomTools-dev-claude"


def _write_jsonl(projects, slug, session_id="11111111-1111-4111-8111-111111111111"):
    directory = projects / slug
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{session_id}.jsonl").write_text("{}\n", encoding="utf-8")


def test_volume_mount_candidate_reports_original_files_without_claiming_identity(tmp_path):
    projects = tmp_path / "projects"
    _write_jsonl(projects, ORIGINAL_SLUG)
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))

    report = diagnose.build_path_alias_diagnostic(
        str(projects), ORIGINAL_CWD, RESOLVED_CWD
    )

    assert report["status"] == "observed_slug_mismatch_candidate"
    assert report["path_form_classification"] == "ancestor-volume-mount-candidate"
    assert report["physical_identity"] == "unverified"
    assert report["original"]["jsonl_count"] == 1
    assert report["resolved"]["jsonl_count"] == 0
    assert report["evidence"] == {
        "path_forms_differ": True,
        "slugs_differ": True,
        "original_jsonl_present": True,
        "resolved_jsonl_present": False,
    }
    assert sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")) == before
    assert all("repair" not in key.lower() for key in report)


def test_existing_reader_slug_is_reported_without_repair_selection(tmp_path):
    projects = tmp_path / "projects"
    _write_jsonl(projects, ORIGINAL_SLUG)
    _write_jsonl(projects, RESOLVED_SLUG, "22222222-2222-4222-8222-222222222222")

    report = diagnose.build_path_alias_diagnostic(
        str(projects), ORIGINAL_CWD, RESOLVED_CWD
    )

    assert report["status"] == "both_slugs_have_transcripts"
    assert report["original"]["jsonl_count"] == 1
    assert report["resolved"]["jsonl_count"] == 1
    assert report["physical_identity"] == "unverified"


def test_posix_slug_lookup_does_not_fold_case(tmp_path):
    projects = tmp_path / "projects"
    original_cwd = "/tmp/Project"
    resolved_cwd = "/tmp/project"
    resolved_slug = diagnose.slug_encode(resolved_cwd)
    _write_jsonl(projects, resolved_slug)

    report = diagnose.build_path_alias_diagnostic(
        str(projects), original_cwd, resolved_cwd
    )

    assert report["status"] == "resolved_slug_has_transcripts_only"
    assert report["original"]["jsonl_count"] == 0
    assert report["resolved"]["jsonl_count"] == 1


def test_unavailable_project_root_does_not_claim_slug_absence(tmp_path):
    report = diagnose.build_path_alias_diagnostic(
        str(tmp_path / "missing-projects"), ORIGINAL_CWD, RESOLVED_CWD
    )

    assert report["status"] == "insufficient_evidence"
    assert report["evidence"]["original_jsonl_present"] is None
    assert report["evidence"]["resolved_jsonl_present"] is None
    assert report["original"]["scan_status"] == "unavailable"
    assert report["resolved"]["scan_status"] == "unavailable"


def test_cli_reports_path_alias_observation_in_json_without_mutating_fixture(tmp_path):
    state = tmp_path / "state"
    metadata_dir = (
        state
        / "appdata"
        / "Claude"
        / "claude-code-sessions"
        / "account"
        / "organisation"
    )
    metadata_dir.mkdir(parents=True)
    session_id = "33333333-3333-4333-8333-333333333333"
    (metadata_dir / "local_one.json").write_text(
        json.dumps(
            {
                "sessionId": "local_one",
                "cliSessionId": session_id,
                "cwd": ORIGINAL_CWD,
                "createdAt": 1,
                "updatedAt": 1,
            }
        ),
        encoding="utf-8",
    )
    _write_jsonl(state / "projects", ORIGINAL_SLUG, session_id)
    before = sorted(str(path.relative_to(state)) for path in state.rglob("*"))

    result = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "diagnose.py"),
            "--state",
            str(state),
            "--json",
            "--cwd",
            ORIGINAL_CWD,
            "--resolved-cwd",
            RESOLVED_CWD,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    payload = json.loads(result.stdout)

    report = payload["path_alias_diagnostic"]
    assert report["status"] == "observed_slug_mismatch_candidate"
    assert report["original"]["jsonl_count"] == 1
    assert report["resolved"]["jsonl_count"] == 0
    assert report["physical_identity"] == "unverified"
    assert sorted(str(path.relative_to(state)) for path in state.rglob("*")) == before


def test_help_exposes_observed_path_inputs():
    result = subprocess.run(
        [sys.executable, str(TOOLS / "diagnose.py"), "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )

    assert "--cwd PATH" in result.stdout
    assert "--resolved-cwd PATH" in result.stdout
