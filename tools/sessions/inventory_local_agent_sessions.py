"""Inventory local-agent session containers without reading their contents.

The report is aggregate-only: counts grouped into root, owner, ``local_*``, and
``outputs`` buckets, plus the nested transcript roots used by local-agent
sandboxes. Identifiers appear only as opaque subjects on bounded errors.
LevelDB stores and transcript contents are never parsed, and no product/mode
classification (including Cowork) is attempted.
"""

import argparse
import os
import sys

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _TOOLS_DIR)
from platform_support import (  # noqa: E402
    default_claude_appdata_dir,
    default_claude_sessions_index_dir,
)
from session_metadata import build_metadata_path_inventory  # noqa: E402
from sidecar_common import (  # noqa: E402
    ScanState,
    bounded_directory_entries,
    entry_kind,
    expected_path_kind,
    opaque_id,
    write_json,
)
from transcript_files import build_transcript_path_inventory  # noqa: E402


def _empty_transcript_counts():
    return {
        "projects_root_count": 0,
        "project_directory_count": 0,
        "transcript_file_count": 0,
    }


def _is_reparse_point(path):
    """Return whether a path is a symlink or Windows junction."""
    try:
        if os.path.islink(path):
            return True
        isjunction = getattr(os.path, "isjunction", None)
        return bool(isjunction and isjunction(path))
    except OSError:
        return True


