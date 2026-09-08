"""
Claude Code Desktop Session Recovery Tools -- Diagnostic
=========================================================

Files read:
  - Claude Desktop's platform-specific claude-code-sessions directory
  - ~/.claude/projects/<slug>/*.jsonl (Windows/macOS)
  - %LOCALAPPDATA%\\AnthropicClaude\\  (version detection only)

Files written:
  - Nothing. This tool is read-only.

Invoked directly. Mutators are invoked via the commands this tool prints.

Usage:
    python tools/diagnose.py                   # probe live state
    python tools/diagnose.py --json            # machine-readable output
    python tools/diagnose.py --state <path>    # probe a fixture state dir
                                               # (<path>/appdata/Claude/... and
                                               #  <path>/projects/...)
"""
import argparse
import json
import ntpath
import os
import platform
import re
import stat
import sys

from session_state import (
    build_snapshot,
    default_claude_paths,
    desktop_process_check_command,
    make_diagnosis_id,
    scan_vscode_dropped_sessions,
    slug_encode,
)


# ---------------------------------------------------------------------------
# Compatibility
# ---------------------------------------------------------------------------

_INTERNAL_SNAPSHOT_KEYS = {"_desktop_session_pair_identities"}
_RAW_PAIR_IDENTITY_KEYS = {"account_uuid", "organisation_uuid"}

# Focused slug tests and downstream callers historically import this name
# from diagnose.py; retain this narrow compatibility alias while state lives in
# session_state.py.
_slug_encode = slug_encode


def _redact_user_home(path):
    """Replace the current user's home prefix before displaying a path."""
    if not isinstance(path, str) or not path:
        return path
    home = os.path.abspath(os.path.expanduser("~"))
    path_norm = os.path.normcase(os.path.normpath(path))
    home_norm = os.path.normcase(os.path.normpath(home))
    if path_norm == home_norm:
        return "%USERPROFILE%" if platform.system() == "Windows" else "~"
    home_prefix = home_norm.rstrip("\\/") + os.sep
    if path_norm.startswith(home_prefix):
        placeholder = "%USERPROFILE%" if platform.system() == "Windows" else "~"
        return placeholder + path[len(home):]
    return path


def _redact_snapshot(value, key=None):
    """Return a diagnostic snapshot safe to paste into an issue report."""
    if isinstance(value, dict):
        return {
            child_key: _redact_snapshot(child_value, child_key)
            for child_key, child_value in value.items()
            if (
                child_key not in _INTERNAL_SNAPSHOT_KEYS
                and child_key not in _RAW_PAIR_IDENTITY_KEYS
            )
        }
    if isinstance(value, list):
        return [_redact_snapshot(item, key) for item in value]
    if isinstance(value, str) and key:
        lowered_key = key.lower()
        if lowered_key == "cwd" or lowered_key.endswith("_path") or lowered_key == "repo_root":
            return _redact_user_home(value)
    return value


def _shell_display_path(path):
    """Return a redacted path that remains copy/pasteable in the shell."""
    redacted = _redact_user_home(path)
    if platform.system() == "Darwin" and isinstance(redacted, str) and redacted.startswith("~"):
        return "$HOME" + redacted[1:]
    return redacted


# ---------------------------------------------------------------------------
# Explicit path-alias observation
# ---------------------------------------------------------------------------

def _looks_like_windows_path(path):
    """Return whether *path* uses a Windows drive or UNC spelling."""
    return isinstance(path, str) and bool(re.match(r"^(?:[A-Za-z]:[\\/]|\\\\)", path))


def _windows_path_parts(path):
    """Return case-folded non-root components for a Windows path."""
    normalised = ntpath.normpath(path).replace("/", "\\")
    drive, tail = ntpath.splitdrive(normalised)
    del drive
    return tuple(part.casefold() for part in tail.split("\\") if part)


def _has_shared_windows_suffix(original_cwd, resolved_cwd):
    """Return whether the resolved path retains the original path suffix."""
    original_parts = _windows_path_parts(original_cwd)
    resolved_parts = _windows_path_parts(resolved_cwd)
    return bool(original_parts) and len(resolved_parts) >= len(original_parts) and (
        resolved_parts[-len(original_parts):] == original_parts
    )


