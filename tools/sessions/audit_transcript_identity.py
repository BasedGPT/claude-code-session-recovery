"""Read-only inventory of physical transcript identity and slug evidence."""

import argparse
from collections import defaultdict
import json
import os
import sys

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from platform_support import default_claude_paths  # noqa: E402
from session_metadata import build_metadata_path_inventory  # noqa: E402
from session_state import slug_encode  # noqa: E402
from transcript_audit import read_first_record_field, redact_user_home  # noqa: E402


def _projects_transcripts(projects_dir):
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
                transcripts = sorted(entries, key=lambda entry: entry.name)
        except OSError:
            errors.append("slug_list_failed")
            continue
        for entry in transcripts:
            if not entry.name.endswith(".jsonl"):
                continue
            try:
                is_file = entry.is_file()
            except OSError:
                errors.append("transcript_stat_failed")
                continue
            if not is_file:
                errors.append("transcript_not_regular_file")
                continue
            session_id = os.path.splitext(entry.name)[0]
            paths.append((session_id, os.path.abspath(entry.path)))
    scan_errors = [
        {"reference": f"scan-entry-{index:04d}", "code": code}
        for index, code in enumerate(errors, start=1)
    ]
    return paths, scan_errors


def _metadata_records(appdata_claude_dir):
    inventory = build_metadata_path_inventory(appdata_claude_dir)
    records = [
        {
            "path": record.path,
            "account": record.account_uuid,
            "organisation": record.organisation_uuid,
            "data": record.data,
        }
        for record in inventory.records
    ]
    errors = [
        {"reference": error.reference, "code": error.code}
        for error in inventory.errors
    ]
    return records, errors


def _is_worktree_path(value):
    if not isinstance(value, str):
        return False
    normalised = value.replace("\\", "/").casefold()
    return "/.claude/worktrees/" in normalised or normalised.endswith("/.claude/worktrees")


def _resolve_paths(args):
    _live_appdata, live_projects = default_claude_paths()
    if args.state:
        state = os.path.abspath(args.state)
        if not os.path.isdir(state):
            raise ValueError("state scan root is unavailable")
        projects_dir = os.path.join(state, "projects")
        appdata_dir = os.path.join(state, "appdata", "Claude")
    else:
        projects_dir = os.path.abspath(args.projects_dir or live_projects)
        appdata_dir = os.path.abspath(args.appdata_claude_dir or _live_appdata)
    if not os.path.isdir(projects_dir):
        raise ValueError("projects scan root is unavailable")
    return projects_dir, appdata_dir