def _normalized_realpath(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _safe_scan_path(path, root_realpath, state, *, subject_namespace):
    """Reject reparse points and paths resolved outside the declared root."""
    subject = opaque_id(subject_namespace, path)
    try:
        if _is_reparse_point(path):
            state.error("reparse_point_skipped", subject)
            return False
        candidate_realpath = _normalized_realpath(path)
        if os.path.commonpath((candidate_realpath, root_realpath)) != root_realpath:
            state.error("path_outside_scan_root", subject)
            return False
    except (OSError, ValueError):
        state.error("path_boundary_check_error", subject)
        return False
    return True


def _boundary_safe_directory_tree(
    root,
    *,
    depth,
    max_directory_entries,
    subject_namespace,
):
    """Check a fixed-depth directory tree without following reparse points."""
    boundary_state = ScanState()
    root = os.path.abspath(root)
    if _is_reparse_point(root):
        boundary_state.error(
            "reparse_point_skipped", opaque_id(subject_namespace, root)
        )
        return False, boundary_state
    root_kind = expected_path_kind(
        root,
        boundary_state,
        expected="directory",
        subject_namespace=subject_namespace,
    )
    if root_kind == "absent":
        return True, boundary_state
    if root_kind != "directory":
        return False, boundary_state
    try:
        root_realpath = _normalized_realpath(root)
    except (OSError, ValueError):
        boundary_state.error(
            "path_boundary_check_error", opaque_id(subject_namespace, root)
        )
        return False, boundary_state

    directories = [root]
    for _level in range(depth):
        next_directories = []
        for directory in directories:
            entries = bounded_directory_entries(
                directory,
                boundary_state,
                cap=max_directory_entries,
                subject_namespace=subject_namespace,
            )
            for entry in entries:
                if not _safe_scan_path(
                    entry.path,
                    root_realpath,
                    boundary_state,
                    subject_namespace=subject_namespace,
                ):
                    continue
                if entry_kind(
                    entry,
                    boundary_state,
                    subject_namespace=subject_namespace,
                ) == "directory":
                    next_directories.append(entry.path)
        directories = next_directories
        if boundary_state.partial:
            return False, boundary_state
    return True, boundary_state


def _merge_boundary_failure(state, boundary_state, *, code):
    if boundary_state.partial:
        state.error(code)


def _inventory_local_transcript_root(
    session_path,
    state,
    *,
    scan_root_realpath,
    max_directory_entries,
    max_transcript_entries,
    transcript_file_count,
):
    """Count one local-agent ``.claude/projects`` tree without opening files."""
    counts = _empty_transcript_counts()
    claude_path = os.path.join(session_path, ".claude")
    if not _safe_scan_path(
        claude_path,
        scan_root_realpath,
        state,
        subject_namespace="agent-claude",
    ):
        return counts, False
    claude_kind = expected_path_kind(
        claude_path,
        state,
        expected="directory",
        subject_namespace="agent-claude",
        follow_symlinks=False,
    )
    if claude_kind != "directory":
        return counts, False

    projects_path = os.path.join(claude_path, "projects")
    if not _safe_scan_path(
        projects_path,
        scan_root_realpath,
        state,
        subject_namespace="agent-transcript-root",
    ):
        return counts, False
    projects_kind = expected_path_kind(
        projects_path,
        state,
        expected="directory",
        subject_namespace="agent-transcript-root",
        follow_symlinks=False,
    )
    if projects_kind != "directory":
        return counts, False
    counts["projects_root_count"] = 1

    slugs = bounded_directory_entries(
        projects_path,
        state,
        cap=max_directory_entries,
        subject_namespace="agent-transcript-root",
    )
    for slug in slugs:
        if not _safe_scan_path(
            slug.path,
            scan_root_realpath,
            state,
            subject_namespace="agent-transcript-slug",
        ):
            continue
        if entry_kind(
            slug, state, subject_namespace="agent-transcript-slug"
        ) != "directory":
            continue
        counts["project_directory_count"] += 1
        entries = bounded_directory_entries(
            slug.path,
            state,
            cap=max_directory_entries,
            subject_namespace="agent-transcript-slug",
        )
        for entry in entries:
            if not entry.name.endswith(".jsonl"):
                continue
            if not _safe_scan_path(
                entry.path,
                scan_root_realpath,
                state,
                subject_namespace="agent-transcript-file",
            ):
                continue
            if entry_kind(
                entry, state, subject_namespace="agent-transcript-file"
            ) != "file":
                continue
            if (
                transcript_file_count + counts["transcript_file_count"]
                >= max_transcript_entries
            ):
                state.cap("transcript_entry_cap_reached")
                return counts, True
            counts["transcript_file_count"] += 1
    return counts, False


def _standard_root_summary(
    state,
    *,
    max_directory_entries=20000,
    standard_appdata_claude_dir=None,
    standard_projects_dir=None,
):
    """Summarise standard roots without returning their paths or records."""
    if standard_appdata_claude_dir is None and standard_projects_dir is None:
        return None

    summary = {}
    if standard_appdata_claude_dir is not None:
        metadata_root = os.path.join(
            os.path.abspath(standard_appdata_claude_dir),
            "claude-code-sessions",
        )
        metadata_safe, metadata_boundary = _boundary_safe_directory_tree(
            metadata_root,
            depth=3,
            max_directory_entries=max_directory_entries,
            subject_namespace="standard-metadata-root",
        )
        if not metadata_safe:
            _merge_boundary_failure(
                state,
                metadata_boundary,
                code="standard_metadata_boundary_incomplete",
            )
            summary["claude-code-sessions"] = {
                "status": "partial",
                "physical_file_count": 0,
                "parsed_record_count": 0,
                "error_count": 1,
            }
        else:
            metadata = build_metadata_path_inventory(standard_appdata_claude_dir)
            for error in metadata.errors:
                state.error("standard_metadata_{}".format(error.code))
            summary["claude-code-sessions"] = {
                "status": metadata.status,
                "physical_file_count": metadata.physical_file_count,
                "parsed_record_count": len(metadata.records),
                "error_count": len(metadata.errors),
            }

    if standard_projects_dir is not None:
        projects_safe, projects_boundary = _boundary_safe_directory_tree(
            standard_projects_dir,
            depth=2,
            max_directory_entries=max_directory_entries,
            subject_namespace="standard-projects-root",
        )
        if not projects_safe:
            _merge_boundary_failure(
                state,
                projects_boundary,
                code="standard_projects_boundary_incomplete",
            )
            summary["projects"] = {
                "status": "partial",
                "physical_file_count": 0,
                "session_id_count": 0,
                "duplicate_session_id_count": 0,
                "error_count": 1,
            }
        else:
            transcripts = build_transcript_path_inventory(standard_projects_dir)
            for error in transcripts.errors:
                state.error("standard_projects_{}".format(error.code))
            summary["projects"] = {
                "status": transcripts.status,
                "physical_file_count": transcripts.physical_count,
                "session_id_count": transcripts.unique_session_id_count,
                "duplicate_session_id_count": len(transcripts.duplicate_session_ids),
                "error_count": len(transcripts.errors),
            }
    return summary


def inventory(
    root,
    *,
    max_directory_entries=20000,
    max_owners=10000,
    max_sessions=100000,
    max_output_entries=1000000,
    max_transcript_entries=1000000,
    standard_appdata_claude_dir=None,
    standard_projects_dir=None,
):
    state = ScanState()
    owner_total = 0
    session_total = 0
    outputs_directory_total = 0
    output_entry_total = 0
    transcript_counts = _empty_transcript_counts()

    root_kind = expected_path_kind(
        root, state, expected="directory", subject_namespace="agent-root"
    )
    root_status = "present" if root_kind == "directory" else root_kind
    if root_kind != "directory":
        return _result(
            state,
            root_status,
            owner_total,
            session_total,
            outputs_directory_total,
            output_entry_total,
            transcript_counts,
            max_directory_entries,
            max_owners,
            max_sessions,
            max_output_entries,
            max_transcript_entries,
            _standard_root_summary(
                state,
                max_directory_entries=max_directory_entries,
                standard_appdata_claude_dir=standard_appdata_claude_dir,
                standard_projects_dir=standard_projects_dir,
            ),
        )
    root = os.path.abspath(root)
    if _is_reparse_point(root):
        state.error("reparse_point_skipped", opaque_id("agent-root", root))
        return _result(
            state,
            root_status,
            owner_total,
            session_total,
            outputs_directory_total,
            output_entry_total,
            transcript_counts,
            max_directory_entries,
            max_owners,
            max_sessions,
            max_output_entries,
            max_transcript_entries,
            _standard_root_summary(
                state,
                max_directory_entries=max_directory_entries,
                standard_appdata_claude_dir=standard_appdata_claude_dir,
                standard_projects_dir=standard_projects_dir,
            ),
        )
    try:
        root_realpath = _normalized_realpath(root)
    except (OSError, ValueError):
        state.error("path_boundary_check_error", opaque_id("agent-root", root))
        return _result(
            state,
            root_status,
            owner_total,
            session_total,
            outputs_directory_total,
            output_entry_total,
            transcript_counts,
            max_directory_entries,
            max_owners,
            max_sessions,
            max_output_entries,
            max_transcript_entries,
            _standard_root_summary(
                state,
                max_directory_entries=max_directory_entries,
                standard_appdata_claude_dir=standard_appdata_claude_dir,
                standard_projects_dir=standard_projects_dir,
            ),
        )

    accounts = bounded_directory_entries(
        root, state, cap=max_directory_entries, subject_namespace="agent-root"
    )
    stop = False
    for account in accounts:
        if not _safe_scan_path(
            account.path,
            root_realpath,
            state,
            subject_namespace="agent-account",
        ):
            continue
        if entry_kind(account, state, subject_namespace="agent-account") != "directory":
            continue
        organisations = bounded_directory_entries(
            account.path, state, cap=max_directory_entries,
            subject_namespace="agent-account",
        )
        for organisation in organisations:
            if not _safe_scan_path(
                organisation.path,
                root_realpath,
                state,
                subject_namespace="agent-owner",
            ):
                continue
            if entry_kind(
                organisation, state, subject_namespace="agent-owner"
            ) != "directory":
                continue
            if owner_total >= max_owners:
                state.cap("owner_cap_reached")
                stop = True
                break
            owner_total += 1
            sessions = bounded_directory_entries(
                organisation.path,
                state,
                cap=max_directory_entries,
                subject_namespace="agent-owner",
            )
            for session in sessions:
                if not session.name.startswith("local_"):
                    continue
                if not _safe_scan_path(
                    session.path,
                    root_realpath,
                    state,
                    subject_namespace="agent-local",
                ):
                    continue
                if entry_kind(
                    session, state, subject_namespace="agent-local"
                ) != "directory":
                    continue
                if session_total >= max_sessions:
                    state.cap("session_cap_reached")
                    stop = True
                    break
                session_total += 1
                local_transcript_counts, transcript_stop = (
                    _inventory_local_transcript_root(
                        session.path,
                        state,
                        scan_root_realpath=root_realpath,
                        max_directory_entries=max_directory_entries,
                        max_transcript_entries=max_transcript_entries,
                        transcript_file_count=transcript_counts[
                            "transcript_file_count"
                        ],
                    )
                )
                for key, value in local_transcript_counts.items():
                    transcript_counts[key] += value
                if transcript_stop:
                    stop = True
                outputs_path = os.path.join(session.path, "outputs")
                if not _safe_scan_path(
                    outputs_path,
                    root_realpath,
                    state,
                    subject_namespace="agent-outputs",
                ):
                    if stop:
                        break
                    continue
                outputs_kind = expected_path_kind(
                    outputs_path,
                    state,
                    expected="directory",
                    subject_namespace="agent-outputs",
                )
                if outputs_kind == "directory":
                    outputs_directory_total += 1
                    remaining = max_output_entries - output_entry_total
                    if remaining <= 0:
                        state.cap("output_entry_cap_reached")
                        stop = True
                    else:
                        outputs = bounded_directory_entries(
                            outputs_path,
                            state,
                            cap=max_directory_entries,
                            subject_namespace="outputs",
                        )
                        safe_outputs = [
                            entry for entry in outputs
                            if _safe_scan_path(
                                entry.path,
                                root_realpath,
                                state,
                                subject_namespace="agent-output-entry",
                            )
                        ]
                        count = min(len(safe_outputs), remaining)
                        output_entry_total += count
                        if len(safe_outputs) > remaining:
                            state.cap("output_entry_cap_reached")
                            stop = True
                if stop:
                    break
            if stop:
                break
        if stop:
            break

    return _result(
        state,
        "present",
        owner_total,
        session_total,
        outputs_directory_total,
        output_entry_total,
        transcript_counts,
        max_directory_entries,
        max_owners,
        max_sessions,
        max_output_entries,
        max_transcript_entries,
        _standard_root_summary(
            state,
            max_directory_entries=max_directory_entries,
            standard_appdata_claude_dir=standard_appdata_claude_dir,
            standard_projects_dir=standard_projects_dir,
        ),
    )


def _result(state, root_status, owner_total, session_total,
            outputs_directory_total, output_entry_total,
            transcript_counts, max_directory_entries, max_owners, max_sessions,
            max_output_entries, max_transcript_entries, standard_roots):
    result = {
        "audit": "local_agent_session_inventory",
        **state.fields(),
        "buckets": {
            "root": {"status": root_status, "count": 1 if root_status == "present" else 0},
            "owner": {"count": owner_total},
            "local_*": {"count": session_total},
            "outputs": {
                "directory_count": outputs_directory_total,
                "entry_count": output_entry_total,
            },
        },
        "transcript_roots": transcript_counts,
        "limits": {
            "max_directory_entries": max_directory_entries,
            "max_owners": max_owners,
            "max_sessions": max_sessions,
            "max_output_entries": max_output_entries,
            "max_transcript_entries": max_transcript_entries,
        },
    }
    if standard_roots is not None:
        result["standard_roots"] = standard_roots
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--state", help="Fixture state root")
    parser.add_argument(
        "--root",
        default=os.path.join(default_claude_appdata_dir(), "local-agent-mode-sessions"),
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--max-directory-entries", type=int, default=20000)
    parser.add_argument("--max-owners", type=int, default=10000)
    parser.add_argument("--max-sessions", type=int, default=100000)
    parser.add_argument("--max-output-entries", type=int, default=1000000)
    parser.add_argument("--max-transcript-entries", type=int, default=1000000)
    parser.add_argument(
        "--standard-appdata-claude-dir",
        help="Override the standard appdata/Claude root used for comparison",
    )
    parser.add_argument(
        "--standard-projects-dir",
        help="Override the standard projects root used for comparison",
    )
    args = parser.parse_args(argv)
    state_root = os.path.abspath(args.state) if args.state else None
    root = (
        os.path.join(
            state_root, "appdata", "Claude", "local-agent-mode-sessions"
        )
        if state_root
        else args.root
    )
    standard_appdata_claude_dir = (
        args.standard_appdata_claude_dir
        or (os.path.join(state_root, "appdata", "Claude") if state_root else None)
        or default_claude_appdata_dir()
    )
    standard_projects_dir = (
        args.standard_projects_dir
        or (os.path.join(state_root, "projects") if state_root else None)
        or default_claude_sessions_index_dir()
    )
    result = inventory(
        root,
        max_directory_entries=max(1, args.max_directory_entries),
        max_owners=max(1, args.max_owners),
        max_sessions=max(1, args.max_sessions),
        max_output_entries=max(1, args.max_output_entries),
        max_transcript_entries=max(1, args.max_transcript_entries),
        standard_appdata_claude_dir=standard_appdata_claude_dir,
        standard_projects_dir=standard_projects_dir,
    )
    if args.json:
        write_json(result)
    else:
        print("Local-agent session inventory: {}".format(result["status"]))
        print("Root: {}".format(result["buckets"]["root"]["status"]))
        print("Owners: {}".format(result["buckets"]["owner"]["count"]))
        print("local_* sessions: {}".format(result["buckets"]["local_*"]["count"]))
        print("outputs directories: {}".format(
            result["buckets"]["outputs"]["directory_count"]
        ))
        print("outputs entries: {}".format(
            result["buckets"]["outputs"]["entry_count"]
        ))
        print("local-agent projects roots: {}".format(
            result["transcript_roots"]["projects_root_count"]
        ))
        print("local-agent project directories: {}".format(
            result["transcript_roots"]["project_directory_count"]
        ))
        print("local-agent transcript files: {}".format(
            result["transcript_roots"]["transcript_file_count"]
        ))
        print("standard metadata files: {}".format(
            result["standard_roots"]["claude-code-sessions"][
                "physical_file_count"
            ]
        ))
        print("standard transcript files: {}".format(
            result["standard_roots"]["projects"]["physical_file_count"]
        ))
        print("Errors: {}".format(result["error_count"]))
    return 2 if result["partial"] else 0


if __name__ == "__main__":
    sys.exit(main())
