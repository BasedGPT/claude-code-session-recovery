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
    DEFAULT_MAX_LINE_BYTES,
    DEFAULT_MAX_NODES_PER_FILE,
    audit_transcript_paths,
    redact_user_home,
)


def _projects_transcripts(projects_dir):
    """Return direct slug-child JSONLs in deterministic physical order."""
    try:
        with os.scandir(projects_dir) as entries:
            slugs = sorted(entries, key=lambda entry: entry.name)
    except OSError as exc:
        raise ValueError("projects scan root is unavailable") from exc
    paths = []
    errors = []
    for slug in slugs:
        try:
            is_directory = slug.is_dir()
        except OSError:
            errors.append("slug_stat_failed")
            continue
        if not is_directory:
            continue
        try:
            with os.scandir(slug.path) as entries:
                names = sorted(entries, key=lambda entry: entry.name)
        except OSError:
            errors.append("slug_list_failed")
            continue
        for entry in names:
            if not entry.name.endswith(".jsonl"):
                continue
            try:
                is_file = entry.is_file()
            except OSError:
                errors.append("transcript_stat_failed")
                continue
            if is_file:
                paths.append(os.path.abspath(entry.path))
            else:
                errors.append("transcript_not_regular_file")
    scan_errors = [
        {"reference": f"scan-entry-{index:04d}", "code": code}
        for index, code in enumerate(errors, start=1)
    ]
    return paths, scan_errors


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
    if projects_dir is not None:
        if not os.path.isdir(projects_dir):
            raise ValueError("projects scan root is unavailable")
        discovered, scan_errors = _projects_transcripts(projects_dir)
    return sorted(set(discovered + explicit)), scan_errors


def _public_result(result, *, include_paths=False, details=False, limit=30):
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
        "--max-nodes-per-file", type=int, default=DEFAULT_MAX_NODES_PER_FILE,
        help="Maximum UUID/message values retained per file.",
    )
    args = parser.parse_args(argv)
    if args.limit < 0:
        parser.error("--limit must be non-negative")

    try:
        paths, scan_errors = _resolve_paths(args)
        result = audit_transcript_paths(
            paths,
            max_line_bytes=args.max_line_bytes,
            max_nodes_per_file=args.max_nodes_per_file,
        )
        result["summary"]["scan_error_count"] = len(scan_errors)
        if scan_errors:
            result["errors"].extend(scan_errors)
            result["errors"].sort(
                key=lambda error: (error["reference"], error["code"])
            )
            result["status"] = "partial"
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
