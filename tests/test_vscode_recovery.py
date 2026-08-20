"""Focused tests for shared VS Code recovery path resolution."""

from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS = str(REPO_ROOT / "tools")
SESSIONS = str(REPO_ROOT / "tools" / "sessions")
for import_root in (TOOLS, SESSIONS):
    if import_root not in sys.path:
        sys.path.insert(0, import_root)

import audit_vscode_session_surfaces as audit  # noqa: E402
import platform_support  # noqa: E402
import recover_vscode_sessions as recovery  # noqa: E402


def test_audit_and_recovery_import_the_same_shared_default_resolvers():
    assert (
        audit.default_claude_sessions_index_dir
        is recovery.default_claude_sessions_index_dir
        is platform_support.default_claude_sessions_index_dir
    )
    assert (
        audit.default_vscode_workspace_storage_dir
        is recovery.default_vscode_workspace_storage_dir
        is platform_support.default_vscode_workspace_storage_dir
    )


def test_recovery_uses_shared_defaults_and_reports_resolved_paths(
    tmp_path, monkeypatch, capsys
):
    projects = tmp_path / "shared-projects"
    workspace = tmp_path / "shared-workspaceStorage"
    monkeypatch.setattr(
        recovery, "default_claude_sessions_index_dir", lambda: str(projects)
    )
    monkeypatch.setattr(
        recovery, "default_vscode_workspace_storage_dir", lambda: str(workspace)
    )
    monkeypatch.setattr(recovery.sys, "argv", ["recover_vscode_sessions.py"])

    assert recovery.main() is None
    rendered = capsys.readouterr().out

    assert "Workspace storage : {}".format(workspace) in rendered
    assert "Projects dir      : {}".format(projects) in rendered


def test_recovery_cli_path_overrides_bypass_shared_defaults(
    tmp_path, monkeypatch, capsys
):
    projects = tmp_path / "override-projects"
    workspace = tmp_path / "override-workspaceStorage"

    def unexpected_default():
        raise AssertionError("shared default called despite an explicit override")

    monkeypatch.setattr(
        recovery, "default_claude_sessions_index_dir", unexpected_default
    )
    monkeypatch.setattr(
        recovery, "default_vscode_workspace_storage_dir", unexpected_default
    )
    monkeypatch.setattr(
        recovery.sys,
        "argv",
        [
            "recover_vscode_sessions.py",
            "--projects-dir", str(projects),
            "--workspace-dir", str(workspace),
        ],
    )

    assert recovery.main() is None
    rendered = capsys.readouterr().out

    assert "Workspace storage : {}".format(workspace) in rendered
    assert "Projects dir      : {}".format(projects) in rendered
