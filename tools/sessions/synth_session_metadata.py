"""
Invoked via diagnose.py. Not intended for direct invocation.
To diagnose your state: python tools/diagnose.py

Synthesise missing Claude Desktop local_*.json metadata files for JSONL
transcripts that exist in ~/.claude/projects/ but are not referenced by any
metadata file's cliSessionId. Desktop's history panel requires a metadata file
to render a session -- without one the session is invisible, even though the
full transcript is on disk.

Files read:
  - %APPDATA%\\Claude\\claude-code-sessions\\<account-uuid>\\<org-uuid>\\local_*.json
  - %USERPROFILE%\\.claude\\projects\\<slug>\\*.jsonl

Files written:
  - (nothing with dry-run / default -- output written to ./synth-out/ instead)
  - %APPDATA%\\Claude\\claude-code-sessions\\<account-uuid>\\<org-uuid>\\local_<uuid>.json
    (created, with --apply only)

Backup created at:
  - ./synth-out/<filename>.json  (alongside this script, always -- review before --apply)

Rollback command:
  - del "%APPDATA%\\Claude\\claude-code-sessions\\<account-uuid>\\<org-uuid>\\local_<uuid>.json"
    (delete the synthesised file -- original state had no file here)

Caveats:
  - Synthesised files set enabledMcpTools and remoteMcpServersConfig to empty
    defaults. MCPs will appear unavailable in the synthesised session until you
    re-enable them in Claude Desktop's settings. This is the deliberate v1
    trade-off -- do not add --copy-mcp or similar flags.

Usage:
    python tools/sessions/synth_session_metadata.py --diagnosis-id <hex>
    python tools/sessions/synth_session_metadata.py --diagnosis-id <hex> --apply
"""
import argparse
import glob
import json
import os
import platform
import sys
import uuid
from datetime import datetime, timezone

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _TOOLS_DIR)
try:
    from diagnose import build_snapshot, make_diagnosis_id, _find_meta_dirs, _build_jsonl_index
except ImportError as exc:
    print("ERROR: cannot import from diagnose.py: {}".format(exc))
    print("Run from the repo root: python tools/sessions/synth_session_metadata.py")
    sys.exit(1)

# --- Configuration ---

def _default_paths():
    """Return (appdata_claude_dir, projects_dir) for the current platform."""
    _sys = platform.system()
    if _sys == "Darwin":
        return (
            os.path.expanduser("~/Library/Application Support/Claude"),
            os.path.expanduser("~/.claude/projects"),
        )
    if _sys == "Linux":
        return (
            os.path.expanduser("~/.config/Claude"),
            os.path.expanduser("~/.claude/projects"),
        )
    return (
        os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Claude"),
        os.path.join(os.path.expanduser("~"), ".claude", "projects"),
    )


APPDATA_CLAUDE_DIR, PROJECTS_DIR = _default_paths()

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(TOOL_DIR, "synth-out")


# ---------------------------------------------------------------------------
# Gate 5 -- Known-do-not-run conditions
# ---------------------------------------------------------------------------

