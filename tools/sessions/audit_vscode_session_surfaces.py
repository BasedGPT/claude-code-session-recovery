"""Audit VS Code session-list storage surfaces without modifying them.

The audit counts transcript-bearing project slugs, observes
``sessions-index.json`` by presence and size only, and queries ``state.vscdb``
read-only for the established ``agentSessions.model.cache`` key. It also
queries the separate global ``state.vscdb`` for the documented
``Anthropic.claude-code`` object and counts opaque overlaps between its
``hiddenSessionIds`` list and local transcript filenames. It neither retains
session IDs nor offers a recovery recommendation.
"""

import argparse
import hashlib
import json
import os
import pathlib
import sqlite3
import sys
import tempfile

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _TOOLS_DIR)
from platform_support import (  # noqa: E402
    default_claude_sessions_index_dir,
    default_vscode_workspace_storage_dir,
)
from sidecar_common import (  # noqa: E402
    ScanState,
    bounded_directory_entries,
    entry_kind,
    entry_size,
    expected_path_kind,
    file_stat_identity,
    opaque_id,
    write_json,
)

CLAUDE_CACHE_KEYS = ("agentSessions.model.cache",)
GLOBAL_STATE_KEY = "Anthropic.claude-code"
HIDDEN_SESSION_IDS_FIELD = "hiddenSessionIds"
SURFACE_COMBINATIONS = ("index_only", "db_only", "both", "neither")
DATABASE_SNAPSHOT_SUFFIXES = ("", "-wal", "-shm")
COPY_CHUNK_BYTES = 64 * 1024
DEFAULT_MAX_HIDDEN_SESSION_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_HIDDEN_SESSION_IDS = 100000


class DatabaseOpcodeLimit(RuntimeError):
    """Raised when SQLite exceeds the configured read-operation budget."""


class HiddenSessionIdsMalformed(RuntimeError):
    """Raised when the global Claude Code state has an invalid shape."""


class HiddenSessionIdsOversize(RuntimeError):
    """Raised when the hidden-session value exceeds a local bound."""

    def __init__(self, limit_kind):
        super().__init__()
        self.limit_kind = limit_kind


def _session_digest(session_id):
    """Return the same opaque identifier for a transcript or hidden ID."""
    return opaque_id("session", session_id)


def _scan_projects(projects_dir, state, max_directory_entries, max_slugs,
                   max_transcripts):
    transcript_slugs = 0
    index_count = 0
    index_bytes = 0
    slugs_scanned = 0
    transcript_digests = set()
    transcripts_scanned = 0
    transcript_cap_recorded = False
    projects_kind = expected_path_kind(
        projects_dir,
        state,
        expected="directory",
        subject_namespace="projects-root",
    )
    if projects_kind != "directory":
        if projects_kind == "absent":
            state.error("projects_root_absent", opaque_id("projects-root", projects_dir))
        return (
            transcript_slugs,
            index_count,
            index_bytes,
            slugs_scanned,
            transcript_digests,
            transcripts_scanned,
        )
    entries = bounded_directory_entries(
        projects_dir, state, cap=max_directory_entries,
        subject_namespace="projects-root",
    )
    for slug in entries:
        if entry_kind(slug, state, subject_namespace="project-slug") != "directory":
            continue
        if slugs_scanned >= max_slugs:
            state.cap("slug_cap_reached")
            break
        slugs_scanned += 1
        children = bounded_directory_entries(
            slug.path, state, cap=max_directory_entries,
            subject_namespace="slug",
        )
        has_transcript = False
        for child in children:
            if entry_kind(
                child, state, subject_namespace="project-entry"
            ) != "file":
                continue
            if child.name.endswith(".jsonl"):
                has_transcript = True
                if transcripts_scanned >= max_transcripts:
                    if not transcript_cap_recorded:
                        state.cap(
                            "transcript_cap_reached",
                            opaque_id("projects-root", projects_dir),
                        )
                        transcript_cap_recorded = True
                else:
                    transcript_digests.add(
                        _session_digest(child.name[:-len(".jsonl")])
                    )
                    transcripts_scanned += 1
            elif child.name == "sessions-index.json":
                index_count += 1
                size = entry_size(
                    child, state, subject_namespace="sessions-index"
                )
                if size is not None:
                    index_bytes += size
        if has_transcript:
            transcript_slugs += 1
    return (
        transcript_slugs,
        index_count,
        index_bytes,
        slugs_scanned,
        transcript_digests,
        transcripts_scanned,
    )


