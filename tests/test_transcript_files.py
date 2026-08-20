"""Focused parity checks for shared transcript-file interpretation."""

import os
import sys

import pytest


TOOLS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools")
sys.path.insert(0, TOOLS)

import transcript_files


def _write_transcript(tmp_path, content):
    path = tmp_path / "session.jsonl"
    path.write_text(content, encoding="utf-8")
    return path


def test_object_iterator_skips_malformed_and_non_object_records(tmp_path):
    path = _write_transcript(
        tmp_path,
        "not-json\n"
        + '{"oversized": ' + ("1" * 5000) + "}\n"
        + "[\"not-an-object\"]\n{\"type\": \"assistant\"}\n",
    )

    assert list(transcript_files.iter_transcript_records(path)) == [{"type": "assistant"}]


def test_timestamp_forms_keep_numeric_and_iso_policies_separate():
    assert transcript_files.timestamp_ms(1704067200123.9) == 1704067200123
    assert transcript_files.timestamp_ms("2024-01-01T00:00:00Z") == 1704067200000
    assert transcript_files.timestamp_ms("not-a-time") is None
    assert transcript_files.iso_timestamp_ms("2024-01-01T00:00:00+00:00") == 1704067200000
    with pytest.raises(AttributeError):
        transcript_files.iso_timestamp_ms(1704067200000)


def test_assistant_counter_stops_when_the_diagnosis_threshold_is_reached(monkeypatch):
    def records(_path, *, errors, object_records=True):
        yield {"type": "assistant"}
        yield {"type": "assistant"}
        raise AssertionError("counter read past its stop threshold")

    monkeypatch.setattr(transcript_files, "iter_transcript_records", records)

    assert transcript_files.count_assistant_records("unused", stop_at=2) == 2


def test_repair_scan_stops_once_its_two_fields_are_found(monkeypatch):
    def records(_path, *, errors, object_records):
        yield {"timestamp": "2024-01-01T00:00:00Z"}
        yield {
            "type": "user",
            "message": {"role": "user", "content": "first request"},
        }
        raise AssertionError("repair scan read past its two fields")

    monkeypatch.setattr(transcript_files, "iter_transcript_records", records)

    assert transcript_files.first_iso_timestamp_and_user("unused") == (
        1704067200000,
        "first request",
    )


def test_strict_cwd_reader_keeps_non_object_record_failure(tmp_path):
    path = _write_transcript(tmp_path, "[\"not-an-object\"]\n")

    with pytest.raises(AttributeError):
        transcript_files.first_cwd(path)


def test_synthesis_summary_skips_bad_json_but_preserves_raw_iso_values(tmp_path):
    path = _write_transcript(
        tmp_path,
        "bad json\n"
        "{\"timestamp\": \"2024-01-01T00:00:00Z\", \"cwd\": \"C:\\\\project\", "
        "\"type\": \"user\", \"message\": {\"role\": \"user\", \"content\": \"secret prompt\"}}\n"
        "{\"timestamp\": \"2024-01-01T00:01:00Z\", \"message\": {\"role\": \"assistant\", \"model\": \"claude-test\"}}\n",
    )

    assert transcript_files.synthesis_summary(path) == {
        "first_ts": "2024-01-01T00:00:00Z",
        "last_ts": "2024-01-01T00:01:00Z",
        "first_user_text": "secret prompt",
        "last_model": "claude-test",
        "cwd": r"C:\project",
        "user_turn_count": 1,
    }


def test_interpreter_never_prints_transcript_text(tmp_path, capsys):
    path = _write_transcript(
        tmp_path,
        '{"type": "custom-title", "customTitle": "Private Client Project"}\n',
    )

    assert transcript_files.cache_metadata(path)[0] == "Private Client Project"
    assert capsys.readouterr().out == ""
