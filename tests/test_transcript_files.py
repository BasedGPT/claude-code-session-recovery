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


def _write_inventory_transcripts(root, slug_count=1, files_per_slug=1):
    paths = []
    sequence = 0
    for slug_index in range(slug_count):
        slug = root / f"slug-{slug_index:02d}"
        slug.mkdir(parents=True, exist_ok=True)
        for _file_index in range(files_per_slug):
            sequence += 1
            session_id = f"{sequence:08d}-0000-0000-0000-{sequence:012d}"
            path = slug / f"{session_id}.jsonl"
            path.write_text("{}\n", encoding="utf-8")
            paths.append(path)
    return paths


@pytest.mark.parametrize(
    ("cap_name", "error_code"),
    [
        ("MAX_PROJECT_DIRECTORY_ENTRIES", "projects_entry_cap_exceeded"),
        ("MAX_TRANSCRIPT_SLUG_DIRECTORIES", "slug_directory_cap_exceeded"),
    ],
)
def test_transcript_directory_caps_are_partial_and_repeatable(
    tmp_path, monkeypatch, cap_name, error_code
):
    _write_inventory_transcripts(tmp_path, slug_count=3)
    monkeypatch.setattr(transcript_files, cap_name, 2)

    first = transcript_files.build_transcript_path_inventory(str(tmp_path))
    second = transcript_files.build_transcript_path_inventory(str(tmp_path))

    assert first == second
    assert first.status == "partial"
    assert first.physical_count == 2
    assert {error.code for error in first.errors} == {error_code}


@pytest.mark.parametrize(
    ("cap_name", "error_code"),
    [
        ("MAX_TRANSCRIPT_ENTRIES_PER_SLUG", "slug_entry_cap_exceeded"),
        ("MAX_TRANSCRIPT_FILES", "transcript_file_cap_exceeded"),
        ("MAX_TRANSCRIPT_PATHS", "transcript_path_cap_exceeded"),
    ],
)
def test_transcript_file_and_path_caps_retain_a_lossless_bounded_prefix(
    tmp_path, monkeypatch, cap_name, error_code
):
    _write_inventory_transcripts(tmp_path, files_per_slug=3)
    monkeypatch.setattr(transcript_files, cap_name, 2)

    inventory = transcript_files.build_transcript_path_inventory(str(tmp_path))

    assert inventory.status == "partial"
    assert inventory.physical_count == 2
    assert sum(len(paths) for paths in inventory.by_session_id.values()) == 2
    assert {error.code for error in inventory.errors} == {error_code}


def test_transcript_global_traversal_cap_is_partial(tmp_path, monkeypatch):
    _write_inventory_transcripts(tmp_path, files_per_slug=2)
    monkeypatch.setattr(transcript_files, "MAX_TRANSCRIPT_TRAVERSAL_ENTRIES", 1)

    inventory = transcript_files.build_transcript_path_inventory(str(tmp_path))

    assert inventory.status == "partial"
    assert inventory.physical_count == 0
    assert {error.code for error in inventory.errors} == {
        "transcript_traversal_cap_exceeded"
    }