def _has_cache_key(database_path, max_opcodes):
    uri = pathlib.Path(os.path.abspath(database_path)).as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=1)
    interval = min(1000, max_opcodes)
    opcode_ticks = 0
    exceeded = False

    def progress():
        nonlocal opcode_ticks, exceeded
        opcode_ticks += interval
        if opcode_ticks >= max_opcodes:
            exceeded = True
            return 1
        return 0

    try:
        conn.execute("PRAGMA query_only=ON")
        conn.set_progress_handler(progress, interval)
        for key in CLAUDE_CACHE_KEYS:
            found = conn.execute(
                "SELECT 1 FROM ItemTable WHERE key = ? LIMIT 1", (key,)
            ).fetchone()
            if found:
                return True, opcode_ticks
        return False, opcode_ticks
    except sqlite3.OperationalError:
        if exceeded:
            raise DatabaseOpcodeLimit()
        raise
    finally:
        conn.set_progress_handler(None, 0)
        conn.close()


def _empty_hidden_session_result():
    return {
        "key_status": "inconclusive",
        "value_status": "inconclusive",
        "hidden_id_count": None,
        "unique_hidden_id_count": None,
        "transcript_overlap_count": None,
        "hidden_ids_without_transcript_match_count": None,
        "transcript_ids_compared": None,
        "listing_interpretation": None,
        "conclusive": False,
    }


def _read_hidden_session_ids(database_path, max_opcodes, max_hidden_bytes,
                             max_hidden_ids, transcript_digests):
    """Read only bounded structure from the global Claude Code state object."""
    uri = pathlib.Path(os.path.abspath(database_path)).as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=1)
    interval = min(1000, max_opcodes)
    opcode_ticks = 0
    exceeded = False

    def progress():
        nonlocal opcode_ticks, exceeded
        opcode_ticks += interval
        if opcode_ticks >= max_opcodes:
            exceeded = True
            return 1
        return 0

    try:
        conn.execute("PRAGMA query_only=ON")
        conn.set_progress_handler(progress, interval)
        size_row = conn.execute(
            "SELECT length(CAST(value AS BLOB)) FROM ItemTable WHERE key = ? LIMIT 1",
            (GLOBAL_STATE_KEY,),
        ).fetchone()
        compared = len(transcript_digests)
        if not size_row:
            return {
                "key_status": "absent",
                "value_status": "absent",
                "hidden_id_count": 0,
                "unique_hidden_id_count": 0,
                "transcript_overlap_count": 0,
                "hidden_ids_without_transcript_match_count": 0,
                "transcript_ids_compared": compared,
                "listing_interpretation": "global_key_absent",
                "conclusive": True,
            }, opcode_ticks

        value_length = size_row[0]
        if not isinstance(value_length, int) or value_length < 0:
            raise HiddenSessionIdsMalformed()
        if value_length > max_hidden_bytes:
            raise HiddenSessionIdsOversize("bytes")
        row = conn.execute(
            "SELECT substr(CAST(value AS BLOB), 1, ?) FROM ItemTable WHERE key = ? LIMIT 1",
            (max_hidden_bytes + 1, GLOBAL_STATE_KEY),
        ).fetchone()
        if not row:
            raise HiddenSessionIdsMalformed()
        value = row[0]
        if isinstance(value, bytes):
            raw = value
        elif isinstance(value, str):
            raw = value.encode("utf-8")
        else:
            raise HiddenSessionIdsMalformed()
        if len(raw) > max_hidden_bytes:
            raise HiddenSessionIdsOversize("bytes")
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise HiddenSessionIdsMalformed()
        if not isinstance(document, dict):
            raise HiddenSessionIdsMalformed()

        if HIDDEN_SESSION_IDS_FIELD not in document:
            hidden_ids = []
            value_status = "field_absent"
        else:
            hidden_ids = document[HIDDEN_SESSION_IDS_FIELD]
            if not isinstance(hidden_ids, list):
                raise HiddenSessionIdsMalformed()
            value_status = "valid"
        if len(hidden_ids) > max_hidden_ids:
            raise HiddenSessionIdsOversize("count")

        hidden_digests = set()
        for hidden_id in hidden_ids:
            if not isinstance(hidden_id, str) or not hidden_id:
                raise HiddenSessionIdsMalformed()
            hidden_digests.add(_session_digest(hidden_id))

        overlap = len(hidden_digests.intersection(transcript_digests))
        without_match = len(hidden_digests) - overlap
        if not hidden_digests:
            interpretation = "no_hidden_ids"
        elif overlap:
            interpretation = "hidden_transcripts_on_disk"
        else:
            interpretation = "hidden_ids_without_transcript_match"
        return {
            "key_status": "present",
            "value_status": value_status,
            "hidden_id_count": len(hidden_ids),
            "unique_hidden_id_count": len(hidden_digests),
            "transcript_overlap_count": overlap,
            "hidden_ids_without_transcript_match_count": without_match,
            "transcript_ids_compared": compared,
            "listing_interpretation": interpretation,
            "conclusive": True,
        }, opcode_ticks
    except sqlite3.OperationalError:
        if exceeded:
            raise DatabaseOpcodeLimit()
        raise
    finally:
        conn.set_progress_handler(None, 0)
        conn.close()


