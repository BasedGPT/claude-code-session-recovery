"""Tests for tools/sessions/session_watch.py.

Each test exercises _run() directly with tmp_path fixtures so no real
~/.claude state is touched.
"""
import sys
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools", "sessions"))

import session_watch  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_jsonl(projects_dir, slug, filename, content=b"data"):
    d = projects_dir / slug
    d.mkdir(parents=True, exist_ok=True)
    p = d / filename
    p.write_bytes(content)
    return p


def _write_manifest(state_dir, entries):
    """Write latest.tsv with {relpath: size} entries (sha256/mtime filled minimally)."""
    latest = state_dir / "latest.tsv"
    lines = []
    for relpath, size in entries.items():
        lines.append(f"aabbcc\t{size}\t0.0\t{relpath}")
    latest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return latest


def _run(tmp_path, test_mode=False, monkeypatch_fn=None):
    projects = tmp_path / "projects"
    state = tmp_path / "state"
    settings = tmp_path / "settings.json"
    settings.write_text('{"cleanupPeriodDays": 36500}', encoding="utf-8")
    if monkeypatch_fn:
        monkeypatch_fn(projects, state, settings)
    return projects, state, settings


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_first_run_creates_baseline_no_alert(tmp_path, capsys):
    """First run with no latest.tsv: logs BASELINE, no ALERT, exits 0."""
    projects = tmp_path / "projects"
    state = tmp_path / "state"
    settings = tmp_path / "settings.json"
    settings.write_text("{}", encoding="utf-8")

    _make_jsonl(projects, "proj-a", "session.jsonl", b"hello")

    rc = session_watch._run(projects, state, settings, test_mode=False)

    assert rc == 0
    log_text = (state / "watch.log").read_text(encoding="utf-8")
    assert "BASELINE" in log_text
    assert "ALERT" not in log_text
    assert (state / "latest.tsv").is_file()
    assert (state / "latest.version").is_file()

    # stderr should mention BASELINE, not ALERT
    captured = capsys.readouterr()
    assert "BASELINE" in captured.err
    assert "ALERT" not in captured.err


def test_file_disappears_fires_alert(tmp_path, capsys):
    """A transcript present in latest.tsv but missing from disk triggers ALERT."""
    projects = tmp_path / "projects"
    projects.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    settings = tmp_path / "settings.json"
    settings.write_text('{"cleanupPeriodDays": 36500}', encoding="utf-8")

    relpath = "proj-a/session.jsonl"
    _write_manifest(state, {relpath: 200})
    (state / "latest.version").write_text("1.0.0", encoding="utf-8")

    # projects dir exists but file is gone
    (projects / "proj-a").mkdir(parents=True, exist_ok=True)

    rc = session_watch._run(projects, state, settings, test_mode=False)

    assert rc == 0
    captured = capsys.readouterr()
    assert "ALERT" in captured.err
    assert "DISAPPEARED" in captured.err
    assert relpath in captured.err

    log_text = (state / "watch.log").read_text(encoding="utf-8")
    assert "ALERT" in log_text
    assert relpath in log_text


def test_file_shrinks_fires_alert(tmp_path, capsys):
    """A transcript that shrinks (size < prev) triggers ALERT."""
    from pathlib import Path as _Path
    projects = tmp_path / "projects"
    state = tmp_path / "state"
    state.mkdir()
    settings = tmp_path / "settings.json"
    settings.write_text("{}", encoding="utf-8")

    # Use os.sep-native relpath so it matches what _scan() produces on this OS.
    relpath = str(_Path("proj-b") / "session.jsonl")
    _write_manifest(state, {relpath: 500})
    (state / "latest.version").write_text("1.0.0", encoding="utf-8")

    # file exists on disk but is smaller than the manifest records
    _make_jsonl(projects, "proj-b", "session.jsonl", b"x" * 100)

    rc = session_watch._run(projects, state, settings, test_mode=False)

    assert rc == 0
    captured = capsys.readouterr()
    assert "ALERT" in captured.err
    # Confirm this is a shrink, not a disappear (the file is on disk, just smaller)
    assert "SHRANK" in captured.err
    assert "proj-b" in captured.err


