"""Audit structural evidence that may relate local Claude sessions.

This command is read-only. It reports only explicit ``forkedFrom`` links,
message-UUID overlap, first timestamps, and opaque title grouping. It never
labels a session duplicate, stale, canonical, removable, or safe to delete.

Paths and source identifiers are hidden by default. Use ``--include-paths``
only when local path disclosure is acceptable.
"""

import argparse
import datetime
import json
import os
import re
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
    iter_bounded_jsonl,
    load_bounded_json_object,
    opaque_id,
    write_json,
)

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
CLASSIFICATIONS = (
    "explicit_lineage",
    "shared_history_candidate",
    "title_only_ambiguous",
    "insufficient_evidence",
)


def _timestamp_ms(value):
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    if isinstance(value, str):
        try:
            parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return int(parsed.timestamp() * 1000)
        except (ValueError, OverflowError):
            return None
    return None


def _reference(value):
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        for key in ("sessionId", "session_id", "uuid", "id"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
    return None


def _title_group(value):
    if not isinstance(value, str) or not value.strip():
        return None
    return opaque_id("title", value.strip())


def _upsert_session(conn, key, *, first_timestamp=None, title_group=None):
    conn.execute(
        "INSERT OR IGNORE INTO sessions(session_key, opaque_session) VALUES (?, ?)",
        (key, opaque_id("session", key)),
    )
    if first_timestamp is not None:
        conn.execute(
            "UPDATE sessions SET first_timestamp = CASE "
            "WHEN first_timestamp IS NULL OR first_timestamp > ? THEN ? "
            "ELSE first_timestamp END WHERE session_key = ?",
            (first_timestamp, first_timestamp, key),
        )
    if title_group:
        conn.execute(
            "UPDATE sessions SET title_group = COALESCE(title_group, ?) "
            "WHERE session_key = ?",
            (title_group, key),
        )


def _metadata_directories(root, state, caps):
    sessions_root = os.path.join(root, "claude-code-sessions")
    if expected_path_kind(
        sessions_root,
        state,
        expected="directory",
        subject_namespace="metadata-root",
    ) != "directory":
        return
    accounts = bounded_directory_entries(
        sessions_root, state, cap=caps["max_directory_entries"],
        subject_namespace="metadata-root",
    )
    for account in accounts:
        if entry_kind(
            account, state, subject_namespace="metadata-account"
        ) != "directory":
            continue
        organisations = bounded_directory_entries(
            account.path, state, cap=caps["max_directory_entries"],
            subject_namespace="account",
        )
        for organisation in organisations:
            if entry_kind(
                organisation, state, subject_namespace="metadata-owner"
            ) == "directory":
                yield organisation.path


def _scan_metadata(conn, appdata_dir, state, caps, counters):
    for directory in _metadata_directories(appdata_dir, state, caps) or ():
        entries = bounded_directory_entries(
            directory, state, cap=caps["max_directory_entries"],
            subject_namespace="metadata-directory",
        )
        for entry in entries:
            if not (entry.name.startswith("local_") and entry.name.endswith(".json")):
                continue
            if entry_kind(
                entry, state, subject_namespace="metadata-file"
            ) != "file":
                continue
            if counters["files_scanned"] >= caps["max_files"]:
                state.cap("file_cap_reached")
                return
            counters["files_scanned"] += 1
            subject = opaque_id("metadata", entry.name)
            file_size = entry_size(
                entry, state, subject_namespace="metadata-file"
            )
            if file_size is None:
                continue
            if file_size > caps["max_metadata_bytes"]:
                state.cap("file_byte_cap_reached", subject)
                continue
            if counters["bytes_scanned"] + file_size > caps["max_total_bytes"]:
                state.cap("total_byte_cap_reached")
                return
            counters["bytes_scanned"] += file_size
            record = load_bounded_json_object(
                entry.path, state, subject=subject,
                max_bytes=caps["max_metadata_bytes"],
            )
            if record is None:
                continue
            recorded_session_id = _reference(record.get("sessionId"))
            metadata_session_id = recorded_session_id or entry.name
            metadata_record = os.path.relpath(
                entry.path,
                os.path.join(appdata_dir, "claude-code-sessions"),
            )
            key = "metadata\0{}\0{}".format(
                metadata_record, metadata_session_id
            )
            _upsert_session(
                conn,
                key,
                first_timestamp=_timestamp_ms(
                    record.get("createdAt") or record.get("firstTimestamp")
                ),
                title_group=_title_group(record.get("title")),
            )
            conn.execute(
                "INSERT OR IGNORE INTO sources(session_key, kind, path) VALUES (?, ?, ?)",
                (key, "metadata", entry.path),
            )
            if recorded_session_id:
                conn.execute(
                    "INSERT OR IGNORE INTO session_references(reference, session_key) "
                    "VALUES (?, ?)",
                    (recorded_session_id, key),
                )
            cli_session_id = _reference(record.get("cliSessionId"))
            if cli_session_id:
                conn.execute(
                    "INSERT OR IGNORE INTO cli_memberships(cli_session_id, session_key) "
                    "VALUES (?, ?)",
                    (cli_session_id, key),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO session_references(reference, session_key) "
                    "VALUES (?, ?)",
                    (cli_session_id, key),
                )
            target = _reference(record.get("forkedFrom"))
            if target:
                conn.execute(
                    "INSERT OR IGNORE INTO explicit_links(source_key, target_key) "
                    "VALUES (?, ?)",
                    (key, target),
                )


def _scan_transcripts(conn, projects_dir, state, caps, counters):
    if expected_path_kind(
        projects_dir,
        state,
        expected="directory",
        subject_namespace="projects-root",
    ) != "directory":
        return
    slugs = bounded_directory_entries(
        projects_dir, state, cap=caps["max_directory_entries"],
        subject_namespace="projects-root",
    )
    for slug in slugs:
        if entry_kind(slug, state, subject_namespace="project-slug") != "directory":
            continue
        entries = bounded_directory_entries(
            slug.path, state, cap=caps["max_directory_entries"],
            subject_namespace="slug",
        )
        for entry in entries:
            if not entry.name.endswith(".jsonl"):
                continue
            if entry_kind(
                entry, state, subject_namespace="transcript-file"
            ) != "file":
                continue
            if counters["files_scanned"] >= caps["max_files"]:
                state.cap("file_cap_reached")
                return
            counters["files_scanned"] += 1
            cli_session_id = os.path.splitext(entry.name)[0]
            subject = opaque_id("transcript", cli_session_id)
            file_size = entry_size(
                entry, state, subject_namespace="transcript-file"
            )
            if file_size is None:
                continue
            if file_size > caps["max_file_bytes"]:
                state.cap("file_byte_cap_reached", subject)
                continue
            if counters["bytes_scanned"] + file_size > caps["max_total_bytes"]:
                state.cap("total_byte_cap_reached")
                return
            counters["bytes_scanned"] += file_size
            counters["transcripts_scanned"] += 1
            session_keys = [
                row[0] for row in conn.execute(
                    "SELECT session_key FROM cli_memberships "
                    "WHERE cli_session_id = ? ORDER BY session_key",
                    (cli_session_id,),
                )
            ]
            if not session_keys:
                transcript_record = os.path.relpath(entry.path, projects_dir)
                session_keys = ["transcript\0{}".format(transcript_record)]
            for key in session_keys:
                _upsert_session(conn, key)
                conn.execute(
                    "INSERT OR IGNORE INTO session_references(reference, session_key) "
                    "VALUES (?, ?)",
                    (cli_session_id, key),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO sources(session_key, kind, path) "
                    "VALUES (?, ?, ?)",
                    (key, "transcript", entry.path),
                )
            nodes = 0
            for record in iter_bounded_jsonl(
                entry.path,
                state,
                subject=subject,
                max_lines=caps["max_lines_per_file"],
                max_line_bytes=caps["max_line_bytes"],
                max_file_bytes=caps["max_file_bytes"],
            ):
                counters["records_scanned"] += 1
                timestamp = _timestamp_ms(record.get("timestamp"))
                title = record.get("customTitle") or record.get("aiTitle")
                target = _reference(record.get("forkedFrom"))
                for key in session_keys:
                    _upsert_session(
                        conn, key, first_timestamp=timestamp,
                        title_group=_title_group(title),
                    )
                    if target:
                        conn.execute(
                            "INSERT OR IGNORE INTO explicit_links"
                            "(source_key, target_key) VALUES (?, ?)",
                            (key, target),
                        )
                node_uuid = record.get("uuid")
                if isinstance(node_uuid, str) and UUID_RE.match(node_uuid):
                    node_uuid = node_uuid.lower()
                    for key in session_keys:
                        exists = conn.execute(
                            "SELECT 1 FROM nodes WHERE node_uuid = ? "
                            "AND session_key = ?",
                            (node_uuid, key),
                        ).fetchone()
                        if exists:
                            continue
                        if nodes >= caps["max_nodes_per_file"]:
                            if nodes == caps["max_nodes_per_file"]:
                                state.cap("node_cap_reached", subject)
                                nodes += 1
                            continue
                        conn.execute(
                            "INSERT OR IGNORE INTO nodes(node_uuid, session_key) "
                            "VALUES (?, ?)",
                            (node_uuid, key),
                        )
                        nodes += 1


def _materialize_overlap_relationships(conn, max_relationships, state, counters):
    """Stream UUID memberships into a strictly budgeted relationship table.

    The primary-key index on ``nodes(node_uuid, session_key)`` supplies rows in
    the required order. Candidate pairs are stopped before operation
    ``max_relationships + 1``; no self-join or unbounded cross-product query is
    issued.
    """
    current_uuid = None
    prior_sessions = []
    operations = 0
    cursor = conn.execute(
        "SELECT node_uuid, session_key FROM nodes ORDER BY node_uuid, session_key"
    )
    for node_uuid, session_key in cursor:
        if node_uuid != current_uuid:
            current_uuid = node_uuid
            prior_sessions = []
        for prior_session in prior_sessions:
            if operations >= max_relationships:
                state.cap("relationship_cap_reached")
                counters["relationship_pair_operations"] = operations
                counters["shared_relationship_count"] = conn.execute(
                    "SELECT COUNT(*) FROM relationships"
                ).fetchone()[0]
                return False
            operations += 1
            conn.execute(
                "INSERT INTO relationships(left_key, right_key, overlap_count) "
                "VALUES (?, ?, 1) ON CONFLICT(left_key, right_key) DO UPDATE SET "
                "overlap_count = overlap_count + 1",
                (prior_session, session_key),
            )
        prior_sessions.append(session_key)
    counters["relationship_pair_operations"] = operations
    counters["shared_relationship_count"] = conn.execute(
        "SELECT COUNT(*) FROM relationships"
    ).fetchone()[0]
    return True


def _build_findings(conn, include_paths, max_findings, max_relationships, state):
    shared = {}
    shared_rows = conn.execute(
        "SELECT left_key, right_key, overlap_count FROM relationships "
        "ORDER BY left_key, right_key"
    ).fetchall()
    for left, right, count in shared_rows:
        shared.setdefault(left, []).append((right, count))
        shared.setdefault(right, []).append((left, count))

    title_counts = dict(conn.execute(
        "SELECT title_group, COUNT(*) FROM sessions WHERE title_group IS NOT NULL "
        "GROUP BY title_group"
    ))
    explicit_targets = {}
    explicit_sources = {}
    explicit_operations = 0
    explicit_rows = conn.execute(
        "SELECT source_key, target_key FROM explicit_links "
        "ORDER BY source_key, target_key"
    )
    for source, target_reference in explicit_rows:
        remaining = max_relationships - explicit_operations
        target_sessions = [
            row[0] for row in conn.execute(
                "SELECT session_key FROM session_references "
                "WHERE reference = ? ORDER BY session_key LIMIT ?",
                (target_reference, remaining + 1),
            )
        ]
        required_operations = len(target_sessions) or 1
        if required_operations > remaining:
            state.cap("relationship_cap_reached")
            return [], False
        explicit_operations += required_operations
        if target_sessions:
            explicit_targets.setdefault(source, []).extend(target_sessions)
            for target_session in target_sessions:
                explicit_sources.setdefault(target_session, []).append(source)
        else:
            explicit_targets.setdefault(source, []).append(target_reference)

    findings = []
    rows = conn.execute(
        "SELECT session_key, opaque_session, first_timestamp, title_group "
        "FROM sessions ORDER BY opaque_session"
    )
    for key, opaque_session, first_timestamp, title_group in rows:
        if key in explicit_targets or key in explicit_sources:
            classification = "explicit_lineage"
        elif key in shared:
            classification = "shared_history_candidate"
        elif title_group and title_counts.get(title_group, 0) > 1:
            classification = "title_only_ambiguous"
        else:
            classification = "insufficient_evidence"
        finding = {
            "session": opaque_session,
            "classification": classification,
            "evidence": {
                "explicit_fork_targets": [
                    opaque_id("session", value)
                    for value in explicit_targets.get(key, [])
                ],
                "explicit_fork_sources": [
                    opaque_id("session", value)
                    for value in explicit_sources.get(key, [])
                ],
                "shared_uuid_partners": [
                    {
                        "session": opaque_id("session", partner),
                        "overlap_count": count,
                    }
                    for partner, count in shared.get(key, [])
                ],
                "first_timestamp_ms": first_timestamp,
                "title_group": title_group,
            },
        }
        if include_paths:
            finding["paths"] = [
                path for (path,) in conn.execute(
                    "SELECT path FROM sources WHERE session_key = ? ORDER BY path",
                    (key,),
                )
            ]
        findings.append(finding)
        if len(findings) >= max_findings:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM sessions"
            ).fetchone()[0]
            if remaining > max_findings:
                state.cap("finding_cap_reached")
            break
    return findings, True


