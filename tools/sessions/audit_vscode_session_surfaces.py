"""Audit VS Code session-list storage surfaces without modifying them.

The audit counts transcript-bearing project slugs, observes
``sessions-index.json`` by presence and size only, and queries ``state.vscdb``
read-only for the established ``agentSessions.model.cache`` key. It neither
parses the index/cache payloads nor offers a recovery recommendation.
"""

import argparse
import hashlib
import os
import pathlib
import platform
import sqlite3
import sys
import tempfile

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _TOOLS_DIR)
from platform_support import default_claude_paths  # noqa: E402
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
SURFACE_COMBINATIONS = ("index_only", "db_only", "both", "neither")
DATABASE_SNAPSHOT_SUFFIXES = ("", "-wal", "-shm")
COPY_CHUNK_BYTES = 64 * 1024


class DatabaseOpcodeLimit(RuntimeError):
    """Raised when SQLite exceeds the configured read-operation budget."""


def default_workspace_dir():
    if platform.system() == "Darwin":
        return os.path.expanduser(
            "~/Library/Application Support/Code/User/workspaceStorage"
        )
    if platform.system() == "Linux":
        return os.path.expanduser("~/.config/Code/User/workspaceStorage")
    return os.path.join(
        os.environ.get("APPDATA", os.path.expanduser("~")),
        "Code", "User", "workspaceStorage",
    )


def _scan_projects(projects_dir, state, max_directory_entries, max_slugs):
    transcript_slugs = 0
    index_count = 0
    index_bytes = 0
    slugs_scanned = 0
    if expected_path_kind(
        projects_dir,
        state,
        expected="directory",
        subject_namespace="projects-root",
    ) != "directory":
        return transcript_slugs, index_count, index_bytes, slugs_scanned
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
            elif child.name == "sessions-index.json":
                index_count += 1
                size = entry_size(
                    child, state, subject_namespace="sessions-index"
                )
                if size is not None:
                    index_bytes += size
        if has_transcript:
            transcript_slugs += 1
    return transcript_slugs, index_count, index_bytes, slugs_scanned


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


def _inspect_database_snapshot(database_path, max_database_bytes,
                               max_database_opcodes, state, subject,
                               temp_parent=None):
    """Copy a stable live source set and inspect SQLite only in temporary state."""
    pre_copy = _capture_source_set(database_path, state)
    if pre_copy is None:
        return False, False, 0, 0
    if _source_set_bytes(pre_copy) > max_database_bytes:
        state.cap("database_byte_cap_reached", subject)
        return False, False, 0, 0
    pre_fingerprints = _fingerprint_source_set(
        database_path,
        pre_copy,
        max_database_bytes,
        state,
        subject,
    )
    if pre_fingerprints is None:
        return False, False, 0, 0

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
                return False, False, 0, 0

            if not _source_set_is_stable(
                database_path,
                pre_copy,
                max_database_bytes,
                state,
                subject,
            ):
                return False, False, 0, copied_bytes

            snapshot_database = os.path.join(temp_dir, "state.vscdb")
            has_key = False
            used = 0
            try:
                has_key, used = _has_cache_key(
                    snapshot_database, max_database_opcodes
                )
            except DatabaseOpcodeLimit:
                state.cap("database_opcode_cap_reached", subject)
                used = max_database_opcodes
            except (OSError, sqlite3.Error):
                state.error("database_unreadable", subject)
            final_fingerprints = _fingerprint_source_set(
                database_path, pre_copy, max_database_bytes, state, subject
            )
            if final_fingerprints is None:
                has_key = False
            elif final_fingerprints != pre_fingerprints:
                state.error("database_source_changed", subject)
                has_key = False
            return True, has_key, used, copied_bytes
    except OSError:
        state.error("database_snapshot_error", subject)
        return False, False, 0, 0


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


def audit_surfaces(projects_dir, workspace_dir, *, max_directory_entries=20000,
                   max_slugs=10000, max_databases=10000,
                   max_database_bytes=512 * 1024 * 1024,
                   max_database_opcodes=100000, snapshot_temp_parent=None):
    state = ScanState()
    transcript_slugs, index_count, index_bytes, slugs_scanned = _scan_projects(
        projects_dir, state, max_directory_entries, max_slugs
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
    conclusive = not state.partial
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
        "surface_combination": combination,
        "limits": {
            "max_directory_entries": max_directory_entries,
            "max_slugs": max_slugs,
            "max_databases": max_databases,
            "max_database_bytes": max_database_bytes,
            "max_database_opcodes": max_database_opcodes,
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--state", help="Fixture state root")
    parser.add_argument("--projects-dir", default=default_claude_paths()[1])
    parser.add_argument("--workspace-dir", default=default_workspace_dir())
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--max-directory-entries", type=int, default=20000)
    parser.add_argument("--max-slugs", type=int, default=10000)
    parser.add_argument("--max-databases", type=int, default=10000)
    parser.add_argument("--max-database-bytes", type=int, default=512 * 1024 * 1024)
    parser.add_argument("--max-database-opcodes", type=int, default=100000)
    args = parser.parse_args(argv)
    if args.state:
        state_root = os.path.abspath(args.state)
        projects_dir = os.path.join(state_root, "projects")
        workspace_dir = os.path.join(
            state_root, "appdata", "Code", "User", "workspaceStorage"
        )
    else:
        projects_dir = args.projects_dir
        workspace_dir = args.workspace_dir
    result = audit_surfaces(
        projects_dir,
        workspace_dir,
        max_directory_entries=max(1, args.max_directory_entries),
        max_slugs=max(1, args.max_slugs),
        max_databases=max(1, args.max_databases),
        max_database_bytes=max(1, args.max_database_bytes),
        max_database_opcodes=max(1, args.max_database_opcodes),
    )
    if args.json:
        write_json(result)
    else:
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
        print("Surface combination: {}".format(result["surface_combination"]))
        print("Errors: {}".format(result["error_count"]))
    return 2 if result["partial"] else 0


if __name__ == "__main__":
    sys.exit(main())