KNOWN_DO_NOT_RUN = [
    (
        lambda s: s["jsonl_orphan_count"] == 0,
        "No orphan JSONL files found. All transcripts already have metadata.",
    ),
    (
        lambda s: s["metadata_missing_cli_count"] > 0,
        (
            "Some metadata files are missing cliSessionId. Run "
            "repair_session_metadata.py first to link those sessions, "
            "then re-run diagnose.py before synthesising new metadata."
        ),
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
# JSONL reading
# ---------------------------------------------------------------------------

def _read_jsonl_summary(path):
    """Walk a JSONL once and extract fields needed for metadata synthesis."""
    first_ts = None
    last_ts = None
    first_user_text = None
    last_model = None
    cwd = None
    user_turn_count = 0

    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ts = rec.get("timestamp")
                if ts:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts

                if cwd is None and rec.get("cwd"):
                    cwd = rec["cwd"]

                msg = rec.get("message")
                if isinstance(msg, dict):
                    role = msg.get("role")

                    if role == "user" and rec.get("type") == "user":
                        user_turn_count += 1
                        if first_user_text is None:
                            c = msg.get("content")
                            if isinstance(c, str):
                                first_user_text = c
                            elif isinstance(c, list):
                                for item in c:
                                    if isinstance(item, dict) and item.get("type") == "text":
                                        first_user_text = item.get("text", "")
                                        break

                    if role == "assistant":
                        m = msg.get("model")
                        if m:
                            last_model = m
    except OSError:
        pass

    return {
        "first_ts": first_ts,
        "last_ts": last_ts,
        "first_user_text": first_user_text,
        "last_model": last_model,
        "cwd": cwd,
        "user_turn_count": user_turn_count,
    }


def _iso_to_epoch_ms(iso_str):
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def _derive_title(first_user_text, fallback_id):
    if not first_user_text:
        return "Recovered session {}".format(fallback_id[:8])
    for line in first_user_text.splitlines():
        line = line.strip()
        if line:
            return line[:80]
    return "Recovered session {}".format(fallback_id[:8])


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------

def synthesise(cli_session_id, jsonl_path):
    """Build a synthetic metadata dict for one orphan JSONL."""
    summary = _read_jsonl_summary(jsonl_path)
    new_session_id = "local_{}".format(uuid.uuid4())
    cwd = summary["cwd"] or ""

    return {
        "sessionId": new_session_id,
        "cliSessionId": cli_session_id,
        "cwd": cwd,
        "originCwd": cwd,
        "createdAt": _iso_to_epoch_ms(summary["first_ts"]),
        "lastActivityAt": _iso_to_epoch_ms(summary["last_ts"]),
        "model": summary["last_model"] or "claude-sonnet-4-6",
        "isArchived": False,
        "title": _derive_title(summary["first_user_text"], cli_session_id),
        "titleSource": "auto",
        "permissionMode": "default",
        "completedTurns": summary["user_turn_count"],
        # v1: empty MCP config. Re-enable MCPs manually in Desktop after synthesis.
        "enabledMcpTools": {},
        "remoteMcpServersConfig": [],
    }


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------

def _find_orphan_jsonls(appdata_claude_dir, projects_dir):
    """Return {session_id: absolute_jsonl_path} for JSONLs with no metadata."""
    meta_cli_ids = set()
    for _acct, _org, meta_dir in _find_meta_dirs(appdata_claude_dir):
        for f in glob.glob(os.path.join(meta_dir, "local_*.json")):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    d = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            cli = d.get("cliSessionId")
            if cli:
                meta_cli_ids.add(cli)

    jsonl_index = _build_jsonl_index(projects_dir)
    return {sid: path for sid, path in jsonl_index.items() if sid not in meta_cli_ids}


def _find_meta_dir(appdata_claude_dir):
    """Return the first (and typically only) metadata directory."""
    for _acct, _org, meta_dir in _find_meta_dirs(appdata_claude_dir):
        return meta_dir
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(
        description=(
            "Synthesise metadata for JSONL transcripts with no Desktop history entry. "
            "Dry-run by default -- add --apply to write to AppData."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--diagnosis-id",
        metavar="HEX", default=None, dest="diagnosis_id",
        help="Diagnosis token from diagnose.py (required).",
    )
    ap.add_argument(
        "--force-with-diagnosis-id",
        metavar="VALUE", default=None, dest="force_diagnosis_id",
        help="Set to 'audit-only' to run dry-run without a current token.",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Write synthesised files to AppData. Default is dry-run only.",
    )
    ap.add_argument(
        "--state",
        metavar="PATH", default=None,
        help="Fixture state directory for testing.",
    )
    args = ap.parse_args()

    # Gate 3: diagnosis-token check
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
        print("State has changed since diagnose.py was last run. Re-run: python tools/diagnose.py")
        sys.exit(2)

    # Gate 5: known-do-not-run conditions
    for predicate, message in KNOWN_DO_NOT_RUN:
        try:
            if predicate(snapshot):
                print("REFUSED: " + message)
                sys.exit(3)
        except Exception:
            pass

    orphans = _find_orphan_jsonls(appdata_claude_dir, projects_dir)
    meta_dir = _find_meta_dir(appdata_claude_dir)

    used_id = args.diagnosis_id if not force_mode else "(forced-audit-only)"
    print("Orphan JSONLs (no metadata): {}".format(len(orphans)))
    print("Mode: {}".format("APPLY (writing to AppData)" if args.apply else "dry-run (writing to ./synth-out/)"))
    print("Diagnosis ID: {}".format(used_id))
    print()

    out_dir = os.path.join(TOOL_DIR, "synth-out") if not args.state else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "synth-out"
    )
    os.makedirs(out_dir, exist_ok=True)

    generated = 0
    skipped = 0
    failed = 0

    for sid in sorted(orphans):
        jsonl_path = orphans[sid]
        try:
            synth = synthesise(sid, jsonl_path)
        except Exception as e:
            print("  FAIL  {} : {}".format(sid, e))
            failed += 1
            continue

        out_name = synth["sessionId"] + ".json"
        out_path = os.path.join(out_dir, out_name)

        try:
            with open(out_path, "w", encoding="utf-8") as fh:
                json.dump(synth, fh, indent=2)
        except OSError as e:
            print("  FAIL  {} : could not write dry-run output: {}".format(sid, e))
            failed += 1
            continue

        ts_str = datetime.fromtimestamp(
            (synth["createdAt"] or 0) / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%S") if synth.get("createdAt") else "?"

        print("  SYNTH {}".format(sid))
        print("        {} | {}".format(ts_str, synth["title"][:60]))
        print("        cwd={}".format(synth["cwd"] or "(unknown)"))
        print("        model={}  turns={}".format(synth["model"], synth["completedTurns"]))
        print()

        if args.apply and meta_dir:
            apply_path = os.path.join(meta_dir, out_name)
            if os.path.exists(apply_path):
                print("  REFUSED: {} already exists -- skipping".format(apply_path))
                skipped += 1
                continue
            try:
                import shutil
                shutil.copy2(out_path, apply_path)
                print("  APPLIED -> {}".format(apply_path))
            except OSError as e:
                print("  FAIL  copy to AppData: {}".format(e))
                failed += 1
                continue

        generated += 1

    print("Generated: {}  Skipped: {}  Failed: {}".format(generated, skipped, failed))
    if not args.apply and generated:
        print("\nReview ./synth-out/ then re-run with --apply to install into AppData.")


if __name__ == "__main__":
    main()