def _classify_observed_path_forms(original_cwd, resolved_cwd):
    """Classify an observed path pair without asserting physical identity."""
    if original_cwd == resolved_cwd:
        return "same-path-form"
    if not (_looks_like_windows_path(original_cwd) and _looks_like_windows_path(resolved_cwd)):
        return "mixed-path-form"
    original_drive = ntpath.splitdrive(original_cwd)[0].casefold()
    resolved_drive = ntpath.splitdrive(resolved_cwd)[0].casefold()
    if original_drive and resolved_drive and original_drive != resolved_drive:
        if _has_shared_windows_suffix(original_cwd, resolved_cwd):
            return "ancestor-volume-mount-candidate"
    return "mixed-windows-path-form"


def _entry_is_reparse_point(entry):
    """Return whether a matching project directory is a link/reparse point."""
    try:
        if entry.is_symlink():
            return True
        attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))
    except OSError:
        return True


def _scan_project_slug(projects_dir, slug, *, case_insensitive=False):
    """Count direct JSONL files in a case-insensitive slug directory.

    The scan is deliberately limited to the two caller-supplied slugs.  It
    does not follow directory links or read transcript contents.
    """
    result = {
        "slug": slug_encode(_redact_user_home(slug)),
        "directory_count": 0,
        "jsonl_count": 0,
        "reparse_point_count": 0,
        "scan_status": "complete",
    }
    try:
        with os.scandir(projects_dir) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name.casefold())
    except OSError:
        result["scan_status"] = "unavailable"
        return result

    requested_name = slug.casefold() if case_insensitive else slug
    for entry in entries:
        entry_name = entry.name.casefold() if case_insensitive else entry.name
        if entry_name != requested_name:
            continue
        try:
            if _entry_is_reparse_point(entry):
                result["reparse_point_count"] += 1
                result["scan_status"] = "reparse_point"
                continue
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            result["scan_status"] = "partial"
            continue
        result["directory_count"] += 1
        try:
            children = os.scandir(entry.path)
        except OSError:
            result["scan_status"] = "partial"
            continue
        try:
            with children:
                for child in children:
                    try:
                        child_name = child.name.casefold() if case_insensitive else child.name
                        if child.is_file(follow_symlinks=False) and child_name.endswith(".jsonl"):
                            result["jsonl_count"] += 1
                    except OSError:
                        result["scan_status"] = "partial"
        except OSError:
            result["scan_status"] = "partial"
    return result