def audit_lineage(appdata_dir, projects_dir, *, include_paths=False,
                  temp_parent=None, **overrides):
    caps = {
        "max_files": 10000,
        "max_directory_entries": 20000,
        "max_lines_per_file": 200000,
        "max_nodes_per_file": 1000000,
        "max_line_bytes": 1024 * 1024,
        "max_file_bytes": 1024 * 1024 * 1024,
        "max_total_bytes": 10 * 1024 * 1024 * 1024,
        "max_metadata_bytes": 4 * 1024 * 1024,
        "max_findings": 10000,
        "max_relationships": 100000,
    }
    caps.update(overrides)
    state = ScanState()
    counters = {
        "files_scanned": 0,
        "transcripts_scanned": 0,
        "records_scanned": 0,
        "bytes_scanned": 0,
        "relationship_pair_operations": 0,
        "shared_relationship_count": 0,
    }
    findings = []
    discovered_session_count = None
    relationships_complete = False
    relationship_stage_started = False
    try:
        with tempfile.TemporaryDirectory(
            prefix="session-lineage-", dir=temp_parent
        ) as temp_dir:
            database = os.path.join(temp_dir, "membership.sqlite")
            conn = None
            try:
                conn = sqlite3.connect(database)
                conn.executescript(
                    "PRAGMA temp_store=FILE; PRAGMA cache_size=-32768;"
                    "CREATE TABLE sessions (session_key TEXT PRIMARY KEY, "
                    "opaque_session TEXT NOT NULL, first_timestamp INTEGER, title_group TEXT);"
                    "CREATE TABLE sources (session_key TEXT NOT NULL, kind TEXT NOT NULL, "
                    "path TEXT NOT NULL, PRIMARY KEY(session_key, kind, path));"
                    "CREATE TABLE explicit_links (source_key TEXT NOT NULL, target_key TEXT NOT NULL, "
                    "PRIMARY KEY(source_key, target_key));"
                    "CREATE TABLE cli_memberships (cli_session_id TEXT NOT NULL, "
                    "session_key TEXT NOT NULL, PRIMARY KEY(cli_session_id, session_key));"
                    "CREATE TABLE session_references (reference TEXT NOT NULL, "
                    "session_key TEXT NOT NULL, PRIMARY KEY(reference, session_key));"
                    "CREATE TABLE nodes (node_uuid TEXT NOT NULL, session_key TEXT NOT NULL, "
                    "PRIMARY KEY(node_uuid, session_key));"
                    "CREATE TABLE relationships (left_key TEXT NOT NULL, right_key TEXT NOT NULL, "
                    "overlap_count INTEGER NOT NULL, PRIMARY KEY(left_key, right_key));"
                    "CREATE INDEX nodes_by_session ON nodes(session_key);"
                )
                _scan_metadata(conn, appdata_dir, state, caps, counters)
                _scan_transcripts(conn, projects_dir, state, caps, counters)
                if not state.partial:
                    discovered_session_count = conn.execute(
                        "SELECT COUNT(*) FROM sessions"
                    ).fetchone()[0]
                    relationship_stage_started = True
                    try:
                        relationships_complete = _materialize_overlap_relationships(
                            conn, caps["max_relationships"], state, counters
                        )
                        conn.commit()
                        if relationships_complete:
                            findings, relationships_complete = _build_findings(
                                conn,
                                include_paths,
                                caps["max_findings"],
                                caps["max_relationships"],
                                state,
                            )
                        relationships_complete = (
                            relationships_complete and not state.partial
                        )
                    except sqlite3.Error:
                        raise
                    except Exception:
                        state.error("relationship_stage_error")
                        relationships_complete = False
            finally:
                if conn is not None:
                    conn.close()
    except (OSError, sqlite3.Error):
        state.error("temporary_index_error")
        relationships_complete = False
    except Exception:
        state.error(
            "relationship_stage_error"
            if relationship_stage_started
            else "temporary_index_error"
        )
        relationships_complete = False

    if relationships_complete:
        try:
            counts = {classification: 0 for classification in CLASSIFICATIONS}
            for finding in findings:
                counts[finding["classification"]] += 1
        except Exception:
            state.error("relationship_stage_error")
            relationships_complete = False
    if not relationships_complete:
        findings = []
        counts = None
        counters["relationship_pair_operations"] = None
        counters["shared_relationship_count"] = None
    result = {
        "audit": "session_lineage",
        **state.fields(),
        **counters,
        "session_count": (
            discovered_session_count if not state.partial else None
        ),
        "reported_session_count": (
            len(findings) if relationships_complete else None
        ),
        "classification_counts": counts,
        "findings": findings,
        "relationship_results_suppressed": not relationships_complete,
        "limits": caps,
    }
    return result


