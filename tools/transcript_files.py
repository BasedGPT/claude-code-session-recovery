"""Read-only interpretation of Claude Code transcript files.

This module owns the JSONL record boundary shared by diagnostics and session
tools.  It deliberately does not print records or paths: callers decide what
is safe to expose in their own CLI output.
"""
import datetime
import json
import os
from dataclasses import dataclass


# Discovery caps are deliberately generous for real Claude installations while
# keeping hostile directory trees from retaining or traversing unbounded state.
# Tests may monkeypatch them to exercise each boundary without large fixtures.
MAX_PROJECT_DIRECTORY_ENTRIES = 20_000
MAX_TRANSCRIPT_SLUG_DIRECTORIES = 10_000
MAX_TRANSCRIPT_ENTRIES_PER_SLUG = 50_000
MAX_TRANSCRIPT_TRAVERSAL_ENTRIES = 500_000
MAX_TRANSCRIPT_FILES = 250_000
MAX_TRANSCRIPT_PATHS = 200_000


class DuplicateTranscriptIdError(RuntimeError):
    """Raised when a UUID-keyed compatibility index would be ambiguous."""

    def __init__(self, session_ids):
        self.session_ids = tuple(sorted(set(session_ids)))
        detail = ", ".join(self.session_ids[:5])
        if len(self.session_ids) > 5:
            detail += ", ..."
        super().__init__(
            "duplicate transcript session IDs require multimap handling: " + detail
        )


class IncompleteTranscriptInventoryError(RuntimeError):
    """Raised when a selector cannot prove transcript discovery was complete."""

    def __init__(self, errors):
        self.errors = tuple(errors)
        codes = sorted({error.code for error in self.errors})
        detail = ", ".join(codes[:5])
        if len(codes) > 5:
            detail += ", ..."
        super().__init__("transcript inventory is partial: " + detail)


@dataclass(frozen=True)
class TranscriptInventoryError:
    """Opaque transcript discovery error safe for default CLI output."""

    reference: str
    code: str


@dataclass(frozen=True)
class TranscriptPathInventory:
    """Lossless physical transcript inventory grouped by filename stem.

    ``by_session_id`` keeps every physical path.  Callers that need one path
    must prove the tuple has exactly one member before choosing it.
    """

    by_session_id: dict
    physical_count: int
    duplicate_session_ids: tuple
    status: str
    errors: tuple

    @property
    def unique_session_id_count(self):
        return len(self.by_session_id)

    @property
    def is_complete(self):
        return self.status == "complete"

    def paths_for(self, session_id):
        return self.by_session_id.get(session_id, ())


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
                except ValueError:
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
    """Yield known transcript paths, preserving the historical iterator API.

    The iterator raises :class:`IncompleteTranscriptInventoryError` before
    yielding any path when discovery is partial.  Callers that need to inspect
    the known subset and its errors must use
    :func:`build_transcript_path_inventory` directly.
    """
    inventory = build_transcript_path_inventory(projects_dir, session_id_is_valid)
    require_complete_transcript_inventory(inventory)
    for session_id, paths in inventory.by_session_id.items():
        for path in paths:
            yield session_id, path


def _bounded_scandir(path, entry_cap, traversal_state):
    """Return a sorted bounded prefix plus cap flags.

    One extra native directory entry may be examined, but is never retained,
    to distinguish an exact-cap listing from an over-cap listing.  Sorting is
    therefore bounded by ``entry_cap`` rather than the directory's true size.
    """
    retained = []
    entry_cap_exceeded = False
    traversal_cap_exceeded = False
    with os.scandir(path) as entries:
        for entry in entries:
            if traversal_state[0] >= MAX_TRANSCRIPT_TRAVERSAL_ENTRIES:
                traversal_cap_exceeded = True
                break
            if len(retained) >= entry_cap:
                entry_cap_exceeded = True
                break
            retained.append(entry)
            traversal_state[0] += 1
    retained.sort(key=lambda entry: entry.name)
    return retained, entry_cap_exceeded, traversal_cap_exceeded


