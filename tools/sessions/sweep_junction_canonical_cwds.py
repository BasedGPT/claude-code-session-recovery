"""
Classify all worktree-session metadata by cwd type (junction vs canonical) and
verify that each session's JSONL is at the expected slug.

Useful after a junction removal or path rename to identify which worktree
sessions may have stale cwd references. Each session whose cwd contains a
worktrees/ path is classified into one of these cwd-type buckets:

  junction   -- cwd path is a Windows reparse-point alias (junction-vs-realpath
                 mismatch; JSONL may be at the canonical slug instead)
  canonical  -- cwd path is the real on-disk path (expected state)
  other      -- cwd path doesn't exist on disk or can't be classified

JSONL location for each session is checked against the expected slug derived
from the cwd field. Sessions where the JSONL is at a different slug are flagged.

Files read:
  - %APPDATA%\\Claude\\claude-code-sessions\\<account-uuid>\\<org-uuid>\\local_*.json
  - %USERPROFILE%\\.claude\\projects\\<slug>\\*.jsonl

Files written:
  - Nothing. Read-only.

Exit code:
  0  script ran successfully
  2  state schema not recognised

Usage:
  python tools/sessions/sweep_junction_canonical_cwds.py
  python tools/sessions/sweep_junction_canonical_cwds.py --state <fixture-state-path>
"""
import argparse
import json
import os
import sys

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _TOOLS_DIR)
try:
    from session_state import (
        build_snapshot,
        classify_cwd,
        default_claude_paths,
        find_metadata_directories,
        slug_encode,
    )
    from transcript_files import build_transcript_index
except ImportError as exc:
    print("ERROR: cannot import from diagnose.py: {}".format(exc))
    print("Run from the repo root: python tools/sessions/sweep_junction_canonical_cwds.py")
    sys.exit(1)

# --- Configuration ---
APPDATA_CLAUDE_DIR, PROJECTS_DIR = default_claude_paths()


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
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

    jsonl_index = build_transcript_index(projects_dir)

    counts = {"junction": 0, "canonical": 0, "other": 0}
    jsonl_at_expected = 0
    jsonl_at_other_slug = 0
    jsonl_absent = 0
    no_cli = 0
    null_cwd = 0
    parse_errors = 0
    issues = []

    for _acct, _org, meta_dir in find_metadata_directories(appdata_claude_dir):
        for fname in sorted(os.listdir(meta_dir)):
            if not (fname.startswith("local_") and fname.endswith(".json")):
                continue
            fpath = os.path.join(meta_dir, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as fh:
                    d = json.load(fh)
            except Exception:
                parse_errors += 1
                continue

            cwd = d.get("cwd") or ""
            if "worktrees" not in cwd.lower():
                continue

            if not cwd:
                null_cwd += 1
                continue

            cli = d.get("cliSessionId") or ""
            title = (d.get("title") or "")[:50]
            cwd_kind = classify_cwd(cwd)
            counts[cwd_kind] = counts.get(cwd_kind, 0) + 1

            if cwd_kind == "junction":
                issues.append(("JUNCTION_CWD", fname[:40], cwd[:60], title))

            if not cli:
                no_cli += 1
                continue

            expected_slug = slug_encode(cwd)
            if cli in jsonl_index:
                actual_slug = os.path.basename(os.path.dirname(jsonl_index[cli]))
                if actual_slug == expected_slug:
                    jsonl_at_expected += 1
                else:
                    jsonl_at_other_slug += 1
                    issues.append(("JSONL_AT_OTHER_SLUG", fname[:40],
                                   "expected={} actual={}".format(expected_slug[:30], actual_slug[:30]),
                                   title))
            else:
                jsonl_absent += 1
                issues.append(("JSONL_ABSENT", fname[:40], cwd[:60], title))

    total = sum(counts.values())
    print("=== Worktree-cwd session sweep ===")
    print("  Total sessions with worktrees/ in cwd: {}".format(total))
    print("  Null or empty cwd:                     {}".format(null_cwd))
    print("  Junction-prefixed cwd:                 {}".format(counts.get("junction", 0)))
    print("  Canonical-prefixed cwd:                {}".format(counts.get("canonical", 0)))
    print("  Other / unclassifiable cwd:            {}".format(counts.get("other", 0)))
    print("  No cliSessionId set:                   {}".format(no_cli))
    print("  JSONL at expected slug:                {}".format(jsonl_at_expected))
    print("  JSONL at a different slug:             {}".format(jsonl_at_other_slug))
    print("  JSONL absent:                          {}".format(jsonl_absent))
    print("  Metadata parse errors:                 {}".format(parse_errors))
    print()

    if issues:
        print("=== Issues (first 30) ===")
        for kind, fname, detail, title in issues[:30]:
            print("  {:<26} {:<40} {:<40} {}".format(kind, fname, str(detail)[:40], title))
    else:
        print("No issues found.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