def _capture_source_set(database_path, state):
    """Capture the exact live DB/WAL/SHM set and stable-comparison identities."""
    captured = {}
    for suffix in DATABASE_SNAPSHOT_SUFFIXES:
        status, identity = file_stat_identity(
            database_path + suffix,
            state,
            subject_namespace="state-database-source",
            required=(suffix == ""),
        )
        if status == "error":
            return None
        if status == "file":
            captured[suffix] = identity
    return captured


def _source_set_bytes(source_set):
    return sum(identity[2] for identity in source_set.values())


def _source_set_is_stable(database_path, original, max_database_bytes,
                          state, subject):
    """Recompare the complete live source set and surface every drift mode."""
    current = _capture_source_set(database_path, state)
    if current is None:
        return False
    stable = True
    if _source_set_bytes(current) > max_database_bytes:
        state.cap("database_byte_cap_reached", subject)
        stable = False
    if current != original:
        state.error("database_source_changed", subject)
        stable = False
    return stable


def _fingerprint_source_set(database_path, original, max_database_bytes,
                            state, subject):
    """Hash each live source with stat-before/hash/stat-after stability gates."""
    fingerprints = {}
    total_bytes = 0
    for suffix, original_identity in original.items():
        source = database_path + suffix
        status, before = file_stat_identity(
            source,
            state,
            subject_namespace="state-database-source",
            required=True,
        )
        if status != "file":
            return None
        if before != original_identity:
            state.error("database_source_changed", subject)
            return None
        digest = hashlib.sha256()
        hashed_bytes = 0
        try:
            with open(source, "rb") as handle:
                while True:
                    remaining = max_database_bytes - total_bytes
                    read_size = min(COPY_CHUNK_BYTES, remaining + 1)
                    chunk = handle.read(read_size)
                    if not chunk:
                        break
                    if len(chunk) > remaining:
                        state.cap("database_byte_cap_reached", subject)
                        return None
                    digest.update(chunk)
                    hashed_bytes += len(chunk)
                    total_bytes += len(chunk)
        except OSError:
            state.error("database_source_hash_error", subject)
            return None
        status, after = file_stat_identity(
            source,
            state,
            subject_namespace="state-database-source",
            required=True,
        )
        if status != "file":
            return None
        if after != before or hashed_bytes != original_identity[2]:
            state.error("database_source_changed", subject)
            return None
        fingerprints[suffix] = digest.hexdigest()
    if not _source_set_is_stable(
        database_path, original, max_database_bytes, state, subject
    ):
        return None
    return fingerprints


def _copy_snapshot_file(source, destination, copied_bytes, max_database_bytes):
    """Chunk-copy one source file, returning cumulative bytes or ``None`` at cap."""
    file_bytes = 0
    digest = hashlib.sha256()
    with open(source, "rb") as source_handle, open(destination, "xb") as target:
        while True:
            remaining = max_database_bytes - copied_bytes
            read_size = min(COPY_CHUNK_BYTES, remaining + 1)
            chunk = source_handle.read(read_size)
            if not chunk:
                return copied_bytes, file_bytes, digest.hexdigest()
            if len(chunk) > remaining:
                return None, file_bytes, None
            target.write(chunk)
            digest.update(chunk)
            copied_bytes += len(chunk)
            file_bytes += len(chunk)


