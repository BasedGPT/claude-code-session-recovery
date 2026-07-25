"""Read-only interpretation of Claude Code transcript files.

This module owns the JSONL record boundary shared by diagnostics and session
tools.  It deliberately does not print records or paths: callers decide what
is safe to expose in their own CLI output.
"""
import datetime
import glob
import json
import os


def iter_transcript_records(path, *, errors="strict", object_records=True):
    """Yield parsed records from a transcript, skipping malformed JSON rows.

    ``errors`` is passed to ``open`` so callers can retain their previous
    decoding policy.  ``object_records=False`` retains non-object JSON values
    for callers whose historical record contract rejected them at use time.
    An unreadable transcript is treated as having no usable records; a strict
    decoding failure remains visible to callers that chose the strict policy.
    """
    try:
        with open(path, "r", encoding="utf-8", errors=errors) as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict) or not object_records:
                    yield record
    except OSError:
        return


def timestamp_ms(value):
    """Return a transcript timestamp as epoch milliseconds, or ``None``.

    Claude Code records use either epoch milliseconds or ISO-8601 strings.
    Numeric values keep their recorded unit for compatibility with the VS Code
    cache format; ISO strings are converted to milliseconds.
    """
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    if isinstance(value, str):
        try:
            parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return int(parsed.timestamp() * 1000)
        except (ValueError, OverflowError):
            return None
    return None


def iso_timestamp_ms(value):
    """Return an ISO-8601 transcript timestamp as epoch milliseconds.

    This intentionally keeps the strict historical contract of the repair and
    synthesis tools: non-string values raise ``AttributeError`` at ``replace``
    instead of being silently accepted as numeric timestamps.
    """
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return int(parsed.timestamp() * 1000)
    except ValueError:
        return None


def iter_transcript_paths(projects_dir, session_id_is_valid=None):
    """Yield ``(session_id, absolute_jsonl_path)`` for direct slug children."""
    if session_id_is_valid is None:
        session_id_is_valid = lambda session_id: len(session_id) == 36
    if not os.path.isdir(projects_dir):
        return
    for slug in sorted(os.listdir(projects_dir)):
        slug_dir = os.path.join(projects_dir, slug)
        if not os.path.isdir(slug_dir):
            continue
        for path in glob.glob(os.path.join(slug_dir, "*.jsonl")):
            session_id = os.path.splitext(os.path.basename(path))[0]
            if session_id_is_valid(session_id):
                yield session_id, path


def build_transcript_index(projects_dir, session_id_is_valid=None):
    """Return ``{session_id: absolute_jsonl_path}`` for direct slug children."""
    index = {}
    for session_id, path in iter_transcript_paths(projects_dir, session_id_is_valid):
        index[session_id] = path
    return index


def count_assistant_records(path, stop_at=None):
    """Count assistant records, returning early after ``stop_at`` when given."""
    count = 0
    for record in iter_transcript_records(path, errors="replace"):
        if record.get("type") == "assistant":
            count += 1
            if stop_at is not None and count >= stop_at:
                return count
    return count


def first_cwd(path):
    """Return the first non-empty ``cwd`` recorded in a transcript."""
    for record in iter_transcript_records(path, object_records=False):
        cwd = record.get("cwd")
        if cwd:
            return cwd
    return None


def first_iso_timestamp_and_user(path):
    """Return repair-match fields, preserving its narrow ISO timestamp policy."""
    first_timestamp_ms = None
    first_user_text = None
    for record in iter_transcript_records(path, errors="replace", object_records=False):
        if first_timestamp_ms is None:
            timestamp = record.get("timestamp")
            if timestamp:
                first_timestamp_ms = iso_timestamp_ms(timestamp)
        if first_user_text is None and record.get("type") == "user":
            message = record.get("message", {})
            if isinstance(message, dict) and message.get("role") == "user":
                content = message.get("content", "")
                if isinstance(content, str):
                    first_user_text = content[:80]
                elif isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            first_user_text = item.get("text", "")[:80]
                            break
        if first_timestamp_ms is not None and first_user_text is not None:
            break
    return first_timestamp_ms, first_user_text


def synthesis_summary(path):
    """Return metadata-synthesis fields from one complete transcript scan."""
    summary = {
        "first_ts": None,
        "last_ts": None,
        "first_user_text": None,
        "last_model": None,
        "cwd": None,
        "user_turn_count": 0,
    }
    for record in iter_transcript_records(path, object_records=False):
        timestamp = record.get("timestamp")
        if timestamp:
            if summary["first_ts"] is None:
                summary["first_ts"] = timestamp
            summary["last_ts"] = timestamp

        if summary["cwd"] is None and record.get("cwd"):
            summary["cwd"] = record["cwd"]

        message = record.get("message")
        if isinstance(message, dict):
            role = message.get("role")
            if role == "user" and record.get("type") == "user":
                summary["user_turn_count"] += 1
                if summary["first_user_text"] is None:
                    content = message.get("content")
                    if isinstance(content, str):
                        summary["first_user_text"] = content
                    elif isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict) and item.get("type") == "text":
                                summary["first_user_text"] = item.get("text", "")
                                break
            if role == "assistant":
                model = message.get("model")
                if model:
                    summary["last_model"] = model
    return summary


def cache_metadata(path):
    """Return ``(title, created_ms, last_message_ms)`` for VS Code recovery."""
    title = None
    created_ms = None
    last_message_ms = None
    for record in iter_transcript_records(path, errors="replace"):
        if title is None:
            entry_type = record.get("type", "")
            if entry_type == "custom-title" and record.get("customTitle"):
                title = str(record["customTitle"])
            elif entry_type == "ai-title" and record.get("aiTitle"):
                title = str(record["aiTitle"])

        record_timestamp = timestamp_ms(record.get("timestamp"))
        if record_timestamp is not None:
            if created_ms is None:
                created_ms = record_timestamp
            last_message_ms = record_timestamp
    return title, created_ms, last_message_ms