def main(argv=None):
    defaults = default_claude_paths()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--state", help="Fixture state root")
    parser.add_argument("--appdata-claude-dir", default=defaults[0])
    parser.add_argument("--projects-dir", default=defaults[1])
    parser.add_argument("--include-paths", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--max-files", type=int, default=10000)
    parser.add_argument("--max-lines-per-file", type=int, default=200000)
    parser.add_argument("--max-nodes-per-file", type=int, default=1000000)
    parser.add_argument("--max-line-bytes", type=int, default=1024 * 1024)
    parser.add_argument("--max-file-bytes", type=int, default=1024 * 1024 * 1024)
    parser.add_argument("--max-total-bytes", type=int, default=10 * 1024 * 1024 * 1024)
    parser.add_argument("--max-findings", type=int, default=10000)
    parser.add_argument("--max-relationships", type=int, default=100000)
    args = parser.parse_args(argv)
    if args.state:
        root = os.path.abspath(args.state)
        appdata_dir = os.path.join(root, "appdata", "Claude")
        projects_dir = os.path.join(root, "projects")
    else:
        appdata_dir = args.appdata_claude_dir
        projects_dir = args.projects_dir
    result = audit_lineage(
        appdata_dir,
        projects_dir,
        include_paths=args.include_paths,
        max_files=max(1, args.max_files),
        max_lines_per_file=max(1, args.max_lines_per_file),
        max_nodes_per_file=max(1, args.max_nodes_per_file),
        max_line_bytes=max(1, args.max_line_bytes),
        max_file_bytes=max(1, args.max_file_bytes),
        max_total_bytes=max(1, args.max_total_bytes),
        max_findings=max(1, args.max_findings),
        max_relationships=max(1, args.max_relationships),
    )
    if args.json:
        write_json(result)
    else:
        print("Session lineage structural audit: {}".format(result["status"]))
        print("Sessions: {}".format(result["session_count"]))
        if result["relationship_results_suppressed"]:
            print("Relationship findings: suppressed (bounded generation incomplete)")
        else:
            for classification in CLASSIFICATIONS:
                print("  {}: {}".format(
                    classification,
                    result["classification_counts"][classification],
                ))
        print("Errors: {}".format(result["error_count"]))
        print("No duplicate, stale, canonical, removal, or deletion claim is made.")
    return 2 if result["partial"] else 0


if __name__ == "__main__":
    sys.exit(main())