def _copy_source_set(database_path, source_set, source_fingerprints, temp_dir,
                     max_database_bytes, state, subject):
    copied_bytes = 0
    for suffix, identity in source_set.items():
        source = database_path + suffix
        destination = os.path.join(temp_dir, "state.vscdb" + suffix)
        try:
            copied_bytes_result, file_bytes, copied_fingerprint = _copy_snapshot_file(
                source, destination, copied_bytes, max_database_bytes
            )
        except OSError:
            state.error("database_snapshot_copy_error", subject)
            return None
        if copied_bytes_result is None:
            state.cap("database_byte_cap_reached", subject)
            return None
        copied_bytes = copied_bytes_result
        if file_bytes != identity[2]:
            state.error("database_source_changed", subject)
            return None
        if copied_fingerprint != source_fingerprints[suffix]:
            state.error("database_source_changed", subject)
            return None
    return copied_bytes


def _inspect_snapshot_query(database_path, max_database_bytes,
                            max_database_opcodes, state, subject, query,
                            empty_result_factory, temp_parent=None):
    """Copy a stable live source set and run a bounded query in the snapshot."""
    pre_copy = _capture_source_set(database_path, state)
    if pre_copy is None:
        return False, empty_result_factory(), 0, 0
    if _source_set_bytes(pre_copy) > max_database_bytes:
        state.cap("database_byte_cap_reached", subject)
        return False, empty_result_factory(), 0, 0
    pre_fingerprints = _fingerprint_source_set(
        database_path,
        pre_copy,
        max_database_bytes,
        state,
        subject,
    )
    if pre_fingerprints is None:
        return False, empty_result_factory(), 0, 0

    try:
        with tempfile.TemporaryDirectory(
            prefix="vscode-state-snapshot-", dir=temp_parent
        ) as temp_dir:
            copied_bytes = _copy_source_set(
                database_path,
                pre_copy,
                pre_fingerprints,
                temp_dir,
                max_database_bytes,
                state,
                subject,
            )
            if copied_bytes is None:
                return False, empty_result_factory(), 0, 0

            if not _source_set_is_stable(
                database_path,
                pre_copy,
                max_database_bytes,
                state,
                subject,
            ):
                return False, empty_result_factory(), 0, copied_bytes

            snapshot_database = os.path.join(temp_dir, "state.vscdb")
            result = empty_result_factory()
            used = 0
            try:
                result, used = query(
                    snapshot_database,
                    max_database_opcodes,
                )
            except DatabaseOpcodeLimit:
                state.cap("database_opcode_cap_reached", subject)
                used = max_database_opcodes
            except HiddenSessionIdsOversize as exc:
                code = (
                    "hidden_session_ids_count_cap_reached"
                    if exc.limit_kind == "count"
                    else "hidden_session_ids_byte_cap_reached"
                )
                state.cap(code, subject)
                if isinstance(result, dict):
                    result["key_status"] = "present"
                    result["value_status"] = "oversize"
            except HiddenSessionIdsMalformed:
                state.error("hidden_session_ids_malformed", subject)
                if isinstance(result, dict):
                    result["key_status"] = "present"
                    result["value_status"] = "malformed"
            except (OSError, sqlite3.Error):
                state.error("database_unreadable", subject)
            final_fingerprints = _fingerprint_source_set(
                database_path, pre_copy, max_database_bytes, state, subject
            )
            if final_fingerprints is None:
                result = empty_result_factory()
            elif final_fingerprints != pre_fingerprints:
                state.error("database_source_changed", subject)
                result = empty_result_factory()
            return True, result, used, copied_bytes
    except OSError:
        state.error("database_snapshot_error", subject)
        return False, empty_result_factory(), 0, 0


