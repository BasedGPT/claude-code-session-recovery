"""Privacy controls for diagnostic and recovery output."""

import os
import sys


TOOLS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools")
SESSIONS = os.path.join(TOOLS, "sessions")
sys.path.insert(0, TOOLS)
sys.path.insert(0, SESSIONS)

import diagnose
import find_missing_jsonls_in_backup as backup_search


def test_redact_user_home(monkeypatch):
    monkeypatch.setattr(diagnose.os.path, "expanduser", lambda _value: r"C:\Users\PrivateName")
    monkeypatch.setattr(diagnose.platform, "system", lambda: "Windows")

    result = diagnose._redact_user_home(
        r"C:\Users\PrivateName\AppData\Local\Packages\Claude_example"
    )

    assert result == r"%USERPROFILE%\AppData\Local\Packages\Claude_example"
    assert "PrivateName" not in result


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