def build_transcript_path_inventory(projects_dir, session_id_is_valid=None):
    """Return every physical transcript path grouped by session ID.

    The grouping is lossless: duplicate IDs under different slug directories
    are retained in sorted path order rather than silently overwriting one
    another.
    """
    if session_id_is_valid is None:
        session_id_is_valid = lambda session_id: len(session_id) == 36

    grouped = {}
    error_codes = []
    traversal_state = [0]
    slug_directory_count = 0
    transcript_file_count = 0
    transcript_path_count = 0
    stop_inventory = False

    def add_error(code, *, unique=False):
        if not unique or code not in error_codes:
            error_codes.append(code)

    projects_dir = os.path.abspath(projects_dir)
    try:
        slug_entries, entry_cap, traversal_cap = _bounded_scandir(
            projects_dir,
            MAX_PROJECT_DIRECTORY_ENTRIES,
            traversal_state,
        )
        if entry_cap:
            add_error("projects_entry_cap_exceeded", unique=True)
        if traversal_cap:
            add_error("transcript_traversal_cap_exceeded", unique=True)
            stop_inventory = True
    except FileNotFoundError:
        # Session-state fixtures and fresh installs legitimately have no
        # projects directory.  Known absence is a complete empty inventory;
        # an existing but inaccessible root remains partial below.
        slug_entries = []
    except OSError:
        slug_entries = []
        add_error("projects_root_list_failed")

    for slug_entry in slug_entries:
        if stop_inventory:
            break
        try:
            is_directory = slug_entry.is_dir()
        except OSError:
            add_error("slug_stat_failed")
            continue
        if not is_directory:
            continue
        if slug_directory_count >= MAX_TRANSCRIPT_SLUG_DIRECTORIES:
            add_error("slug_directory_cap_exceeded", unique=True)
            break
        slug_directory_count += 1
        try:
            transcript_entries, entry_cap, traversal_cap = _bounded_scandir(
                slug_entry.path,
                MAX_TRANSCRIPT_ENTRIES_PER_SLUG,
                traversal_state,
            )
            if entry_cap:
                add_error("slug_entry_cap_exceeded", unique=True)
            if traversal_cap:
                add_error("transcript_traversal_cap_exceeded", unique=True)
                stop_inventory = True
        except OSError:
            add_error("slug_list_failed")
            continue
        for entry in transcript_entries:
            if not entry.name.endswith(".jsonl"):
                continue
            try:
                is_file = entry.is_file()
            except OSError:
                add_error("transcript_stat_failed")
                continue
            if not is_file:
                add_error("transcript_not_regular_file")
                continue
            if transcript_file_count >= MAX_TRANSCRIPT_FILES:
                add_error("transcript_file_cap_exceeded", unique=True)
                stop_inventory = True
                break
            transcript_file_count += 1
            session_id = os.path.splitext(entry.name)[0]
            if session_id_is_valid(session_id):
                if transcript_path_count >= MAX_TRANSCRIPT_PATHS:
                    add_error("transcript_path_cap_exceeded", unique=True)
                    stop_inventory = True
                    break
                grouped.setdefault(session_id, []).append(os.path.abspath(entry.path))
                transcript_path_count += 1

    by_session_id = {
        session_id: tuple(sorted(paths))
        for session_id, paths in sorted(grouped.items())
    }
    duplicate_session_ids = tuple(
        session_id for session_id, paths in by_session_id.items() if len(paths) > 1
    )
    return TranscriptPathInventory(
        by_session_id=by_session_id,
        physical_count=sum(len(paths) for paths in by_session_id.values()),
        duplicate_session_ids=duplicate_session_ids,
        status="partial" if error_codes else "complete",
        errors=tuple(
            TranscriptInventoryError(
                reference="inventory-entry-{:04d}".format(index),
                code=code,
            )
            for index, code in enumerate(error_codes, start=1)
        ),
    )


def require_complete_transcript_inventory(inventory):
    """Return ``inventory`` or fail closed when discovery was incomplete."""
    if not inventory.is_complete:
        raise IncompleteTranscriptInventoryError(inventory.errors)
    return inventory


def build_transcript_index(projects_dir, session_id_is_valid=None):
    """Return a unique ``{session_id: absolute_jsonl_path}`` compatibility map.

    Historically this helper silently retained the last path seen for a
    duplicate UUID.  That behaviour could direct a mutator at an arbitrary
    transcript, so callers must now migrate to
    :func:`build_transcript_path_inventory` when duplicates exist.
    """
    inventory = build_transcript_path_inventory(projects_dir, session_id_is_valid)
    require_complete_transcript_inventory(inventory)
    if inventory.duplicate_session_ids:
        raise DuplicateTranscriptIdError(inventory.duplicate_session_ids)
    return {
        session_id: paths[0]
        for session_id, paths in inventory.by_session_id.items()
    }


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
