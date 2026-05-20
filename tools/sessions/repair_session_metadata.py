"""
Invoked via diagnose.py. Not intended for direct invocation.
To diagnose your state: python tools/diagnose.py

Repair session metadata files that are missing the cliSessionId field.
Adds cliSessionId by matching each broken metadata file's createdAt
timestamp against the first timestamp in each JSONL transcript, within
a configurable window (default: 5 seconds). Single-candidate matches
only -- ambiguous matches are skipped with a report.

Files read:
  - %APPDATA%\\Claude\\claude-code-sessions\\<account-uuid>\\<org-uuid>\\local_*.json
  - %USERPROFILE%\\.claude\\projects\\<slug>\\*.jsonl

Files written:
  - %APPDATA%\\Claude\\claude-code-sessions\\<account-uuid>\\<org-uuid>\\<filename>.json
    (only with --apply; cliSessionId field added in-place)

Backup created at:
  - ./repair-backup/<original-filename>.json  (alongside this script)

Rollback command:
  - copy /Y repair-backup\\*.json "%APPDATA%\\Claude\\claude-code-sessions\\<account-uuid>\\<org-uuid>\\"

Usage:
    python tools/sessions/repair_session_metadata.py --diagnosis-id <hex>
    python tools/sessions/repair_session_metadata.py --diagnosis-id <hex> --apply
    python tools/sessions/repair_session_metadata.py --diagnosis-id <hex> --window-ms 8000
"""
import argparse
import glob
import json
import os
import shutil
import sys
from datetime import datetime, timezone

# Import shared helpers from tools/diagnose.py (parent directory).
_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _TOOLS_DIR)
try:
    from diagnose import build_snapshot, make_diagnosis_id, _find_meta_dirs
except ImportError as exc:
    print("ERROR: cannot import from diagnose.py: {}".format(exc))
    print("Run from the repo root: python tools/sessions/repair_session_metadata.py")
    sys.exit(1)

# --- Configuration ---
# Used when --state is not supplied (live mode).
# PROJECTS_DIR: where JSONL transcripts live (typically ~/.claude/projects).
PROJECTS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "projects")
# APPDATA_CLAUDE_DIR: parent of the claude-code-sessions directory.
APPDATA_CLAUDE_DIR = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")), "Claude"
)

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(TOOL_DIR, "repair-backup")


# ---------------------------------------------------------------------------
# Gate 5 -- Known-do-not-run conditions
# Checked after diagnosis-token validation.
# Refusal exits 3 with the message.
# ---------------------------------------------------------------------------

KNOWN_DO_NOT_RUN = [
    (
        lambda s: s["metadata_missing_cli_count"] == 0,
        "All metadata files already have cliSessionId. Nothing to repair.",
    ),
    (
        lambda s: s["schema_version"] == "unrecognised",
        (
            "State schema not recognised. Run diagnose.py and report "
            "the unsupported state to the maintainer."
        ),
    ),
]


# ---------------------------------------------------------------------------
# Indexing helpers
# ---------------------------------------------------------------------------

