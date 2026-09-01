"""Real-SQLite dry-run regression for VS Code session recovery."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_FIXTURE = Path(__file__).parent / "fixtures" / "vscode-dry-run-existing-cache"
RECOVER = REPO_ROOT / "tools" / "sessions" / "recover_vscode_sessions.py"
TOOLS = REPO_ROOT / "tools"
SESSIONS = TOOLS / "sessions"
for import_root in (str(TOOLS), str(SESSIONS)):
    if import_root not in sys.path:
        sys.path.insert(0, import_root)

import recover_vscode_sessions as recovery  # noqa: E402


def _fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*"), key=lambda value: value.as_posix()):
        digest.update(item.relative_to(path).as_posix().encode())
        digest.update(b"\0")
        if item.is_file():
            digest.update(item.read_bytes())
    return digest.hexdigest()


def _create_cache_database(path: Path, cache):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)"
        )
        connection.execute(
            "INSERT INTO ItemTable(key, value) VALUES (?, ?)",
            ("agentSessions.model.cache", json.dumps(cache)),
        )


def _run_recovery(projects: Path, workspace: Path, *extra_args: str):
    return subprocess.run(
        [
            sys.executable,
            str(RECOVER),
            "--projects-dir", str(projects),
            "--workspace-dir", str(workspace),
            *extra_args,
        ],
        cwd=projects.parent,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def test_existing_cache_dry_run_reports_candidate_without_writes(tmp_path):
    source_before = _fingerprint(SOURCE_FIXTURE)
    projects = tmp_path / "projects"
    shutil.copytree(SOURCE_FIXTURE / "projects", projects)
    workspace = tmp_path / "workspaceStorage"
    database = workspace / "workspace-one" / "state.vscdb"
    database.parent.mkdir(parents=True)

    existing_cache = [{
        "providerType": "claude-code",
        "resource": "claude-code:/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "label": "Existing cached session",
    }]
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
        connection.execute(
            "INSERT INTO ItemTable(key, value) VALUES (?, ?)",
            ("agentSessions.model.cache", json.dumps(existing_cache)),
        )
    database_before = database.read_bytes()

    result = subprocess.run(
        [
            sys.executable,
            str(RECOVER),
            "--projects-dir", str(projects),
            "--workspace-dir", str(workspace),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "aaaaaaaa-aaa" in result.stdout
    assert "Expected dry-run candidate" in result.stdout
    assert "Read-only orphan-fork marker audit: 1 candidate(s)" in result.stdout
    audit_output = result.stdout.split(
        "Read-only orphan-fork marker audit:", 1
    )[1]
    assert "aaaaaaaa-aaa" in audit_output
    assert "cccccccc-ccc" not in audit_output
    assert "no last-prompt visibility marker" in result.stdout
    assert "cccccccc-ccc" in result.stdout
    assert "DRY RUN" in result.stdout
    assert database.read_bytes() == database_before
    with sqlite3.connect(database) as connection:
        value = connection.execute(
            "SELECT value FROM ItemTable WHERE key='agentSessions.model.cache'"
        ).fetchone()[0]
    assert json.loads(value) == existing_cache
    assert _fingerprint(SOURCE_FIXTURE) == source_before
    assert not list(tmp_path.rglob("sessions-index.json"))
    assert not list(SOURCE_FIXTURE.rglob("sessions-index.json"))


def test_existing_cache_audits_cached_and_suppresses_incomplete_transcripts(
    tmp_path,
):
    projects = tmp_path / "projects"
    shutil.copytree(SOURCE_FIXTURE / "projects", projects)
    slug = projects / "-fixture-project"

    incomplete = slug / "dddddddd-dddd-4ddd-8ddd-dddddddddddd.jsonl"
    incomplete.write_text(
        "".join([
            json.dumps({
                "type": "user",
                "message": {"role": "user", "content": "unanswered"},
            }),
            "\n",
            json.dumps({
                "type": "assistant",
                "message": {"role": "assistant", "content": "response"},
            }),
            "\n",
            json.dumps({
                "type": "user",
                "message": {"role": "user", "content": "still waiting"},
            }),
            "\n",
        ]),
        encoding="utf-8",
    )
    invalid_message = slug / "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee.jsonl"
    invalid_message.write_text(
        "".join([
            '{"type":"user","message":{"role":"user"}}\n',
            json.dumps({
                "type": "assistant",
                "message": {"role": "assistant", "content": "response"},
            }),
            "\n",
        ]),
        encoding="utf-8",
    )
    invalid_encoding = slug / "ffffffff-ffff-4fff-8fff-ffffffffffff.jsonl"
    invalid_encoding.write_bytes(
        b'{"type":"user","message":{"role":"user","content":"request"}}\n'
        b'{"type":"assistant","message":{"role":"assistant","content":"response"}}\n'
        b"\xff\n"
    )
    malformed = slug / "99999999-9999-4999-8999-999999999999.jsonl"
    malformed.write_text(
        "".join([
            json.dumps({
                "type": "user",
                "message": {"role": "user", "content": "request"},
            }),
            "\n",
            json.dumps({
                "type": "assistant",
                "message": {"role": "assistant", "content": "response"},
            }),
            "\nnot-json\n",
        ]),
        encoding="utf-8",
    )

    all_ids = [
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
        "ffffffff-ffff-4fff-8fff-ffffffffffff",
        "99999999-9999-4999-8999-999999999999",
    ]
    workspace = tmp_path / "workspaceStorage"
    _create_cache_database(
        workspace / "workspace-one" / "state.vscdb",
        [{"resource": f"claude-code:/{session_id}"} for session_id in all_ids],
    )

    result = subprocess.run(
        [
            sys.executable,
            str(RECOVER),
            "--projects-dir", str(projects),
            "--workspace-dir", str(workspace),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "All disk sessions are already present" in result.stdout
    assert "Nothing to recover." in result.stdout
    audit_output = result.stdout.split(
        "Read-only orphan-fork marker audit:", 1
    )[1]
    assert "1 candidate(s)" in audit_output
    assert "aaaaaaaa-aaa" in audit_output
    for session_id in all_ids[1:]:
        assert session_id[:12] not in audit_output
    assert "would be injected" not in result.stdout


def test_apply_audits_cached_transcripts_and_preserves_cache_semantics(
    tmp_path, monkeypatch, capsys,
):
    projects = tmp_path / "projects"
    shutil.copytree(SOURCE_FIXTURE / "projects", projects)
    missing = projects / "-fixture-project" / (
        "dddddddd-dddd-4ddd-8ddd-dddddddddddd.jsonl"
    )
    missing.write_text(
        "".join([
            json.dumps({
                "type": "user",
                "message": {"role": "user", "content": "request"},
            }),
            "\n",
            json.dumps({
                "type": "assistant",
                "message": {"role": "assistant", "content": "response"},
            }),
            "\n",
        ]),
        encoding="utf-8",
    )
    cached_ids = [
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    ]
    workspace = tmp_path / "workspaceStorage"
    database = workspace / "workspace-one" / "state.vscdb"
    _create_cache_database(
        database,
        [{"resource": f"claude-code:/{session_id}"} for session_id in cached_ids],
    )

    monkeypatch.setattr(recovery, "_vscode_running", lambda: False)
    monkeypatch.setattr(
        recovery.sys,
        "argv",
        [
            "recover_vscode_sessions.py",
            "--projects-dir", str(projects),
            "--workspace-dir", str(workspace),
            "--apply",
        ],
    )
    monkeypatch.chdir(tmp_path)

    assert recovery.main() is None
    rendered = capsys.readouterr().out
    audit_output = rendered.split(
        "Read-only orphan-fork marker audit:", 1
    )[1]
    assert "2 candidate(s)" in audit_output
    assert "aaaaaaaa-aaa" in audit_output
    assert "dddddddd-ddd" in audit_output
    assert "cccccccc-ccc" not in audit_output
    assert "Injected 1 entry/entries" in rendered

    with sqlite3.connect(database) as connection:
        cache = json.loads(connection.execute(
            "SELECT value FROM ItemTable "
            "WHERE key='agentSessions.model.cache'"
        ).fetchone()[0])
    assert [entry["resource"] for entry in cache] == [
        "claude-code:/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "claude-code:/cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        "claude-code:/dddddddd-dddd-4ddd-8ddd-dddddddddddd",
    ]
    assert list((tmp_path / "repair-backup").glob("*.json"))