def audit_identity(projects_dir, appdata_claude_dir=None, explicit_cwds=()):
    """Return an internal identity-audit result.

    Internal path and field values are retained only long enough for the CLI
    to redact them.  They are never printed by this module.
    """
    explicit_cwds = tuple(explicit_cwds)
    discovered, scan_errors = _projects_transcripts(projects_dir)
    physical = []
    for session_id, path in discovered:
        slug = os.path.basename(os.path.dirname(path))
        physical.append({
            "session_id": session_id,
            "path": os.path.abspath(path),
            "slug": slug,
            "cwd": None,
        })
    physical.sort(key=lambda item: (item["session_id"], item["path"]))
    for index, item in enumerate(physical, start=1):
        item["reference"] = f"transcript-{index:04d}"

    errors = list(scan_errors)
    project_discovery_partial = bool(scan_errors)
    bounded_field_references = []
    for item in physical:
        field_read = read_first_record_field(item["path"], "cwd")
        item["cwd"] = field_read["value"]
        if field_read["error"]:
            project_discovery_partial = True
            errors.append({
                "reference": item["reference"],
                "code": "transcript_read_failed",
            })
        if field_read["bounded"]:
            bounded_field_references.append(item["reference"])

    by_session = defaultdict(list)
    for item in physical:
        by_session[item["session_id"]].append(item)
    duplicate_groups = [
        values for _sid, values in sorted(by_session.items()) if len(values) > 1
    ]
    duplicate_groups.sort(key=lambda values: tuple(item["path"] for item in values))
    duplicate_group_records = [
        {
            "reference": f"session-id-group-{index:04d}",
            "session_id": values[0]["session_id"],
            "paths": [item["path"] for item in values],
            "transcript_references": [item["reference"] for item in values],
        }
        for index, values in enumerate(duplicate_groups, start=1)
    ]

    slug_collision_groups = []
    observed_cwds = defaultdict(set)
    for item in physical:
        if item["cwd"]:
            observed_cwds[slug_encode(item["cwd"])].add(item["cwd"])
    for encoded_slug, cwds in sorted(observed_cwds.items()):
        if len(cwds) > 1:
            slug_collision_groups.append({
                "slug": encoded_slug,
                "cwd_count": len(cwds),
                "transcript_references": sorted(
                    item["reference"] for item in physical
                    if item["cwd"] in cwds
                ),
            })
    for index, group in enumerate(slug_collision_groups, start=1):
        group["reference"] = f"slug-collision-{index:04d}"

    resolved_groups = defaultdict(set)
    for item in physical:
        cwd = item["cwd"]
        if not cwd:
            continue
        try:
            resolved = os.path.realpath(cwd)
        except OSError:
            continue
        if not resolved:
            continue
        key = os.path.normcase(os.path.abspath(resolved))
        resolved_groups[key].add(item["slug"])
    resolved_split_groups = [
        {
            "resolved_key": key,
            "slug_count": len(slugs),
            "slugs": sorted(slugs),
        }
        for key, slugs in sorted(resolved_groups.items())
        if len(slugs) > 1
    ]
    for index, group in enumerate(resolved_split_groups, start=1):
        group["reference"] = f"resolved-path-split-{index:04d}"

    metadata, metadata_errors = _metadata_records(appdata_claude_dir)
    errors.extend(metadata_errors)
    metadata_ambiguous = 0
    cwd_mismatches = 0
    worktree_candidates = 0
    metadata_rows = []
    for index, entry in enumerate(metadata, start=1):
        data = entry["data"]
        cli = data.get("cliSessionId")
        candidates = by_session.get(cli, ()) if isinstance(cli, str) else ()
        expected_slug = slug_encode(data["cwd"]) if data.get("cwd") else None
        candidate_slugs = {candidate["slug"] for candidate in candidates}
        ambiguous = len(candidates) > 1
        mismatch = bool(candidates and expected_slug and expected_slug not in candidate_slugs)
        if ambiguous:
            metadata_ambiguous += 1
        if mismatch:
            cwd_mismatches += 1
        candidate_worktree = any(
            _is_worktree_path(data.get(field))
            for field in ("cwd", "worktreePath")
        ) or any(_is_worktree_path(candidate.get("cwd")) for candidate in candidates)
        if mismatch and candidate_worktree:
            worktree_candidates += 1
        metadata_rows.append({
            "reference": f"metadata-{index:04d}",
            "path": entry["path"],
            "candidate_count": len(candidates),
            "ambiguous": ambiguous,
            "cwd_slug_mismatch": mismatch,
            "worktree_key_mismatch_candidate": bool(mismatch and candidate_worktree),
            "candidate_references": [candidate["reference"] for candidate in candidates],
        })

    explicit_rows = []
    explicit_cwd_mismatches = 0
    for index, cwd in enumerate(explicit_cwds, start=1):
        expected_slug = slug_encode(cwd)
        matches = [item for item in physical if item["slug"] == expected_slug]
        mismatch = not bool(matches)
        if mismatch:
            explicit_cwd_mismatches += 1
        explicit_rows.append({
            "reference": f"cwd-{index:04d}",
            "match_count": len(matches),
            "cwd_slug_mismatch": mismatch,
            "transcript_references": [item["reference"] for item in matches],
        })

    if project_discovery_partial:
        duplicate_group_records = []
        slug_collision_groups = []
        resolved_split_groups = []
        metadata_rows = []
        explicit_rows = []
        metadata_ambiguous = 0
        cwd_mismatches = 0
        worktree_candidates = 0
        explicit_cwd_mismatches = 0

    findings = []
    findings.extend(
        {"kind": "duplicate_session_id", "reference": group["reference"]}
        for group in duplicate_group_records
    )
    findings.extend(
        {"kind": "observed_slug_collision", "reference": group["reference"]}
        for group in slug_collision_groups
    )
    findings.extend(
        {"kind": "resolved_path_split", "reference": group["reference"]}
        for group in resolved_split_groups
    )
    findings.extend(
        {
            "kind": "metadata_ambiguous_transcript",
            "reference": row["reference"],
        }
        for row in metadata_rows if row["ambiguous"]
    )
    findings.extend(
        {"kind": "cwd_slug_mismatch", "reference": row["reference"]}
        for row in metadata_rows if row["cwd_slug_mismatch"]
    )
    findings.extend(
        {"kind": "worktree_key_mismatch_candidate", "reference": row["reference"]}
        for row in metadata_rows if row["worktree_key_mismatch_candidate"]
    )
    findings.extend(
        {"kind": "explicit_cwd_slug_mismatch", "reference": row["reference"]}
        for row in explicit_rows if row["cwd_slug_mismatch"]
    )
    if not project_discovery_partial:
        findings.extend(
            {"kind": "bounded_identity_parse", "reference": reference}
            for reference in bounded_field_references
        )
    findings.sort(key=lambda finding: (finding["reference"], finding["kind"]))
    errors.sort(key=lambda error: (error["reference"], error["code"]))
    status = "partial" if errors else "bounded" if bounded_field_references else "complete"

    return {
        "schema_version": "transcript-identity-audit-v1",
        "read_only": True,
        "status": status,
        "_identity_analysis_suppressed": project_discovery_partial,
        "summary": {
            "physical_transcript_count": len(physical),
            "unique_session_id_count": len(by_session),
            "duplicate_session_id_group_count": len(duplicate_group_records),
            "observed_slug_collision_group_count": len(slug_collision_groups),
            "resolved_path_split_group_count": len(resolved_split_groups),
            "metadata_ambiguous_transcript_count": metadata_ambiguous,
            "cwd_slug_mismatch_count": cwd_mismatches,
            "worktree_key_mismatch_candidate_count": worktree_candidates,
            "explicit_cwd_count": len(explicit_cwds),
            "explicit_cwd_mismatch_count": explicit_cwd_mismatches,
            "scan_error_count": len(errors),
            "bounded_field_read_count": len(bounded_field_references),
        },
        "findings": findings,
        "errors": errors,
        "transcripts": physical,
        "duplicate_groups": duplicate_group_records,
        "slug_collision_groups": slug_collision_groups,
        "resolved_split_groups": resolved_split_groups,
        "metadata": metadata_rows,
        "explicit_cwds": explicit_rows,
    }


