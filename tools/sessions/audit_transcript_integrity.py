"""Read-only JSONL integrity and transcript-graph audit.

The command reports structural facts only.  It does not decide which leaf is
active, does not call a transcript unreachable, and never mutates or prints
transcript content.
"""

import argparse
import json
import os
import sys

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from platform_support import default_claude_paths  # noqa: E402
from transcript_audit import (  # noqa: E402
    AuditConfigurationError,
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_LINE_BYTES,
    DEFAULT_MAX_LINES_PER_FILE,
    DEFAULT_MAX_NODES_PER_FILE,
    audit_transcript_paths,
    redact_user_home,
    suppress_partial_analysis,
)
from transcript_files import build_transcript_path_inventory  # noqa: E402


def _projects_transcripts(projects_dir):
    """Return direct slug-child JSONLs in deterministic physical order."""
    inventory = build_transcript_path_inventory(
        projects_dir,
        session_id_is_valid=lambda _session_id: True,
    )
    paths = [
        path
        for _session_id, session_paths in inventory.by_session_id.items()
        for path in session_paths
    ]
    scan_errors = [
        {"reference": f"scan-entry-{index:04d}", "code": error.code}
        for index, error in enumerate(inventory.errors, start=1)
    ]
    return paths, scan_errors, inventory.is_complete


def _resolve_paths(args):
    live_appdata, live_projects = default_claude_paths()
    explicit = [os.path.abspath(path) for path in args.transcript]

    if args.state:
        state = os.path.abspath(args.state)
        if not os.path.isdir(state):
            raise ValueError("state scan root is unavailable")
        projects_dir = os.path.join(state, "projects")
    elif args.projects_dir:
        projects_dir = os.path.abspath(args.projects_dir)
    elif explicit:
        projects_dir = None
    else:
        projects_dir = live_projects

    discovered = []
    scan_errors = []
    inventory_complete = True
    if projects_dir is not None:
        if not os.path.isdir(projects_dir):
            raise ValueError("projects scan root is unavailable")
        discovered, scan_errors, inventory_complete = _projects_transcripts(
            projects_dir
        )
    return sorted(set(discovered + explicit)), scan_errors, inventory_complete


def _public_result(result, *, include_paths=False, details=False, limit=30):
    if result.get("_analysis_suppressed"):
        return {
            key: result[key]
            for key in (
                "schema_version", "read_only", "status", "limits", "summary",
                "errors",
            )
        }
    public = {
        key: value
        for key, value in result.items()
        if key != "files"
    }
    public["findings"] = result["findings"][:limit]
    if details or include_paths:
        files = []
        for file in result["files"][:limit]:
            item = {
                key: value for key, value in file.items()
                if key not in {"path", "errors"}
            }
            if include_paths:
                item["path"] = redact_user_home(file["path"])
            if file.get("errors"):
                item["error_codes"] = list(file["errors"])
            files.append(item)
        public["files"] = files
    return public


