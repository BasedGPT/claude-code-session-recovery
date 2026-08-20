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


def _fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*"), key=lambda value: value.as_posix()):
        digest.update(item.relative_to(path).as_posix().encode())
        digest.update(b"\0")
        if item.is_file():
            digest.update(item.read_bytes())
    return digest.hexdigest()


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