def _public_result(result, *, include_paths=False, details=False, limit=30):
    if result.get("_identity_analysis_suppressed"):
        return {
            key: result[key]
            for key in ("schema_version", "read_only", "status", "summary", "errors")
        }
    public = {
        key: value for key, value in result.items()
        if key not in {
            "_identity_analysis_suppressed", "transcripts", "duplicate_groups",
            "slug_collision_groups", "resolved_split_groups", "metadata",
            "explicit_cwds",
        }
    }
    public["findings"] = result["findings"][:limit]
    if details or include_paths:
        public["transcripts"] = []
        for item in result["transcripts"][:limit]:
            row = {
                "reference": item["reference"],
                "recorded_cwd": bool(item["cwd"]),
            }
            if include_paths:
                row["path"] = redact_user_home(item["path"])
            public["transcripts"].append(row)
        public["duplicate_groups"] = [
            {
                "reference": group["reference"],
                "transcript_count": len(group["transcript_references"]),
                **({"paths": [redact_user_home(path) for path in group["paths"]]} if include_paths else {}),
            }
            for group in result["duplicate_groups"][:limit]
        ]
        public["slug_collision_groups"] = [
            {
                "reference": group["reference"],
                "cwd_count": group["cwd_count"],
            }
            for group in result["slug_collision_groups"][:limit]
        ]
        public["resolved_split_groups"] = [
            {
                "reference": group["reference"],
                "slug_count": group["slug_count"],
            }
            for group in result["resolved_split_groups"][:limit]
        ]
        public["metadata"] = [
            {
                key: value for key, value in row.items()
                if key != "path" and key != "candidate_references"
            } | ({"path": redact_user_home(row["path"])} if include_paths else {})
            for row in result["metadata"][:limit]
        ]
        public["explicit_cwds"] = [
            {key: value for key, value in row.items() if key != "transcript_references"}
            for row in result["explicit_cwds"][:limit]
        ]
    return public


def _print_human(result, *, include_paths=False, details=False, limit=30):
    summary = result["summary"]
    print("Transcript identity audit (read-only)")
    print("Status: {}".format(result["status"]))
    print("Scan errors: {}".format(summary.get("scan_error_count", 0)))
    for key in (
        "physical_transcript_count", "unique_session_id_count",
        "duplicate_session_id_group_count", "observed_slug_collision_group_count",
        "resolved_path_split_group_count", "metadata_ambiguous_transcript_count",
        "cwd_slug_mismatch_count", "worktree_key_mismatch_candidate_count",
    ):
        print("{}: {}".format(key, summary[key]))
    if result.get("_identity_analysis_suppressed"):
        for error in result["errors"]:
            print("  {} [{}]".format(error["reference"], error["code"]))
        return
    print("Findings: {}".format(len(result["findings"])))
    for finding in result["findings"][:limit]:
        print("  {} [{}]".format(finding["reference"], finding["kind"]))
    if details or include_paths:
        for item in result["transcripts"][:limit]:
            label = item["reference"]
            if include_paths:
                label += " " + redact_user_home(item["path"])
            print("  {} recorded_cwd={}".format(label, bool(item["cwd"])))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Inventory transcript paths and identity ambiguity without mutation."
    )
    parser.add_argument("--state", metavar="PATH", help="Fixture state root containing appdata/ and projects/.")
    parser.add_argument("--projects-dir", metavar="PATH", help="Override the projects scan root.")
    parser.add_argument("--appdata-claude-dir", metavar="PATH", help="Override Claude Desktop data root.")
    parser.add_argument("--cwd", action="append", default=[], metavar="PATH", help="Explicit cwd candidate; may repeat.")
    parser.add_argument("--json", action="store_true", help="Emit the machine-readable envelope.")
    parser.add_argument("--details", action="store_true", help="Include opaque per-file and group details.")
    parser.add_argument("--include-paths", action="store_true", help="Include user-home-redacted paths.")
    parser.add_argument("--limit", type=int, default=30, help="Maximum displayed detail rows (default: 30).")
    args = parser.parse_args(argv)
    if args.limit < 0:
        parser.error("--limit must be non-negative")
    try:
        projects_dir, appdata_dir = _resolve_paths(args)
        result = audit_identity(projects_dir, appdata_dir, args.cwd)
    except ValueError as exc:
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