def _print_human(result, *, include_paths=False, details=False, limit=30):
    summary = result["summary"]
    print("Transcript integrity audit (read-only)")
    print("Status: {}".format(result["status"]))
    print("Scan errors: {}".format(summary.get("scan_error_count", 0)))
    if result.get("_analysis_suppressed"):
        print("Audited files: {}".format(summary["audited_file_count"]))
        print("Retained bytes read: {}".format(summary["retained_bytes_read"]))
        print("Retained physical lines: {}".format(
            summary["retained_physical_line_count"]
        ))
        for error in result["errors"]:
            print("  {} [{}]".format(error["reference"], error["code"]))
        return
    print(
        "Files: expected={} present={} missing={} empty={} unreadable={}".format(
            summary["files_expected"], summary["files_present"],
            summary["files_missing"], summary["files_empty"],
            summary["files_unreadable"],
        )
    )
    print(
        "Records: lines={} blank={} malformed={} non_object={} invalid_utf8={} nul_lines={}".format(
            summary["physical_lines"], summary["blank_lines"],
            summary["malformed_json"], summary["non_object_json"],
            summary["invalid_utf8_lines"], summary["nul_lines"],
        )
    )
    print(
        "Graph: nodes={} roots={} missing_parents={} forks={} leaves={} components={} "
        "reachable={} unrooted={} cycles={}".format(
            summary["node_count"], summary["explicit_roots"],
            summary["missing_parent_references"], summary["fork_points"],
            summary["leaves"], summary["weak_components"],
            summary["reachable_from_explicit_roots"], summary["unrooted_nodes"],
            summary["cycle_count"],
        )
    )
    print(
        "Parent references: total={} retained={} truncated={}".format(
            summary["parent_reference_count"],
            summary["parent_references_retained"],
            summary["parent_references_truncated"],
        )
    )
    findings = result["findings"]
    print("Findings: {}".format(len(findings)))
    for finding in findings[:limit]:
        print("  {} [{}]".format(finding["reference"], finding["kind"]))
    if len(findings) > limit:
        print("  ... and {} more".format(len(findings) - limit))
    if details or include_paths:
        for file in result["files"][:limit]:
            label = file["reference"]
            if include_paths:
                label += " " + redact_user_home(file["path"])
            print("  {}: {}".format(label, file["state"]))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Audit transcript bytes, JSON records, and graph topology read-only."
    )
    parser.add_argument("--state", metavar="PATH", help="Fixture state root containing projects/.")
    parser.add_argument("--projects-dir", metavar="PATH", help="Override the projects scan root.")
    parser.add_argument(
        "--transcript", action="append", default=[], metavar="PATH",
        help="Explicit transcript path; may be repeated and may be missing.",
    )
    parser.add_argument("--json", action="store_true", help="Emit the machine-readable envelope.")
    parser.add_argument("--details", action="store_true", help="Include per-file structural details.")
    parser.add_argument("--include-paths", action="store_true", help="Include user-home-redacted paths.")
    parser.add_argument("--limit", type=int, default=30, help="Maximum displayed detail rows (default: 30).")
    parser.add_argument(
        "--max-line-bytes", type=int, default=DEFAULT_MAX_LINE_BYTES,
        help="Maximum retained bytes per physical line.",
    )
    parser.add_argument(
        "--max-file-bytes", type=int, default=DEFAULT_MAX_FILE_BYTES,
        help="Maximum bytes read from one transcript.",
    )
    parser.add_argument(
        "--max-lines-per-file", type=int, default=DEFAULT_MAX_LINES_PER_FILE,
        help="Maximum physical JSONL lines read from one transcript.",
    )
    parser.add_argument(
        "--max-nodes-per-file", type=int, default=DEFAULT_MAX_NODES_PER_FILE,
        help="Maximum UUID/message values retained per file.",
    )
    args = parser.parse_args(argv)
    if args.limit < 0:
        parser.error("--limit must be non-negative")

    try:
        paths, scan_errors, inventory_complete = _resolve_paths(args)
        result = audit_transcript_paths(
            paths,
            max_line_bytes=args.max_line_bytes,
            max_file_bytes=args.max_file_bytes,
            max_lines_per_file=args.max_lines_per_file,
            max_nodes_per_file=args.max_nodes_per_file,
        )
        if not inventory_complete:
            suppress_partial_analysis(result, extra_errors=scan_errors)
        else:
            result["summary"]["scan_error_count"] = 0
    except (AuditConfigurationError, ValueError) as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 2

    public = _public_result(
        result,
        include_paths=args.include_paths,
        details=args.details,
        limit=args.limit,
    )
    if args.json:
        print(json.dumps(public, sort_keys=True, separators=(",", ":")))
    else:
        _print_human(
            result,
            include_paths=args.include_paths,
            details=args.details,
            limit=args.limit,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
