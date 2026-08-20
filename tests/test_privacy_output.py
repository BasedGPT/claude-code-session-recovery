"""Privacy controls for diagnostic and recovery output."""

import os
import sys


TOOLS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools")
SESSIONS = os.path.join(TOOLS, "sessions")
WORKTREES = os.path.join(TOOLS, "worktrees")
sys.path.insert(0, TOOLS)
sys.path.insert(0, SESSIONS)
sys.path.insert(0, WORKTREES)

import diagnose
import find_missing_jsonls_in_backup as backup_search
import worktree_shrink


def test_redact_user_home(monkeypatch):
    monkeypatch.setattr(diagnose.os.path, "expanduser", lambda _value: r"C:\Users\PrivateName")
    monkeypatch.setattr(diagnose.platform, "system", lambda: "Windows")

    result = diagnose._redact_user_home(
        r"C:\Users\PrivateName\AppData\Local\Packages\Claude_example"
    )

    assert result == r"%USERPROFILE%\AppData\Local\Packages\Claude_example"
    assert "PrivateName" not in result


def test_redact_snapshot_removes_home_paths_recursively(monkeypatch):
    monkeypatch.setattr(diagnose.os.path, "expanduser", lambda _value: r"C:\Users\PrivateName")
    monkeypatch.setattr(diagnose.platform, "system", lambda: "Windows")

    result = diagnose._redact_snapshot(
        {
            "msix_real_path": r"C:\Users\PrivateName\AppData\Local\Packages\Claude_example",
            "cwd": r"C:\Users\PrivateName\Workspace\repo",
            "nested": [
                {"some_path": r"C:\Users\PrivateName\secret.txt"},
            ],
        }
    )

    assert "PrivateName" not in repr(result)
    assert result["msix_real_path"].startswith("%USERPROFILE%")
    assert result["cwd"].startswith("%USERPROFILE%")


def test_redact_snapshot_never_emits_raw_pair_identity_keys():
    result = diagnose._redact_snapshot(
        {
            "desktop_session_pairs": [
                {
                    "pair_label": "pair-01",
                    "account_uuid": "private-account",
                    "organisation_uuid": "private-organisation",
                    "local_metadata_count": 2,
                }
            ]
        }
    )

    assert result == {
        "desktop_session_pairs": [
            {"pair_label": "pair-01", "local_metadata_count": 2}
        ]
    }
    assert "private-account" not in repr(result)
    assert "private-organisation" not in repr(result)


def test_recursive_search_quiet_hides_identifiers_and_paths(tmp_path, capsys):
    session_id = "12345678-1234-1234-1234-123456789abc"
    private_dir = tmp_path / "Private Client"
    private_dir.mkdir()
    (private_dir / f"{session_id}.jsonl").write_text("{}\n", encoding="utf-8")

    found = backup_search._search_location_recursive(
        str(tmp_path), [session_id], quiet=True
    )

    assert found == 1
    assert capsys.readouterr().out == ""


def test_worktree_sentinel_uses_relative_recovery_paths(tmp_path):
    stub = tmp_path / "stub"
    stub.mkdir()
    manifest = {
        "operation_id": "test-operation",
        "branch": "test-branch",
        "quarantine_path": str(tmp_path / "private-root" / "quarantine"),
        "manifest_path": str(tmp_path / "private-root" / "manifest.json"),
        "start_timestamp": "2026-07-18T00:00:00+00:00",
        "head_sha": "a" * 40,
    }

    worktree_shrink.write_sentinel(str(stub), manifest)
    content = (stub / worktree_shrink.SENTINEL_FILE).read_text(encoding="utf-8")

    assert str(tmp_path) not in content
    assert os.path.relpath(manifest["quarantine_path"], stub) in content
    assert os.path.relpath(manifest["manifest_path"], stub) in content