def _parse_created_at_ms(value):
    """Return createdAt as ms-since-epoch int, handling int or ISO string."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except ValueError:
            return None
    return None


def _read_jsonl_first_ts_and_user(jsonl_path):
    """Return (first_ts_ms, first_user_text) from a JSONL transcript.

    Reads records sequentially until both values are found or EOF.
    Returns (None, None) on any error.
    """
    first_ts_ms = None
    first_user = None
    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Extract timestamp from first record that has one
                if first_ts_ms is None:
                    ts = rec.get("timestamp")
                    if ts:
                        try:
                            dt = datetime.fromisoformat(
                                ts.replace("Z", "+00:00")
                            )
                            first_ts_ms = int(dt.timestamp() * 1000)
                        except ValueError:
                            pass
                # Extract first user message text
                if first_user is None and rec.get("type") == "user":
                    msg = rec.get("message", {})
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        content = msg.get("content", "")
                        if isinstance(content, str):
                            first_user = content[:80]
                        elif isinstance(content, list):
                            for item in content:
                                if (
                                    isinstance(item, dict)
                                    and item.get("type") == "text"
                                ):
                                    first_user = item.get("text", "")[:80]
                                    break
                if first_ts_ms is not None and first_user is not None:
                    break
    except OSError:
        pass
    return first_ts_ms, first_user


def index_metadata(appdata_claude_dir):
    """Return (by_cli, broken_no_cli).

    by_cli: {cliSessionId: (path, parsed_dict)}
    broken_no_cli: [(path, parsed_dict)] for files lacking cliSessionId
    """
    by_cli = {}
    broken = []
    for _acct, _org, meta_dir in _find_meta_dirs(appdata_claude_dir):
        for f in sorted(glob.glob(os.path.join(meta_dir, "local_*.json"))):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            cli = data.get("cliSessionId")
            if cli:
                by_cli[cli] = (f, data)
            else:
                broken.append((f, data))
    return by_cli, broken


def index_jsonls(projects_dir):
    """Return {session_id: (jsonl_path, first_ts_ms, first_user_text)}."""
    out = {}
    if not os.path.isdir(projects_dir):
        return out
    for slug in sorted(os.listdir(projects_dir)):
        slug_dir = os.path.join(projects_dir, slug)
        if not os.path.isdir(slug_dir):
            continue
        for f in glob.glob(os.path.join(slug_dir, "*.jsonl")):
            sid = os.path.splitext(os.path.basename(f))[0]
            if len(sid) != 36:
                continue
            first_ts_ms, first_user = _read_jsonl_first_ts_and_user(f)
            out[sid] = (f, first_ts_ms, first_user)
    return out


# ---------------------------------------------------------------------------
# Match logic
# ---------------------------------------------------------------------------

def find_match(broken_meta, jsonl_index, by_cli, window_ms):
    """Return (cliSessionId, ambiguity_label).

    Labels:
      "unique"              -- single candidate in window, not yet claimed
      "unique-with-dupe"    -- single candidate, already claimed by another meta
      "none"                -- no candidate in window
      "multi"               -- multiple candidates too close to distinguish
    """
    created_ms = _parse_created_at_ms(broken_meta.get("createdAt"))
    if created_ms is None:
        return None, "none"

    candidates = []
    for sid, (path, jfirst, juser) in jsonl_index.items():
        if jfirst is None:
            continue
        delta = abs(jfirst - created_ms)
        if delta <= window_ms:
            candidates.append((delta, sid, path, juser))
    candidates.sort()

    if not candidates:
        return None, "none"
    if len(candidates) > 1 and candidates[1][0] - candidates[0][0] < 500:
        return candidates[0][1], "multi"

    cli_match = candidates[0][1]
    if cli_match in by_cli:
        return cli_match, "unique-with-dupe"
    return cli_match, "unique"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(
        description=(
            "Repair session metadata files missing cliSessionId. "
            "Dry-run by default -- add --apply to mutate."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--diagnosis-id",
        metavar="HEX",
        default=None,
        dest="diagnosis_id",
        help="Diagnosis token from diagnose.py (required).",
    )
    ap.add_argument(
        "--force-with-diagnosis-id",
        metavar="VALUE",
        default=None,
        dest="force_diagnosis_id",
        help="Set to 'audit-only' to run dry-run without a current token.",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Apply repairs in-place. Default is dry-run.",
    )
    ap.add_argument(
        "--window-ms",
        type=int,
        default=5000,
        metavar="MS",
        help="createdAt vs JSONL first-timestamp match window in ms (default: 5000).",
    )
    ap.add_argument(
        "--state",
        metavar="PATH",
        default=None,
        help=(
            "Fixture state directory for testing. "
            "Must contain appdata/Claude/... and projects/ subdirectories."
        ),
    )
    args = ap.parse_args()

    # --- Gate 3: diagnosis-token check ---
    force_mode = args.force_diagnosis_id == "audit-only"
    if not args.diagnosis_id and not force_mode:
        print("ERROR: --diagnosis-id required.")
        print("Run: python tools/diagnose.py")
        sys.exit(2)
    if args.apply and force_mode:
        print("ERROR: --apply cannot be combined with --force-with-diagnosis-id=audit-only.")
        sys.exit(2)

    # Resolve directories
    if args.state:
        state_abs = os.path.abspath(args.state)
        appdata_claude_dir = os.path.join(state_abs, "appdata", "Claude")
        projects_dir = os.path.join(state_abs, "projects")
    else:
        appdata_claude_dir = APPDATA_CLAUDE_DIR
        projects_dir = PROJECTS_DIR

    # Compute current snapshot and diagnosis ID
    snapshot = build_snapshot(
        appdata_claude_dir, projects_dir,
        fixture_mode=(args.state is not None),
    )
    current_id = make_diagnosis_id(snapshot)

    if not force_mode and current_id != args.diagnosis_id:
        print(
            "ERROR: Diagnosis token mismatch.\n"
            "  Supplied : {}\n"
            "  Current  : {}".format(args.diagnosis_id, current_id)
        )
        print(
            "State has changed since diagnose.py was last run. "
            "Re-run: python tools/diagnose.py"
        )
        sys.exit(2)

    # --- Gate 5: known-do-not-run conditions ---
    for predicate, message in KNOWN_DO_NOT_RUN:
        try:
            if predicate(snapshot):
                print("REFUSED: " + message)
                sys.exit(3)
        except Exception:
            pass

    # Index state
    by_cli, broken = index_metadata(appdata_claude_dir)
    jsonl_index = index_jsonls(projects_dir)

    used_diagnosis_id = args.diagnosis_id if not force_mode else "(forced-audit-only)"
    print("Metadata files: {} (linked: {}, missing cliSessionId: {})".format(
        len(by_cli) + len(broken), len(by_cli), len(broken)
    ))
    print("JSONL files: {}".format(len(jsonl_index)))
    print("Match window: +-{}ms".format(args.window_ms))
    print("Mode: {}".format("APPLY" if args.apply else "dry-run (use --apply to mutate)"))
    print("Diagnosis ID: {}".format(used_diagnosis_id))
    print()

    if args.apply:
        os.makedirs(BACKUP_DIR, exist_ok=True)

    repaired = 0
    refused_multi = 0
    orphan = 0

    for path, meta in sorted(broken, key=lambda x: _parse_created_at_ms(x[1].get("createdAt")) or 0):
        fname = os.path.basename(path)
        title = meta.get("title", "")[:60]
        created_ms = _parse_created_at_ms(meta.get("createdAt"))
        created_display = (
            datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            if created_ms else "?"
        )

        cli_match, kind = find_match(meta, jsonl_index, by_cli, args.window_ms)

        if kind == "none":
            print("  ORPHAN  {} | {} | {!r} -- no JSONL match in window".format(
                fname, created_display, title
            ))
            orphan += 1
            continue

        if kind == "multi":
            print("  REFUSE  {} | {} | {!r} -- multiple JSONL candidates, manual review needed".format(
                fname, created_display, title
            ))
            refused_multi += 1
            continue

        _jsonl_path, _jfirst, juser = jsonl_index[cli_match]
        dupe_note = " (dupe already linked)" if kind == "unique-with-dupe" else ""

        print("  REPAIR  {}{}".format(fname, dupe_note))
        print("          {} | {!r}".format(created_display, title))
        print("          cliSessionId = {}".format(cli_match))
        if juser:
            print("          first user: {!r}".format(juser))
        print()

        if args.apply:
            shutil.copy2(path, os.path.join(BACKUP_DIR, fname))
            repaired_meta = dict(meta)
            repaired_meta["cliSessionId"] = cli_match
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(repaired_meta, fh, indent=2)

        repaired += 1

    print("Repaired: {}  Refused (multi-candidate): {}  Orphan (no JSONL): {}".format(
        repaired, refused_multi, orphan
    ))
    if not args.apply and repaired:
        print("Review dry-run output above, then re-run with --apply to apply changes.")


if __name__ == "__main__":
    main()