def build_path_alias_diagnostic(projects_dir, original_cwd=None, resolved_cwd=None):
    """Build a read-only report from explicitly observed path strings.

    ``original_cwd`` is the path reported by the writer/metadata side and
    ``resolved_cwd`` is the path observed in the reader (for example, the
    VS Code extension log).  The report compares their derived project slug
    directories but never resolves either path or asserts that they identify
    the same physical directory.
    """
    if not original_cwd or not resolved_cwd:
        return {
            "schema_version": "path-alias-diagnostic-v1",
            "status": "invalid_input",
            "physical_identity": "unverified",
            "message": "Provide both --cwd and --resolved-cwd from observed output.",
        }

    original_slug = slug_encode(original_cwd)
    resolved_slug = slug_encode(resolved_cwd)
    windows_path_mode = (
        _looks_like_windows_path(original_cwd)
        and _looks_like_windows_path(resolved_cwd)
    )
    # The explicit path spelling is the authority for this comparison.  A
    # POSIX fixture may be exercised on a Windows test host, where folding
    # case would incorrectly merge two case-sensitive fixture directories.
    case_insensitive = windows_path_mode
    original = _scan_project_slug(
        projects_dir, original_slug, case_insensitive=case_insensitive
    )
    resolved = _scan_project_slug(
        projects_dir, resolved_slug, case_insensitive=case_insensitive
    )
    path_forms_differ = original_cwd != resolved_cwd
    if case_insensitive:
        slugs_differ = original_slug.casefold() != resolved_slug.casefold()
    else:
        slugs_differ = original_slug != resolved_slug
    scan_complete = original["scan_status"] == resolved["scan_status"] == "complete"
    source_has_jsonl = original["jsonl_count"] > 0 if scan_complete else None
    reader_has_jsonl = resolved["jsonl_count"] > 0 if scan_complete else None
    original["slug"] = slug_encode(_redact_user_home(original_cwd))
    resolved["slug"] = slug_encode(_redact_user_home(resolved_cwd))

    if not path_forms_differ:
        status = "same_path_form"
        message = "The observed paths are equivalent after Windows path normalisation."
    elif not slugs_differ:
        status = "path_forms_share_slug"
        message = "The observed paths differ textually but derive the same project slug."
    elif not scan_complete:
        status = "insufficient_evidence"
        message = "The compared project slug directories could not be scanned completely."
    elif source_has_jsonl and not reader_has_jsonl:
        status = "observed_slug_mismatch_candidate"
        message = (
            "JSONL files are present under the original slug while the resolved slug "
            "has no JSONL files. This is compatible with a path alias mismatch."
        )
    elif source_has_jsonl and reader_has_jsonl:
        status = "both_slugs_have_transcripts"
        message = "Both derived slug directories contain JSONL files; a split is observed but not isolated."
    elif reader_has_jsonl:
        status = "resolved_slug_has_transcripts_only"
        message = "JSONL files are present only under the resolved slug; the original slug has none."
    else:
        status = "no_transcripts_for_comparison"
        message = "The compared slug directories contain no JSONL files to distinguish."

    return {
        "schema_version": "path-alias-diagnostic-v1",
        "status": status,
        "path_form_classification": _classify_observed_path_forms(original_cwd, resolved_cwd),
        "physical_identity": "unverified",
        "message": message,
        "limitations": [
            "Observed path forms do not prove that both paths identify the same physical location.",
            "Python and VS Code resolver behaviour is not inferred from this report.",
            "JSONL presence is checked by filename only; transcript content is not validated.",
            "No move, rewrite, rebind, or repair command is selected.",
        ],
        "original": {
            "cwd": original_cwd,
            "slug": original["slug"],
            "directory_count": original["directory_count"],
            "jsonl_count": original["jsonl_count"],
            "reparse_point_count": original["reparse_point_count"],
            "scan_status": original["scan_status"],
        },
        "resolved": {
            "cwd": resolved_cwd,
            "slug": resolved["slug"],
            "directory_count": resolved["directory_count"],
            "jsonl_count": resolved["jsonl_count"],
            "reparse_point_count": resolved["reparse_point_count"],
            "scan_status": resolved["scan_status"],
        },
        "evidence": {
            "path_forms_differ": path_forms_differ,
            "slugs_differ": slugs_differ,
            "original_jsonl_present": source_has_jsonl,
            "resolved_jsonl_present": reader_has_jsonl,
        },
    }


# ---------------------------------------------------------------------------
# Predicate evaluator
# ---------------------------------------------------------------------------

def _eval_comparison(actual, op, expected):
    """Evaluate a single comparison operation."""
    if op == "==":
        return actual == expected
    if op == "!=":
        return actual != expected
    if op == ">=":
        return actual is not None and actual >= expected
    if op == "<=":
        return actual is not None and actual <= expected
    if op == ">":
        return actual is not None and actual > expected
    if op == "<":
        return actual is not None and actual < expected
    if op == "in":
        return actual in expected
    if op == "regex":
        return bool(re.search(expected, str(actual or "")))
    return False


def eval_match(predicate, snapshot):
    """Evaluate a match predicate dict against a snapshot. Returns bool."""
    if not predicate:
        return False
    if "any" in predicate:
        return any(eval_match(p, snapshot) for p in predicate["any"])
    if "all" in predicate:
        return all(eval_match(p, snapshot) for p in predicate["all"])
    # Leaf node: {"snapshot.field[.subfield...]": {"op": value}}
    # Supports dot-notation for nested dicts, e.g. "snapshot.cwd_prefix_types.bare_root".
    for key, comparison in predicate.items():
        field = key[len("snapshot."):] if key.startswith("snapshot.") else key
        parts = field.split(".")
        actual = snapshot
        for part in parts:
            if isinstance(actual, dict):
                actual = actual.get(part)
            else:
                actual = None
                break
        for op, expected in comparison.items():
            if not _eval_comparison(actual, op, expected):
                return False
    return True