def test_test_flag_prints_alert_but_does_not_update_manifest(tmp_path, capsys):
    """--test mode: alert is printed but latest.tsv is not changed."""
    projects = tmp_path / "projects"
    projects.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    settings = tmp_path / "settings.json"
    settings.write_text("{}", encoding="utf-8")

    relpath = "proj-c/session.jsonl"
    original_manifest = f"aabbcc\t300\t0.0\t{relpath}\n"
    (state / "latest.tsv").write_text(original_manifest, encoding="utf-8")
    (state / "latest.version").write_text("1.0.0", encoding="utf-8")

    (projects / "proj-c").mkdir(parents=True, exist_ok=True)
    # file missing from disk → would trigger disappeared ALERT

    rc = session_watch._run(projects, state, settings, test_mode=True)

    assert rc == 0
    captured = capsys.readouterr()
    assert "ALERT" in captured.err
    assert relpath in captured.err

    # latest.tsv must be unchanged
    assert (state / "latest.tsv").read_text(encoding="utf-8") == original_manifest
    # watch.log must not exist
    assert not (state / "watch.log").exists()


def test_exception_mid_run_exits_zero(tmp_path, monkeypatch, capsys):
    """If _scan raises, the hook still exits 0 — it must never block a session."""
    projects = tmp_path / "projects"
    state = tmp_path / "state"
    settings = tmp_path / "settings.json"
    settings.write_text("{}", encoding="utf-8")

    def boom(_projects_dir):
        raise RuntimeError("simulated scan failure")

    monkeypatch.setattr(session_watch, "_scan", boom)

    rc = session_watch._run(projects, state, settings, test_mode=False)

    assert rc == 0
    captured = capsys.readouterr()
    assert "ERROR" in captured.err


def test_ok_path_writes_log_not_stderr(tmp_path, capsys):
    """No loss: watch.log gets OK line; stderr is clean."""
    from pathlib import Path as _Path
    projects = tmp_path / "projects"
    state = tmp_path / "state"
    state.mkdir()
    settings = tmp_path / "settings.json"
    settings.write_text('{"cleanupPeriodDays": 36500}', encoding="utf-8")

    # Create the file and write a matching manifest entry.
    _make_jsonl(projects, "proj-ok", "session.jsonl", b"content")
    relpath = str(_Path("proj-ok") / "session.jsonl")
    size = len(b"content")
    _write_manifest(state, {relpath: size})
    (state / "latest.version").write_text("1.0.0", encoding="utf-8")

    rc = session_watch._run(projects, state, settings, test_mode=False)

    assert rc == 0
    log_text = (state / "watch.log").read_text(encoding="utf-8")
    assert "OK" in log_text
    assert "ALERT" not in log_text

    captured = capsys.readouterr()
    assert "ALERT" not in captured.err
    assert "BASELINE" not in captured.err


def test_manifest_tsv_format(tmp_path):
    """latest.tsv written in exact sha256 TAB size TAB mtime TAB relpath format."""
    from pathlib import Path as _Path
    projects = tmp_path / "projects"
    state = tmp_path / "state"
    settings = tmp_path / "settings.json"
    settings.write_text("{}", encoding="utf-8")

    _make_jsonl(projects, "p", "x.jsonl", b"abc")

    session_watch._run(projects, state, settings, test_mode=False)

    lines = (state / "latest.tsv").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    parts = lines[0].split("\t")
    assert len(parts) == 4, f"Expected 4 TSV columns, got {len(parts)}: {lines[0]!r}"
    sha, size, mtime, relpath = parts
    assert len(sha) == 64, "sha256 should be 64 hex chars"
    assert size.isdigit(), "size should be integer"
    float(mtime)  # should not raise
    assert relpath == str(_Path("p") / "x.jsonl")


def test_manifest_rotation(tmp_path):
    """With 55 timestamped manifests present, rotation leaves exactly 50."""
    state = tmp_path / "state"
    state.mkdir()
    # Use realistic 19-digit nanosecond-epoch names so lexical == chronological.
    base_ns = 1_748_000_000_000_000_000
    for i in range(55):
        (state / f"manifest.{base_ns + i}.tsv").write_text(str(i), encoding="utf-8")

    session_watch._rotate_manifests(state)

    remaining = sorted(state.glob("manifest.*.tsv"))
    assert len(remaining) == 50
    # The 5 oldest (lowest epoch) should be gone.
    for i in range(5):
        assert not (state / f"manifest.{base_ns + i}.tsv").exists(), (
            f"manifest.{base_ns + i}.tsv should have been deleted"
        )
    # The 50 newest should survive.
    for i in range(5, 55):
        assert (state / f"manifest.{base_ns + i}.tsv").exists(), (
            f"manifest.{base_ns + i}.tsv should have been kept"
        )
