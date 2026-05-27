"""
Claude Code Desktop Session Recovery Tools -- Diagnostic
=========================================================

Files read:
  - %APPDATA%\\Claude\\claude-code-sessions\\<account-uuid>\\<org-uuid>\\local_*.json
  - %USERPROFILE%\\.claude\\projects\\<slug>\\*.jsonl
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
import collections
import glob
import hashlib
import json
import os
import platform
import re
import subprocess
import sys


# ---------------------------------------------------------------------------
# State discovery
# ---------------------------------------------------------------------------

def _find_meta_dirs(appdata_claude_dir):
    """Yield (account_uuid, org_uuid, absolute_dir_path) for each metadata dir.

    Walks: <appdata_claude_dir>/claude-code-sessions/<acct>/<org>/
    """
    sessions_root = os.path.join(appdata_claude_dir, "claude-code-sessions")
    if not os.path.isdir(sessions_root):
        return
    for acct in sorted(os.listdir(sessions_root)):
        acct_dir = os.path.join(sessions_root, acct)
        if not os.path.isdir(acct_dir):
            continue
        for org in sorted(os.listdir(acct_dir)):
            org_dir = os.path.join(acct_dir, org)
            if not os.path.isdir(org_dir):
                continue
            yield acct, org, org_dir


def _build_jsonl_index(projects_dir):
    """Return {session_id: absolute_jsonl_path} for every *.jsonl found."""
    index = {}
    if not os.path.isdir(projects_dir):
        return index
    for slug in sorted(os.listdir(projects_dir)):
        slug_dir = os.path.join(projects_dir, slug)
        if not os.path.isdir(slug_dir):
            continue
        for f in glob.glob(os.path.join(slug_dir, "*.jsonl")):
            sid = os.path.splitext(os.path.basename(f))[0]
            if len(sid) == 36:  # UUID-length check
                index[sid] = f
    return index


def _slug_from_path(path):
    """Derive the project slug from a cwd path string (same encoding Claude uses)."""
    # Strip drive letter colon, replace all path separators with --
    normalised = path.replace("\\", "/")
    # Remove the colon after the drive letter (e.g. "C:" -> "C")
    if len(normalised) >= 2 and normalised[1] == ":":
        normalised = normalised[0] + normalised[2:]
    parts = [p for p in normalised.split("/") if p]
    return "--".join(parts)


def _slug_encode(cwd):
    """Encode a cwd path into the slug Claude Code uses for projects/<slug>/.

    Observed on disk: C:\\Users\\Foo\\project -> C--Users-Foo-project
    Worktree example: C:\\Users\\Foo\\project\\.claude\\worktrees\\foo-1a2b3c
                   -> C--Users-Foo-project--claude-worktrees-foo-1a2b3c

    The CLI applies realpath first, so this function should receive the
    canonical (resolved) path, not a junction alias.
    """
    out = cwd
    for ch in (":", "\\", "/", ".", " "):
        out = out.replace(ch, "-")
    return out


def _count_jsonl_assistant_lines(path, stop_at=None):
    """Count {"role": "assistant"} lines in a JSONL file.

    stop_at: if given, return early once this threshold is reached — the caller
    can treat a return value >= stop_at as "not truncated" without reading the
    whole file. Keeps large sessions fast.
    """
    count = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or '"assistant"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                    if obj.get("type") == "assistant":
                        count += 1
                        if stop_at is not None and count >= stop_at:
                            return count
                except (json.JSONDecodeError, AttributeError):
                    continue
    except OSError:
        return 0
    return count


def _cwd_type(cwd):
    """Classify a cwd path as junction, canonical, bare_root, or other."""
    if not cwd:
        return "other"
    try:
        real = os.path.realpath(cwd)
    except OSError:
        return "other"
    # If the directory doesn't exist, classify as other
    if not os.path.exists(cwd):
        return "other"
    # Junction: realpath differs from the literal path
    if os.path.normcase(real) != os.path.normcase(cwd):
        return "junction"
    # Bare root: only drive letter + one level, no project structure beneath
    parts = cwd.replace("\\", "/").rstrip("/").split("/")
    clean_parts = [p for p in parts if p and p != ":"]
    if len(clean_parts) <= 2:
        return "bare_root"
    return "canonical"


# ---------------------------------------------------------------------------
# Snapshot builder
# ---------------------------------------------------------------------------

def build_snapshot(appdata_claude_dir, projects_dir, fixture_mode=False):
    """Probe state; return a deterministic snapshot dict.

    The snapshot is the input to match predicates in troubleshooting.json and
    the source of the deterministic diagnosis ID.

    fixture_mode=True skips live-system detection (cli version, desktop version,
    process check) so golden outputs generated from fixture state are deterministic
    across environments.
    """
    # Collect all metadata files
    meta_files = []
    for _acct, _org, meta_dir in _find_meta_dirs(appdata_claude_dir):
        for f in sorted(glob.glob(os.path.join(meta_dir, "local_*.json"))):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                meta_files.append((f, data))
            except (OSError, json.JSONDecodeError):
                continue

    jsonl_index = _build_jsonl_index(projects_dir)

    total = len(meta_files)
    # Archived sessions are hidden from Desktop's history picker, so a missing
    # cliSessionId on an archived session does not cause a user-visible
    # blank-pane. Counting them here would trigger a false-positive
    # blank-pane-missing-cli problem report. The audit tool surfaces them
    # separately in the archived_no_cli bucket if the user explicitly looks.
    missing_cli = sum(
        1 for _, d in meta_files
        if not d.get("cliSessionId") and not d.get("isArchived")
    )
    with_cli = sum(1 for _, d in meta_files if d.get("cliSessionId"))

    dangling_cli = sum(
        1 for _, d in meta_files
        if d.get("cliSessionId") and d["cliSessionId"] not in jsonl_index
    )

    # Count cliSessionId values that appear in more than one metadata file.
    # Indicates synth-duplicate metadata created by earlier recovery attempts.
    cli_values = [d.get("cliSessionId") for _, d in meta_files if d.get("cliSessionId")]
    cli_counts = collections.Counter(cli_values)
    duplicate_cli_count = sum(1 for c in cli_counts.values() if c > 1)

    cwd_prefix_types = {"junction": 0, "canonical": 0, "bare_root": 0, "other": 0}
    junction_mismatch_count = 0

    for _, d in meta_files:
        cwd = d.get("cwd", "")
        t = _cwd_type(cwd)
        cwd_prefix_types[t] = cwd_prefix_types.get(t, 0) + 1
        if t == "junction":
            # Junction mismatch: session cwd is a junction, so the slug
            # derived from the junction path differs from the slug derived
            # from the real path. If the JSONL is stored at the canonical
            # slug and the junction-slug dir is absent or empty, these
            # sessions appear as "missing" when navigating via the real path.
            cli = d.get("cliSessionId")
            if cli and cli in jsonl_index:
                try:
                    real = os.path.realpath(cwd)
                    canonical_slug = _slug_from_path(real)
                    junction_slug = _slug_from_path(cwd)
                    if canonical_slug != junction_slug:
                        junction_mismatch_count += 1
                except OSError:
                    pass

    # jsonl_orphan_count: JSONLs with no corresponding metadata cliSessionId.
    # Trigger for synth_session_metadata.py.
    all_jsonl_sids = set(jsonl_index.keys())
    referenced_sids = {d.get("cliSessionId") for _, d in meta_files if d.get("cliSessionId")}
    jsonl_orphan_count = len(all_jsonl_sids - referenced_sids)

    # cwd_slug_mismatch_count: metadata whose cwd slug-encodes to a different slug
    # than the directory containing its JSONL. Works in fixture mode (pure string
    # comparison -- no on-disk existence check needed).
    cwd_slug_mismatch_count = 0
    for _, d in meta_files:
        cli = d.get("cliSessionId")
        cwd = d.get("cwd", "")
        if not cli or not cwd:
            continue
        if cli not in jsonl_index:
            continue
        expected_slug = _slug_encode(cwd)
        actual_slug_dir = os.path.basename(os.path.dirname(jsonl_index[cli]))
        if actual_slug_dir != expected_slug:
            cwd_slug_mismatch_count += 1

    # truncated_jsonl_count: sessions where the JSONL exists and metadata records
    # a completedTurns value, but the file contains fewer assistant-role lines
    # than that count. A truncated session looks intact from the outside — the
    # JSONL is present and the metadata is linked — but opens with missing
    # earlier messages. Skipped in fixture mode because fixture JSONLs are stubs.
    truncated_jsonl_count = 0
    if not fixture_mode:
        for _, d in meta_files:
            cli = d.get("cliSessionId")
            completed_turns = d.get("completedTurns")
            if not cli or not completed_turns or cli not in jsonl_index:
                continue
            if completed_turns <= 0:
                continue
            actual = _count_jsonl_assistant_lines(jsonl_index[cli], stop_at=completed_turns)
            if actual < completed_turns:
                truncated_jsonl_count += 1

    # Schema version: "recognised" if we found metadata files with the expected
    # structure (have sessionId field); "unrecognised" otherwise.
    schema_version = "unrecognised"
    if total > 0:
        known_fields = {"sessionId", "cwd", "createdAt", "model", "title"}
        for _, d in meta_files[:5]:  # spot-check up to 5 files
            if known_fields.intersection(d.keys()):
                schema_version = "recognised"
                break

    # Desktop version, CLI version, and process state are skipped in fixture mode
    # so that golden outputs are deterministic regardless of environment.
    if fixture_mode:
        desktop_version = None
        cli_version = None
        desktop_running = False
        running_inside_desktop = False
    else:
        desktop_version = _detect_desktop_version()
        cli_version = _detect_cli_version()
        desktop_running = _detect_desktop_running()
        running_inside_desktop = _detect_running_inside_desktop() if desktop_running else False

    return {
        "total_metadata_count": total,
        "metadata_with_cli_count": with_cli,
        "metadata_missing_cli_count": missing_cli,
        "metadata_dangling_cli_count": dangling_cli,
        "metadata_duplicate_cli_count": duplicate_cli_count,
        "cwd_junction_mismatch_count": junction_mismatch_count,
        "jsonl_orphan_count": jsonl_orphan_count,
        "cwd_slug_mismatch_count": cwd_slug_mismatch_count,
        "truncated_jsonl_count": truncated_jsonl_count,
        "cwd_prefix_types": cwd_prefix_types,
        "jsonl_count": len(jsonl_index),
        "schema_version": schema_version,
        "desktop_version": desktop_version,
        "cli_version": cli_version,
        "desktop_running": desktop_running,
        "running_inside_desktop": running_inside_desktop,
    }


def _detect_desktop_version():
    local_app = os.environ.get("LOCALAPPDATA", "")
    anthropic_dir = os.path.join(local_app, "AnthropicClaude")
    if not os.path.isdir(anthropic_dir):
        return None
    try:
        for entry in sorted(os.listdir(anthropic_dir), reverse=True):
            if entry.startswith("app-"):
                return entry[4:]  # strip "app-" prefix
    except OSError:
        pass
    return None


def _detect_cli_version():
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            out = result.stdout.strip()
            # Output is typically "2.1.143 (Claude Code)" -- extract semver
            m = re.search(r"\d+\.\d+\.\d+", out)
            if m:
                return m.group(0)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def _detect_desktop_running():
    """Return True only if Claude Desktop (not just the CLI) is running.

    Both Desktop and the CLI appear in tasklist as `claude.exe`, so we
    cannot rely on the process name alone. Desktop installs under
    `%LOCALAPPDATA%\\AnthropicClaude\\app-<version>\\Claude.exe`; the CLI
    runs from wherever npm installed it (typically not under AnthropicClaude).
    We enumerate running `claude.exe` processes and check their executable
    paths. Returns False if no claude.exe under AnthropicClaude is running,
    so a standalone CLI session does not trigger the "QUIT DESKTOP" warning.
    """
    # Try wmic first (deprecated but still present on most installs)
    try:
        result = subprocess.run(
            ["wmic", "process", "where", "name='claude.exe'",
             "get", "ExecutablePath", "/format:csv"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                low = line.lower()
                if "anthropicclaude" in low and "claude.exe" in low:
                    return True
            # wmic ran successfully -- absence of AnthropicClaude path means
            # any claude.exe in tasklist is the CLI, not Desktop.
            return False
    except (OSError, subprocess.TimeoutExpired):
        pass

    # Fallback: PowerShell on systems where wmic is missing (Win11 22H2+).
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process claude -ErrorAction SilentlyContinue | "
             "ForEach-Object { $_.Path }"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if "anthropicclaude" in line.lower():
                    return True
            return False
    except (OSError, subprocess.TimeoutExpired):
        pass

    # Both detection paths failed -- err on the side of caution and report
    # claude.exe presence (the original behaviour). Better to warn unnecessarily
    # than to miss a real running Desktop.
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq claude.exe"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        return "claude.exe" in result.stdout.lower()
    except (OSError, subprocess.TimeoutExpired):
        return False


def _detect_running_inside_desktop():
    """Return True if this process is a descendant of claude.exe.

    Walks the process tree upward from the current PID using wmic. Returns
    False on any error so the caller degrades gracefully.
    """
    try:
        result = subprocess.run(
            ["wmic", "process", "get", "ProcessId,ParentProcessId,Name", "/format:csv"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        # CSV columns: Node, Name, ParentProcessId, ProcessId
        procs = {}
        for line in result.stdout.splitlines():
            parts = line.strip().split(",")
            if len(parts) < 4:
                continue
            try:
                name = parts[1].lower()
                parent_pid = int(parts[2])
                pid = int(parts[3])
                procs[pid] = (name, parent_pid)
            except (ValueError, IndexError):
                continue
        current = os.getpid()
        visited = set()
        while current and current not in visited:
            visited.add(current)
            if current not in procs:
                break
            name, parent = procs[current]
            if "claude" in name:
                return True
            current = parent
        return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Diagnosis ID
# ---------------------------------------------------------------------------

def make_diagnosis_id(snapshot):
    """Return an 8-hex-char deterministic ID derived from the session state.

    Version fields (desktop_version, cli_version) and process state
    (desktop_running) are excluded -- they change independently of the
    broken-state we're diagnosing.
    """
    structural_keys = (
        "total_metadata_count",
        "metadata_with_cli_count",
        "metadata_missing_cli_count",
        "metadata_dangling_cli_count",
        "metadata_duplicate_cli_count",
        "cwd_junction_mismatch_count",
        "cwd_slug_mismatch_count",
        "truncated_jsonl_count",
        "jsonl_orphan_count",
        "cwd_prefix_types",
        "jsonl_count",
        "schema_version",
    )
    structural = {k: snapshot[k] for k in structural_keys if k in snapshot}
    canonical = json.dumps(structural, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]


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


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

_SEP = "-" * 60


def _format_human(diagnosis_id, snapshot, matches, schema_ok, repo_root=None):
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
    if snapshot.get("truncated_jsonl_count", 0) > 0:
        lines.append(
            f"Truncated    : {snapshot['truncated_jsonl_count']} session(s) have fewer"
            " messages than completedTurns records (history appears cut off)"
        )
    lines.append("")

    if snapshot.get("desktop_running"):
        if snapshot.get("running_inside_desktop"):
            lines.append("WARNING: You are running inside Claude Desktop itself.")
            lines.append("  You cannot quit Desktop to run mutators from this session.")
            lines.append("")
            lines.append("  Do this in order:")
            lines.append("    1. Open a new terminal (cmd, PowerShell, or Windows Terminal)")
            if repo_root:
                lines.append('    2. cd "{}"'.format(repo_root))
            lines.append("    3. Quit Claude Desktop: right-click the tray icon -> Quit")
            lines.append('    4. tasklist /FI "IMAGENAME eq claude.exe"  -- must show no results')
            lines.append("    5. Run the repair commands printed below")
        else:
            lines.append("WARNING: Claude Desktop appears to be running (claude.exe in tasklist).")
            lines.append("  Diagnose is safe -- it is read-only.")
            lines.append("  Repair commands below will NOT work until Desktop is fully quit.")
            lines.append("  Quit: right-click the tray icon -> Quit. Then verify:")
            lines.append('    tasklist /FI "IMAGENAME eq claude.exe"')
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
    args = ap.parse_args()

    # Resolve state directories
    if args.state:
        state_abs = os.path.abspath(args.state)
        appdata_claude_dir = os.path.join(state_abs, "appdata", "Claude")
        projects_dir = os.path.join(state_abs, "projects")
    else:
        _sys = platform.system()
        if _sys == "Darwin":
            appdata_claude_dir = os.path.expanduser("~/Library/Application Support/Claude")
            projects_dir = os.path.expanduser("~/.claude/projects")
        elif _sys == "Linux":
            appdata_claude_dir = os.path.expanduser("~/.config/Claude")
            projects_dir = os.path.expanduser("~/.claude/projects")
        else:
            appdata_claude_dir = os.path.join(
                os.environ.get("APPDATA", os.path.expanduser("~")), "Claude"
            )
            projects_dir = os.path.join(os.path.expanduser("~"), ".claude", "projects")

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
    matches = [row for row in rows if eval_match(row.get("match", {}), snapshot)]

    if args.json_output:
        output = {
            "diagnosis_id": diagnosis_id,
            "tested_against": {
                "claude_desktop": snapshot.get("desktop_version"),
                "claude_code_cli": snapshot.get("cli_version"),
                "windows": "11",
            },
            "schema_probe": snapshot["schema_version"],
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
            "snapshot": snapshot,
        }
        print(json.dumps(output, indent=2))
    else:
        print(_format_human(diagnosis_id, snapshot, matches, schema_ok, repo_root))


if __name__ == "__main__":
    main()