def _suppress_ambiguous_synthesis_routes(matches, snapshot):
    """Copy synthesis findings without routes when destination is ambiguous.

    ``jsonl_orphan_count`` is a useful read-only signal, but synthesis is a
    write-bearing operation and cannot safely choose among multiple
    account/organisation roots. Keep the diagnostic findings while removing
    only the unsafe synthesis route; never mutate the loaded troubleshooting
    rows because they are the process-wide routing source of truth.
    """
    if len(snapshot.get("desktop_session_pairs", [])) <= 1:
        return matches
    safe_matches = []
    for row in matches:
        if row.get("mutator") == "tools/sessions/synth_session_metadata.py":
            safe_row = dict(row)
            safe_row["mutator"] = None
            safe_row["next_command"] = None
            safe_matches.append(safe_row)
        else:
            safe_matches.append(row)
    return safe_matches


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

_SEP = "-" * 60


def _format_human(diagnosis_id, snapshot, matches, schema_ok, repo_root=None,
                  vscode_dropped=0, vscode_db_count=0, path_alias=None):
    lines = [
        "DIAGNOSE -- Claude Code Desktop Session Recovery Tools",
        _SEP,
        f"Diagnosis ID : {diagnosis_id}",
    ]
    if snapshot.get("desktop_version"):
        lines.append(f"Desktop ver. : {snapshot['desktop_version']}")
    if snapshot.get("cli_version"):
        lines.append(f"CLI ver.     : {snapshot['cli_version']}")
    lines.append(
        f"Metadata     : {snapshot['total_metadata_count']} files"
        f"  ({snapshot['metadata_with_cli_count']} with cliSessionId,"
        f"  {snapshot['metadata_missing_cli_count']} missing)"
    )
    lines.append(f"JSONL files  : {snapshot['jsonl_count']}")
    if snapshot.get("desktop_session_pairs"):
        pairs = snapshot["desktop_session_pairs"]
        lines.append(f"Desktop pairs : {len(pairs)}")
        for pair in pairs:
            lines.append(
                "  {} local_*.json={}".format(
                    pair["pair_label"],
                    pair["local_metadata_count"],
                )
            )
        lines.append("")
    if snapshot.get("truncated_jsonl_count", 0) > 0:
        lines.append(
            f"Truncated    : {snapshot['truncated_jsonl_count']} session(s) have fewer"
            " messages than completedTurns records (history appears cut off)"
        )
    if snapshot.get("metadata_null_timestamp_count", 0) > 0:
        lines.append(
            f"Null stamps  : {snapshot['metadata_null_timestamp_count']} metadata file(s)"
            " have null createdAt/updatedAt (can blank the entire session list)"
        )
    lines.append("")

    if snapshot.get("desktop_running"):
        check_command = desktop_process_check_command()
        if snapshot.get("running_inside_desktop"):
            lines.append("WARNING: You are running inside Claude Desktop itself.")
            lines.append("  You cannot quit Desktop to run mutators from this session.")
            lines.append("")
            lines.append("  Do this in order:")
            lines.append("    1. Open a new terminal or shell")
            if repo_root:
                lines.append('    2. cd "{}"'.format(_shell_display_path(repo_root)))
            if platform.system() == "Darwin":
                lines.append("    3. Quit Claude Desktop from the menu bar")
            else:
                lines.append("    3. Quit Claude Desktop fully (window and tray)")
            lines.append(f"    4. {check_command}  -- must show no results")
            lines.append("    5. Run the repair commands printed below")
        else:
            lines.append("WARNING: Claude Desktop appears to be running.")
            lines.append("  Diagnose is safe -- it is read-only.")
            lines.append("  Repair commands below will NOT work until Desktop is fully quit.")
            if platform.system() == "Darwin":
                lines.append("  Quit Claude Desktop from the menu bar. Then verify:")
            else:
                lines.append("  Quit Claude Desktop fully (window and tray). Then verify:")
            lines.append(f"    {check_command}")
        lines.append("")

    if snapshot.get("install_type") == "msix":
        lines.append("NOTE: Microsoft Store (MSIX) install detected.")
        msix_path = _redact_user_home(snapshot.get("msix_real_path"))
        if msix_path:
            lines.append(f"  Data path: {msix_path}")
        lines.append("  diagnose.py is read-only and works correctly on MSIX.")
        lines.append("  The write-bearing repair scripts (repair_session_metadata.py,")
        lines.append("  synth_session_metadata.py) are unlikely to surface sessions on")
        lines.append("  an MSIX install: Desktop maintains an internal index that takes")
        lines.append("  precedence over files written externally, regardless of write")
        lines.append("  access to the real package path. Community-confirmed 2026-06-03.")
        lines.append("  See README.md for details.")
        lines.append("")

    if snapshot.get("mapped_drive_unc_mismatch_count", 0) > 0:
        drives = snapshot.get("mapped_drive_affected_drives", [])
        drive_label = "drives" if len(drives) > 1 else "drive"
        drives_str = ", ".join(f"{d}:\\" for d in drives)
        lines.append(f"NOTE: Mapped network {drive_label} detected ({drives_str}).")
        lines.append("  Project slugs under these drive letters won't appear in the VS Code")
        lines.append("  extension's session history sidebar. The extension resolves drive letters")
        lines.append("  to UNC paths (\\\\server\\share\\...) and derives a different slug — one that")
        lines.append("  doesn't exist on disk. The Desktop app and CLI are not affected.")
        lines.append("")
        lines.append("  To fix the VS Code extension sidebar, choose one option:")
        lines.append("    A: Open VS Code via the UNC path instead of the drive letter.")
        lines.append("    B: Rename ~/.claude/projects/<drive-slug>/ to the UNC-encoded slug.")
        lines.append("       See README.md for step-by-step instructions.")
        lines.append("")

    if path_alias is not None:
        lines.append("PATH ALIAS OBSERVATION:")
        lines.append(f"  Status      : {path_alias['status']}")
        if path_alias.get("path_form_classification"):
            lines.append(
                f"  Path forms  : {path_alias['path_form_classification']}"
            )
        original = path_alias.get("original", {})
        resolved = path_alias.get("resolved", {})
        if original.get("cwd"):
            lines.append(f"  Original cwd: {_shell_display_path(original['cwd'])}")
            lines.append(
                "  Original slug: {} ({} JSONL file(s), scan={})".format(
                    original.get("slug"),
                    original.get("jsonl_count", 0),
                    original.get("scan_status", "unknown"),
                )
            )
        if resolved.get("cwd"):
            lines.append(f"  Resolved cwd: {_shell_display_path(resolved['cwd'])}")
            lines.append(
                "  Resolved slug: {} ({} JSONL file(s), scan={})".format(
                    resolved.get("slug"),
                    resolved.get("jsonl_count", 0),
                    resolved.get("scan_status", "unknown"),
                )
            )
        if path_alias.get("message"):
            lines.append(f"  Finding     : {path_alias['message']}")
        lines.append("  Physical identity: not verified from these path strings.")
        lines.append("  No repair, move, rewrite, or rebind is selected by this observation.")
        lines.append("")

    if vscode_dropped > 0:
        lines.append(
            f"NOTE: {vscode_dropped} transcript file(s) on disk are not in the VS Code"
            " extension's session cache."
        )
        lines.append(
            "  Sessions may be missing from Local -> Session History in VS Code even"
        )
        lines.append("  though the transcripts are intact and resumable via the CLI.")
        lines.append("  To recover them:")
        lines.append(
            "    python tools/sessions/recover_vscode_sessions.py"
        )
        lines.append(
            "    python tools/sessions/recover_vscode_sessions.py --apply  "
            "(VS Code must be closed)"
        )
        lines.append("")

    if not schema_ok:
        lines.append("State layout not in supported fixture set. Audit-only mode.")
        lines.append("No repair commands will be suggested for this state.")
        lines.append(
            "Please open an issue at https://github.com/BasedGPT/"
            "claude-code-session-recovery with your diagnose.py --json output."
        )
        return "\n".join(lines)

    if not matches:
        lines.append("State looks healthy. No known broken patterns matched.")
        return "\n".join(lines)

    quit_prefix = "QUIT DESKTOP FIRST: " if snapshot.get("desktop_running") else ""

    for row in matches:
        lines.append(f"PROBLEM FOUND: {row['problem']}")
        lines.append(f"  Details: {row['details']}")
        label = "Safety" if row.get("mutator") else "Status"
        lines.append(f"  {label} : {row['safety']}")
        lines.append("")
        if row.get("mutator"):
            mutator = row["mutator"]
            lines.append("  To repair -- dry-run first, review output, then add --apply:")
            lines.append(
                f"    {quit_prefix}python {mutator}"
                f" --diagnosis-id {diagnosis_id}"
            )
            lines.append(
                f"    {quit_prefix}python {mutator}"
                f" --diagnosis-id {diagnosis_id} --apply"
            )
        elif row.get("next_command"):
            lines.append(f"  Next:  {row['next_command']}")
        else:
            lines.append("  No automatic repair for this state. See:")
            lines.append(f"    {row['details']}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # REL-09 / PY-06: all I/O and side effects inside main()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(
        description="Diagnose Claude Code Desktop session state. Read-only.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Run this first. It tells you exactly what to do next.\n"
            "Mutators shown in the output require --diagnosis-id from this tool."
        ),
    )
    ap.add_argument(
        "--state",
        metavar="PATH",
        default=None,
        help=(
            "Path to a fixture state directory (for testing). "
            "Must contain appdata/Claude/... and projects/ subdirectories."
        ),
    )
    ap.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    ap.add_argument(
        "--cwd",
        dest="path_alias_cwd",
        metavar="PATH",
        help=(
            "Observed writer/metadata cwd for a read-only path-alias check. "
            "Use with --resolved-cwd; values should come from the relevant logs."
        ),
    )
    ap.add_argument(
        "--resolved-cwd",
        dest="path_alias_resolved_cwd",
        metavar="PATH",
        help=(
            "Observed reader/VS Code cwd for a read-only path-alias check. "
            "Use with --cwd; diagnose does not infer resolver behaviour."
        ),
    )
    args = ap.parse_args()

    # Resolve state directories
    if args.state:
        state_abs = os.path.abspath(args.state)
        appdata_claude_dir = os.path.join(state_abs, "appdata", "Claude")
        projects_dir = os.path.join(state_abs, "projects")
    else:
        appdata_claude_dir, projects_dir = default_claude_paths()

    snapshot = build_snapshot(appdata_claude_dir, projects_dir, fixture_mode=args.state is not None)
    diagnosis_id = make_diagnosis_id(snapshot)

    # Load troubleshooting.json from the repo root (tools/ -> parent = repo root)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(script_dir)
    ts_path = os.path.join(repo_root, "troubleshooting.json")

    rows = []
    if os.path.isfile(ts_path):
        with open(ts_path, "r", encoding="utf-8") as fh:
            rows = json.load(fh)

    schema_ok = snapshot["schema_version"] == "recognised"
    matches = (
        [row for row in rows if eval_match(row.get("match", {}), snapshot)]
        if schema_ok else []
    )
    matches = _suppress_ambiguous_synthesis_routes(matches, snapshot)

    path_alias = None
    if args.path_alias_cwd or args.path_alias_resolved_cwd:
        path_alias = build_path_alias_diagnostic(
            projects_dir,
            args.path_alias_cwd,
            args.path_alias_resolved_cwd,
        )

    # VS Code session cache check — live mode only; skipped in fixture/json mode
    # so golden outputs stay deterministic.
    vscode_dropped = 0
    vscode_db_count = 0
    if not args.state and not args.json_output:
        vscode_dropped, vscode_db_count = scan_vscode_dropped_sessions(projects_dir)

    if args.json_output:
        output = {
            "diagnosis_id": diagnosis_id,
            "tested_against": {
                "claude_desktop": snapshot.get("desktop_version"),
                "claude_code_cli": snapshot.get("cli_version"),
                "platform": "fixture" if args.state else platform.system(),
            },
            "schema_probe": snapshot["schema_version"],
            "install_type": snapshot.get("install_type"),
            "msix_real_path": _redact_user_home(snapshot.get("msix_real_path")),
            "desktop_running": snapshot["desktop_running"],
            "matched_problems": [
                {
                    "id": row["id"],
                    "domain": row["domain"],
                    "mutator": row.get("mutator"),
                    "next_command": (
                        "python {} --diagnosis-id {}".format(row["mutator"], diagnosis_id)
                        if row.get("mutator") else None
                    ),
                    "safety_preconditions": [row["safety"]],
                }
                for row in matches
            ],
            "audit_only_problems": [],
            "schema_mismatch": not schema_ok,
            "snapshot": _redact_snapshot(snapshot),
        }
        if path_alias is not None:
            output["path_alias_diagnostic"] = _redact_snapshot(path_alias)
        print(json.dumps(output, indent=2))
    else:
        print(_format_human(
            diagnosis_id, snapshot, matches, schema_ok, repo_root,
            vscode_dropped=vscode_dropped, vscode_db_count=vscode_db_count,
            path_alias=path_alias,
        ))


if __name__ == "__main__":
    main()
