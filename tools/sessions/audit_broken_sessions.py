"""
Diagnose Claude Desktop sessions by broken-state bucket.

Complement to diagnose.py for power users who want per-session detail beyond
what the summary counts show. Categorises every local_*.json in the AppData
session-metadata directory into one of seven buckets and reports counts plus
details for the broken ones.

Bucket order (evaluated in priority order):
  parse_error            -- JSON failed to parse
  archived_no_cli        -- isArchived=True AND cliSessionId absent (would
                            blank-pane if unarchived; surfaced separately
                            so the count of truly-broken-but-hidden cases
                            does not vanish into the archived bucket)
  archived               -- isArchived=True (Desktop hides these from history)
  no_cli_session_id      -- cliSessionId absent or empty
  cli_at_unexpected_slug -- cliSessionId set + JSONL exists, but not at the slug
                            encoded from the metadata cwd field
  cli_but_no_jsonl       -- cliSessionId set, JSONL absent everywhere
  healthy                -- JSONL present at the expected slug

Files read:
  - %APPDATA%\\Claude\\claude-code-sessions\\<account-uuid>\\<org-uuid>\\local_*.json
  - %USERPROFILE%\\.claude\\projects\\<slug>\\*.jsonl

Files written:
  - Nothing. Read-only.

Exit code:
  0  script ran successfully (including when broken sessions are found)
  2  state schema not recognised

Usage:
  python tools/sessions/audit_broken_sessions.py
  python tools/sessions/audit_broken_sessions.py --quiet
  python tools/sessions/audit_broken_sessions.py --limit 50
  python tools/sessions/audit_broken_sessions.py --state <fixture-state-path>
"""
import argparse
import datetime
import json
import os
import sys

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _TOOLS_DIR)
try:
    from session_state import build_snapshot, find_metadata_directories, slug_encode
    from transcript_files import build_transcript_index
except ImportError as exc:
    print("ERROR: cannot import from diagnose.py: {}".format(exc))
    print("Run from the repo root: python tools/sessions/audit_broken_sessions.py")
    sys.exit(1)

# --- Configuration ---
APPDATA_CLAUDE_DIR = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")), "Claude"
)
PROJECTS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "projects")


def _classify(meta, jsonl_index):
    """Return (bucket, info) for a single metadata dict."""
    info = {
        "title": meta.get("title") or "<no title>",
        "lastActivityAt": meta.get("lastActivityAt") or 0,
        "cwd": meta.get("cwd") or "",
        "cliSessionId": meta.get("cliSessionId"),
    }

    if meta.get("isArchived"):
        if not meta.get("cliSessionId"):
            return "archived_no_cli", info
        return "archived", info
    if not meta.get("cliSessionId"):
        return "no_cli_session_id", info

    cli = meta["cliSessionId"]
    if cli not in jsonl_index:
        return "cli_but_no_jsonl", info

    expected_slug = slug_encode(info["cwd"])
    actual_slug = os.path.basename(os.path.dirname(jsonl_index[cli]))
    info["expected_slug"] = expected_slug

    if actual_slug == expected_slug:
        return "healthy", info

    info["actual_slug"] = actual_slug
    return "cli_at_unexpected_slug", info


def _audit(appdata_claude_dir, projects_dir):
    """Walk metadata dir, return {bucket: [(filename, info), ...]}."""
    results = {
        "parse_error":            [],
        "archived_no_cli":        [],
        "archived":               [],
        "no_cli_session_id":      [],
        "cli_at_unexpected_slug": [],
        "cli_but_no_jsonl":       [],
        "healthy":                [],
    }
    jsonl_index = build_transcript_index(projects_dir)

    for _acct, _org, meta_dir in find_metadata_directories(appdata_claude_dir):
        for fname in sorted(os.listdir(meta_dir)):
            if not (fname.startswith("local_") and fname.endswith(".json")):
                continue
            fpath = os.path.join(meta_dir, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as fh:
                    meta = json.load(fh)
            except Exception as e:
                results["parse_error"].append((fname, {"error": str(e)}))
                continue
            bucket, info = _classify(meta, jsonl_index)
            results[bucket].append((fname, info))

    return results


def _fmt_ts(ms):
    if not ms:
        return "<no lastActivityAt>"
    return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def _print_report(results, quiet, limit):
    print("=== SUMMARY ===")
    for k, lst in results.items():
        print("  {:<25} {}".format(k, len(lst)))
    print()
    if quiet:
        return

    for bucket, header in (
        ("archived_no_cli",
         "archived_no_cli -- isArchived AND cliSessionId absent (would blank-pane if unarchived)"),
        ("no_cli_session_id",
         "no_cli_session_id -- cliSessionId absent (Desktop cannot resume)"),
        ("cli_at_unexpected_slug",
         "cli_at_unexpected_slug -- JSONL at a different slug than cwd encodes to"),
        ("cli_but_no_jsonl",
         "cli_but_no_jsonl -- cliSessionId set but JSONL absent"),
    ):
        rows = sorted(results[bucket], key=lambda r: -r[1].get("lastActivityAt", 0))
        if not rows:
            continue
        print("=== {} ({}) ===".format(header, len(rows)))
        for name, info in rows[:limit]:
            print("  [{}] {}".format(_fmt_ts(info.get("lastActivityAt", 0)), name))
            print("    title: {}".format(info["title"][:80]))
            print("    cwd:   {}".format(info["cwd"]))
            if "expected_slug" in info:
                print("    expected slug: {}".format(info["expected_slug"]))
            if "actual_slug" in info:
                print("    actual slug:   {}".format(info["actual_slug"]))
        if len(rows) > limit:
            print("  ... and {} more".format(len(rows) - limit))
        print()


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    ap.add_argument("--quiet", action="store_true", help="Summary only; skip per-row detail.")
    ap.add_argument("--limit", type=int, default=30, help="Max rows per bucket (default: 30).")
    ap.add_argument("--state", metavar="PATH", default=None,
                    help="Fixture state directory for testing.")
    args = ap.parse_args()

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
    if snapshot["schema_version"] == "unrecognised":
        print("ERROR: state schema not recognised. Run diagnose.py and report the "
              "unsupported state to the maintainer.", file=sys.stderr)
        return 2

    results = _audit(appdata_claude_dir, projects_dir)
    _print_report(results, args.quiet, args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