def _inspect_database_snapshot(database_path, max_database_bytes,
                               max_database_opcodes, state, subject,
                               temp_parent=None):
    """Copy a stable live source set and inspect SQLite only in temporary state."""
    inspected, has_key, used, copied = _inspect_snapshot_query(
        database_path,
        max_database_bytes,
        max_database_opcodes,
        state,
        subject,
        lambda snapshot, opcodes: _has_cache_key(snapshot, opcodes),
        lambda: False,
        temp_parent=temp_parent,
    )
    return inspected, bool(has_key), used, copied


def _scan_databases(workspace_dir, state, max_directory_entries, max_databases,
                    max_database_bytes, max_database_opcodes, temp_parent=None):
    database_count = 0
    inspected_database_count = 0
    cache_key_database_count = 0
    opcode_ticks = 0
    snapshot_bytes_copied = 0
    if expected_path_kind(
        workspace_dir,
        state,
        expected="directory",
        subject_namespace="workspace-root",
    ) != "directory":
        return (
            database_count,
            inspected_database_count,
            cache_key_database_count,
            opcode_ticks,
            snapshot_bytes_copied,
        )
    workspaces = bounded_directory_entries(
        workspace_dir, state, cap=max_directory_entries,
        subject_namespace="workspace-root",
    )
    for workspace in workspaces:
        if entry_kind(
            workspace, state, subject_namespace="workspace"
        ) != "directory":
            continue
        database = os.path.join(workspace.path, "state.vscdb")
        database_kind = expected_path_kind(
            database,
            state,
            expected="file",
            subject_namespace="state-database",
        )
        if database_kind != "file":
            continue
        if database_count >= max_databases:
            state.cap("database_cap_reached")
            break
        database_count += 1
        subject = opaque_id("workspace", workspace.name)
        inspected, has_key, used, copied = _inspect_database_snapshot(
            database,
            max_database_bytes,
            max_database_opcodes,
            state,
            subject,
            temp_parent=temp_parent,
        )
        snapshot_bytes_copied += copied
        opcode_ticks += used
        if inspected:
            inspected_database_count += 1
        if has_key:
            cache_key_database_count += 1
    return (
        database_count,
        inspected_database_count,
        cache_key_database_count,
        opcode_ticks,
        snapshot_bytes_copied,
    )


def default_vscode_global_state_db(workspace_dir=None):
    """Return the global state DB beside the configured workspace storage."""
    if workspace_dir is None:
        workspace_dir = default_vscode_workspace_storage_dir()
    user_dir = os.path.dirname(os.path.normpath(workspace_dir))
    return os.path.join(user_dir, "globalStorage", "state.vscdb")


def _absent_global_state_result(transcript_ids_compared):
    hidden = _empty_hidden_session_result()
    hidden.update({
        "key_status": "absent",
        "value_status": "absent",
        "hidden_id_count": 0,
        "unique_hidden_id_count": 0,
        "transcript_overlap_count": 0,
        "hidden_ids_without_transcript_match_count": 0,
        "transcript_ids_compared": transcript_ids_compared,
        "listing_interpretation": "global_database_absent",
        "conclusive": True,
    })
    return hidden


def _scan_global_state(global_state_db, state, max_database_bytes,
                       max_database_opcodes, max_hidden_bytes,
                       max_hidden_ids, transcript_digests,
                       temp_parent=None):
    """Inspect global ``hiddenSessionIds`` without opening its live SQLite DB."""
    compared = len(transcript_digests)
    result = {
        "database_status": "absent",
        "database_count": 0,
        "inspected_database_count": 0,
        "sqlite_opcode_ticks": 0,
        "snapshot_bytes_copied": 0,
        "keys_checked": [GLOBAL_STATE_KEY],
        "hidden_session_ids": _absent_global_state_result(compared),
        "value_parsed_structurally": False,
        "read_only": True,
        "live_sqlite_opened": False,
        "snapshot_only": True,
        "conclusive": True,
    }
    database_kind = expected_path_kind(
        global_state_db,
        state,
        expected="file",
        subject_namespace="global-state-database",
    )
    if database_kind == "absent":
        return result
    result["database_count"] = 1
    result["database_status"] = "present" if database_kind == "file" else "error"
    if database_kind != "file":
        result["hidden_session_ids"] = _empty_hidden_session_result()
        result["hidden_session_ids"]["transcript_ids_compared"] = compared
        result["conclusive"] = False
        return result

    inspected, hidden, used, copied = _inspect_snapshot_query(
        global_state_db,
        max_database_bytes,
        max_database_opcodes,
        state,
        opaque_id("global-state", global_state_db),
        lambda snapshot, opcodes: _read_hidden_session_ids(
            snapshot,
            opcodes,
            max_hidden_bytes,
            max_hidden_ids,
            transcript_digests,
        ),
        _empty_hidden_session_result,
        temp_parent=temp_parent,
    )
    result["inspected_database_count"] = 1 if inspected else 0
    result["sqlite_opcode_ticks"] = used
    result["snapshot_bytes_copied"] = copied
    if not inspected:
        hidden["transcript_ids_compared"] = compared
    result["hidden_session_ids"] = hidden
    result["value_parsed_structurally"] = (
        hidden.get("value_status") in ("valid", "field_absent")
    )
    result["conclusive"] = bool(hidden.get("conclusive"))
    return result


