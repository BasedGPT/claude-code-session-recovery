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
import sys
import uuid
from datetime import datetime, timezone

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _TOOLS_DIR)
try:
    from session_state import default_claude_paths, find_metadata_directories
    from mutator_safety import (
        atomic_copy_file, atomic_write_json, current_snapshot_and_diagnosis_id,
        diagnosis_mode, resolve_state_paths,
    )
    from transcript_files import build_transcript_index, iso_timestamp_ms, synthesis_summary
except ImportError as exc:
    print("ERROR: cannot import from diagnose.py: {}".format(exc))
    print("Run from the repo root: python tools/sessions/synth_session_metadata.py")
    sys.exit(1)

# --- Configuration ---

APPDATA_CLAUDE_DIR, PROJECTS_DIR = default_claude_paths()

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
    summary = synthesis_summary(jsonl_path)
    new_session_id = "local_{}".format(uuid.uuid4())
    cwd = summary["cwd"] or ""

    return {
        "sessionId": new_session_id,
        "cliSessionId": cli_session_id,
        "cwd": cwd,
        "originCwd": cwd,
        "createdAt": iso_timestamp_ms(summary["first_ts"]) if summary["first_ts"] else None,
        "lastActivityAt": iso_timestamp_ms(summary["last_ts"]) if summary["last_ts"] else None,
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
    for _acct, _org, meta_dir in find_metadata_directories(appdata_claude_dir):
        for f in glob.glob(os.path.join(meta_dir, "local_*.json")):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    d = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            cli = d.get("cliSessionId")
            if cli:
                meta_cli_ids.add(cli)

    jsonl_index = build_transcript_index(projects_dir)
    return {sid: path for sid, path in jsonl_index.items() if sid not in meta_cli_ids}


def _find_meta_dir(appdata_claude_dir):
    """Return the first (and typically only) metadata directory."""
    for _acct, _org, meta_dir in find_metadata_directories(appdata_claude_dir):
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
    force_mode, invocation_error = diagnosis_mode(
        args.diagnosis_id, args.force_diagnosis_id, args.apply,
    )
    if invocation_error == "missing":
        print("ERROR: --diagnosis-id required.")
        print("Run: python tools/diagnose.py")
        sys.exit(2)
    if invocation_error == "force_apply":
        print("ERROR: --apply cannot be combined with --force-with-diagnosis-id=audit-only.")
        sys.exit(2)

    # Resolve directories
    appdata_claude_dir, projects_dir = resolve_state_paths(
        args.state, APPDATA_CLAUDE_DIR, PROJECTS_DIR,
    )

    snapshot, current_id = current_snapshot_and_diagnosis_id(
        appdata_claude_dir, projects_dir, fixture_mode=(args.state is not None),
    )

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
            atomic_write_json(out_path, synth)
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
                atomic_copy_file(out_path, apply_path)
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