def _suppress_partial_hidden_result(global_result):
    """Suppress conclusions after any scan failure while retaining structure."""
    hidden = global_result["hidden_session_ids"]
    if hidden.get("conclusive"):
        hidden["key_status"] = "inconclusive"
        hidden["value_status"] = "inconclusive"
        hidden["listing_interpretation"] = None
    for field in (
        "hidden_id_count",
        "unique_hidden_id_count",
        "transcript_overlap_count",
        "hidden_ids_without_transcript_match_count",
    ):
        hidden[field] = None
    hidden["conclusive"] = False
    global_result["value_parsed_structurally"] = False
    global_result["conclusive"] = False


def audit_surfaces(projects_dir, workspace_dir, *, max_directory_entries=20000,
                   global_state_db=None, max_slugs=10000,
                   max_transcripts=100000, max_databases=10000,
                   max_database_bytes=512 * 1024 * 1024,
                   max_database_opcodes=100000,
                   max_hidden_session_bytes=DEFAULT_MAX_HIDDEN_SESSION_BYTES,
                   max_hidden_session_ids=DEFAULT_MAX_HIDDEN_SESSION_IDS,
                   snapshot_temp_parent=None):
    state = ScanState()
    (
        transcript_slugs,
        index_count,
        index_bytes,
        slugs_scanned,
        transcript_digests,
        transcripts_scanned,
    ) = _scan_projects(
        projects_dir,
        state,
        max_directory_entries,
        max_slugs,
        max_transcripts,
    )
    (
        database_count,
        inspected_database_count,
        key_database_count,
        opcode_ticks,
        snapshot_bytes_copied,
    ) = _scan_databases(
        workspace_dir,
        state,
        max_directory_entries,
        max_databases,
        max_database_bytes,
        max_database_opcodes,
        temp_parent=snapshot_temp_parent,
    )
    if global_state_db is None:
        global_state_db = default_vscode_global_state_db(workspace_dir)
    global_state = _scan_global_state(
        global_state_db,
        state,
        max_database_bytes,
        max_database_opcodes,
        max_hidden_session_bytes,
        max_hidden_session_ids,
        transcript_digests,
        temp_parent=snapshot_temp_parent,
    )
    conclusive = not state.partial
    if not conclusive:
        _suppress_partial_hidden_result(global_state)
    has_index = index_count > 0
    has_db = key_database_count > 0
    if not conclusive:
        combination = None
    elif has_index and has_db:
        combination = "both"
    elif has_index:
        combination = "index_only"
    elif has_db:
        combination = "db_only"
    else:
        combination = "neither"
    return {
        "audit": "vscode_session_surfaces",
        **state.fields(),
        "slugs_scanned": slugs_scanned,
        "transcript_ids_scanned": transcripts_scanned,
        "transcript_bearing_slug_count": transcript_slugs,
        "sessions_index": {
            "present_count": index_count,
            "total_size_bytes": index_bytes,
            "content_parsed": False,
            "conclusive": conclusive,
        },
        "state_vscdb": {
            "database_count": database_count,
            "inspected_database_count": inspected_database_count,
            "claude_cache_key_database_count": key_database_count,
            "sqlite_opcode_ticks": opcode_ticks,
            "snapshot_bytes_copied": snapshot_bytes_copied,
            "keys_checked": list(CLAUDE_CACHE_KEYS),
            "cache_values_parsed": False,
            "read_only": True,
            "live_sqlite_opened": False,
            "snapshot_only": True,
            "conclusive": conclusive,
        },
        "global_state_vscdb": global_state,
        "surface_combination": combination,
        "limits": {
            "max_directory_entries": max_directory_entries,
            "max_slugs": max_slugs,
            "max_transcripts": max_transcripts,
            "max_databases": max_databases,
            "max_database_bytes": max_database_bytes,
            "max_database_opcodes": max_database_opcodes,
            "max_hidden_session_bytes": max_hidden_session_bytes,
            "max_hidden_session_ids": max_hidden_session_ids,
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--state", help="Fixture state root")
    parser.add_argument("--projects-dir", default=default_claude_sessions_index_dir())
    parser.add_argument(
        "--workspace-dir", default=default_vscode_workspace_storage_dir()
    )
    parser.add_argument(
        "--global-state-db",
        default=None,
        help="Override the VS Code globalStorage state.vscdb path.",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--max-directory-entries", type=int, default=20000)
    parser.add_argument("--max-slugs", type=int, default=10000)
    parser.add_argument("--max-transcripts", type=int, default=100000)
    parser.add_argument("--max-databases", type=int, default=10000)
    parser.add_argument("--max-database-bytes", type=int, default=512 * 1024 * 1024)
    parser.add_argument("--max-database-opcodes", type=int, default=100000)
    parser.add_argument(
        "--max-hidden-session-bytes",
        type=int,
        default=DEFAULT_MAX_HIDDEN_SESSION_BYTES,
    )
    parser.add_argument(
        "--max-hidden-session-ids",
        type=int,
        default=DEFAULT_MAX_HIDDEN_SESSION_IDS,
    )
    args = parser.parse_args(argv)
    if args.state:
        state_root = os.path.abspath(args.state)
        projects_dir = os.path.join(state_root, "projects")
        workspace_dir = os.path.join(
            state_root, "appdata", "Code", "User", "workspaceStorage"
        )
        global_state_db = os.path.join(
            state_root, "appdata", "Code", "User", "globalStorage", "state.vscdb"
        )
    else:
        projects_dir = args.projects_dir
        workspace_dir = args.workspace_dir
        global_state_db = args.global_state_db or default_vscode_global_state_db(
            workspace_dir
        )
    result = audit_surfaces(
        projects_dir,
        workspace_dir,
        global_state_db=global_state_db,
        max_directory_entries=max(1, args.max_directory_entries),
        max_slugs=max(1, args.max_slugs),
        max_transcripts=max(1, args.max_transcripts),
        max_databases=max(1, args.max_databases),
        max_database_bytes=max(1, args.max_database_bytes),
        max_database_opcodes=max(1, args.max_database_opcodes),
        max_hidden_session_bytes=max(1, args.max_hidden_session_bytes),
        max_hidden_session_ids=max(1, args.max_hidden_session_ids),
    )
    if args.json:
        write_json(result)
    else:
        print("Claude sessions-index path: {}".format(projects_dir))
        print("VS Code workspaceStorage path: {}".format(workspace_dir))
        print("VS Code session surfaces audit: {}".format(result["status"]))
        print("Transcript-bearing slugs: {}".format(
            result["transcript_bearing_slug_count"]
        ))
        print("sessions-index.json files: {} ({} bytes)".format(
            result["sessions_index"]["present_count"],
            result["sessions_index"]["total_size_bytes"],
        ))
        print("state.vscdb files with Claude cache key: {}".format(
            result["state_vscdb"]["claude_cache_key_database_count"]
        ))
        hidden = result["global_state_vscdb"]["hidden_session_ids"]
        print("Global hiddenSessionIds overlap with transcripts: {}".format(
            hidden["transcript_overlap_count"]
        ))
        print("Global hiddenSessionIds interpretation: {}".format(
            hidden["listing_interpretation"]
        ))
        print("Surface combination: {}".format(result["surface_combination"]))
        print("Errors: {}".format(result["error_count"]))
    return 2 if result["partial"] else 0


if __name__ == "__main__":
    sys.exit(main())
