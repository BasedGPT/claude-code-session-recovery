"""
Recover VS Code extension session list entries that exist on disk but are
missing from the extension's workspace SQLite cache.

Background
----------
The Claude Code VS Code extension stores its session sidebar list in a
per-workspace SQLite database:

  Windows : %APPDATA%\\Code\\User\\workspaceStorage\\<WSID>\\state.vscdb
  macOS   : ~/Library/Application Support/Code/User/workspaceStorage/<WSID>/state.vscdb

The key `agentSessions.model.cache` holds a JSON array of session entries.
The extension only reads the first and last 64 KB of each transcript file
when building the list; sessions whose title entry lands in the middle of a
large file are silently excluded. This script reads full transcript files to
find titles and reinjects missing entries.

The session transcript files themselves (`~/.claude/projects/`) are not
touched. This script only modifies `state.vscdb`.

Files read:
  - %APPDATA%\\Code\\User\\workspaceStorage\\*\\state.vscdb
  - ~/.claude/projects/<slug>/*.jsonl

Files written (with --apply only):
  - %APPDATA%\\Code\\User\\workspaceStorage\\*\\state.vscdb
    (agentSessions.model.cache updated in-place)
  - ./repair-backup/vscode-cache-backup-<WSID>-<timestamp>.json
    (backup of original model.cache; directory created if absent)

Prerequisites (mapped drives / junction paths)
-----------------------------------------------
If your project is on a mapped drive (Z:\\, O:\\, etc.) or opened via a
Windows directory junction, the VS Code extension derives a different project
slug than the CLI -- its listSessions() calls fs.realpathSync, which
dereferences the drive letter to its UNC path before encoding the slug.
This means any cache you inject here will be overwritten back to [] the
next time a new session is created, because listSessions() looks in the
wrong (now-empty) slug folder and writes [] back to the cache.

Fix the path-mismatch root cause first (e.g. the SessionStart junction hook
documented in anthropics/claude-code #14088) so listSessions() resolves to
the same slug the CLI used. Once the lookup is consistent, this script's
cache rebuild will survive across new-session creation.

Usage:
    python tools/sessions/recover_vscode_sessions.py
    python tools/sessions/recover_vscode_sessions.py --apply
    python tools/sessions/recover_vscode_sessions.py --projects-dir ~/.claude/projects
    python tools/sessions/recover_vscode_sessions.py --workspace-dir /path/to/workspaceStorage
"""
import argparse
import json
import os
import pathlib
import platform
import re
import sqlite3
import subprocess
import sys
import time

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _TOOLS_DIR)
from transcript_files import (  # noqa: E402
    IncompleteTranscriptInventoryError,
    build_transcript_path_inventory,
    cache_metadata,
    require_complete_transcript_inventory,
)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# Timestamps outside this range are treated as corrupt; fall back to now_ms.
_TS_MIN_MS = 1577836800000  # 2020-01-01 UTC

# Windows process names that write state.vscdb (checked case-insensitively).
_VSCODE_PROCESSES_WIN = ["Code.exe", "Code - Insiders.exe", "VSCodium.exe"]

# ---------------------------------------------------------------------------
# Platform paths
# ---------------------------------------------------------------------------

def _default_workspace_dir():
    sys_name = platform.system()
    if sys_name == "Darwin":
        return os.path.expanduser(
            "~/Library/Application Support/Code/User/workspaceStorage"
        )
    if sys_name == "Linux":
        return os.path.expanduser("~/.config/Code/User/workspaceStorage")
    # Windows
    appdata = os.environ.get("APPDATA", os.path.expanduser("~"))
    return os.path.join(appdata, "Code", "User", "workspaceStorage")


def _default_projects_dir():
    return os.path.join(os.path.expanduser("~"), ".claude", "projects")


# ---------------------------------------------------------------------------
# VS Code process check
# ---------------------------------------------------------------------------

def _vscode_running():
    """Return True if any known VS Code variant appears to be running."""
    sys_name = platform.system()
    if sys_name == "Windows":
        try:
            result = subprocess.run(
                ["tasklist", "/FO", "CSV"],
                capture_output=True, text=True, timeout=10, check=False,
            )
            output_lower = result.stdout.lower()
            # CSV format wraps each name in quotes; match on the quoted name so
            # we don't false-positive on path fragments containing these strings.
            return any(
                f'"{proc.lower()}"' in output_lower
                for proc in _VSCODE_PROCESSES_WIN
            )
        except Exception:
            return False
    else:
        # macOS / Linux: look for VS Code / VSCodium Electron helper processes.
        for pattern in ("Code Helper", "Visual Studio Code", "VSCodium"):
            try:
                result = subprocess.run(
                    ["pgrep", "-f", pattern],
                    capture_output=True, timeout=5, check=False,
                )
                if result.returncode == 0:
                    return True
            except Exception:
                pass
        return False


# ---------------------------------------------------------------------------
# Workspace DB discovery
# ---------------------------------------------------------------------------

def _find_claude_dbs(workspace_dir):
    """Find state.vscdb files that contain agentSessions.model.cache entries.

    Returns list of (db_path, wsid, cache_list) tuples.
    """
    results = []
    if not os.path.isdir(workspace_dir):
        return results
    for wsid in sorted(os.listdir(workspace_dir)):
        db_path = os.path.join(workspace_dir, wsid, "state.vscdb")
        if not os.path.isfile(db_path):
            continue
        # Use pathlib.as_uri() so reserved URI characters (e.g. '#') in the
        # path are percent-encoded; bare f"file:{db_path}" breaks on such paths.
        db_uri = pathlib.Path(db_path).resolve().as_uri() + "?mode=ro"
        try:
            conn = sqlite3.connect(db_uri, uri=True)
            try:
                cur = conn.execute(
                    "SELECT value FROM ItemTable WHERE key='agentSessions.model.cache'"
                )
                row = cur.fetchone()
            finally:
                conn.close()
        except sqlite3.OperationalError:
            continue
        except Exception:
            continue
        if not row or not row[0]:
            continue
        try:
            cache = json.loads(row[0])
        except (ValueError, TypeError):
            continue
        if not isinstance(cache, list):
            continue
        # Only include DBs that have at least one Claude Code entry
        has_cc = any(
            isinstance(e, dict) and "claude-code:/" in e.get("resource", "")
            for e in cache
        )
        if has_cc:
            results.append((db_path, wsid, cache))
    return results


def _cached_uuids(dbs):
    """Return the set of session UUIDs across all discovered DBs."""
    uuids = set()
    for _db_path, _wsid, cache in dbs:
        for entry in cache:
            resource = entry.get("resource", "") if isinstance(entry, dict) else ""
            if resource.startswith("claude-code:/"):
                sid = resource[len("claude-code:/"):]
                if _UUID_RE.match(sid):
                    uuids.add(sid)
    return uuids


def _make_cache_entry(sid, title, created_ms, last_message_ms, now_ms):
    label = title or sid
    ts_max = now_ms + 24 * 60 * 60 * 1000  # accept up to 24 h in the future
    if created_ms is not None and _TS_MIN_MS <= created_ms <= ts_max:
        timing_created = created_ms
    else:
        timing_created = now_ms
    # Use the last real conversation timestamp for sort order, not now_ms.
    # Trailer records (ai-title, last-prompt) carry no timestamp so they don't
    # advance last_message_ms -- recovered sessions sort by actual recency.
    if last_message_ms is not None and _TS_MIN_MS <= last_message_ms <= ts_max:
        timing_last = last_message_ms
    else:
        timing_last = timing_created
    return {
        "providerType": "claude-code",
        "providerLabel": "Claude",
        "resource": f"claude-code:/{sid}",
        "icon": "claude",
        "label": label,
        "tooltip": f"Claude Code session: {label}",
        "status": 1,
        "timing": {
            "created": timing_created,
            "lastRequestEnded": timing_last,
        },
    }


# ---------------------------------------------------------------------------
# Backup helpers
# ---------------------------------------------------------------------------

def _backup_path(wsid, timestamp_s):
    backup_dir = os.path.join(os.getcwd(), "repair-backup")
    os.makedirs(backup_dir, exist_ok=True)
    fname = f"vscode-cache-backup-{wsid[:8]}-{timestamp_s}.json"
    return os.path.join(backup_dir, fname)


def _write_backup(backup_path, wsid, db_path, cache):
    payload = {"wsid": wsid, "db_path": db_path, "original_cache": cache}
    with open(backup_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def _apply_recovery(db_path, new_entries):
    """Merge new_entries into the live model.cache and write back.

    Opens a BEGIN IMMEDIATE transaction so the read-modify-write is atomic
    against any concurrent writer. Re-reads the current cache rather than
    using the scan-time snapshot, so entries added by VS Code between scan
    and apply are preserved.

    Returns (pre_write_cache, added_count, total_count).
    Raises RuntimeError if the key is missing or the UPDATE hits ≠1 row.
    """
    conn = sqlite3.connect(db_path, isolation_level=None)  # manual transaction
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT value FROM ItemTable WHERE key='agentSessions.model.cache'"
        ).fetchone()
        if not row or not row[0]:
            conn.execute("ROLLBACK")
            raise RuntimeError(
                f"agentSessions.model.cache not found in {db_path!r} — "
                "key may have been deleted since the scan"
            )
        try:
            current_cache = json.loads(row[0])
        except (ValueError, TypeError) as exc:
            conn.execute("ROLLBACK")
            raise RuntimeError(
                f"Cache JSON unreadable in {db_path!r}: {exc}"
            ) from exc

        # Build the UUID set already present in the live cache.
        existing_uuids = {
            e.get("resource", "")[len("claude-code:/"):].lower()
            for e in current_cache
            if isinstance(e, dict)
            and e.get("resource", "").startswith("claude-code:/")
        }
        to_add = [
            e for e in new_entries
            if e.get("resource", "")[len("claude-code:/"):].lower()
            not in existing_uuids
        ]
        merged = current_cache + to_add

        cur = conn.execute(
            "UPDATE ItemTable SET value=? WHERE key='agentSessions.model.cache'",
            (json.dumps(merged),),
        )
        if cur.rowcount != 1:
            conn.execute("ROLLBACK")
            raise RuntimeError(
                f"UPDATE matched {cur.rowcount} row(s) in {db_path!r} — expected 1"
            )
        conn.execute("COMMIT")
        return current_cache, len(to_add), len(merged)
    except RuntimeError:
        raise
    except Exception as exc:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise RuntimeError(f"Failed to apply recovery to {db_path!r}: {exc}") from exc
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(
        description=(
            "Recover VS Code extension session list entries that exist on disk "
            "but are missing from the extension's workspace SQLite cache."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "VS Code must be fully closed before running with --apply.\n"
            "Run without --apply first to see what would change."
        ),
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Write the recovered entries into state.vscdb. Default is dry-run.",
    )
    ap.add_argument(
        "--projects-dir",
        default=None,
        metavar="PATH",
        help="Override path to ~/.claude/projects/",
    )
    ap.add_argument(
        "--workspace-dir",
        default=None,
        metavar="PATH",
        help="Override path to VS Code workspaceStorage directory.",
    )
    args = ap.parse_args()

    workspace_dir = args.workspace_dir or _default_workspace_dir()
    projects_dir = args.projects_dir or _default_projects_dir()

    print("VS Code Session Recovery")
    print("-" * 60)
    print(f"Workspace storage : {workspace_dir}")
    print(f"Projects dir      : {projects_dir}")
    print()

    # --- Discover Claude-aware workspace DBs ---
    print("Scanning workspace databases...")
    dbs = _find_claude_dbs(workspace_dir)
    if not dbs:
        print(
            "No VS Code workspace databases with Claude Code session entries found.\n"
            "If you have used Claude Code in VS Code, ensure the workspace storage\n"
            f"directory exists at: {workspace_dir}"
        )
        return

    print(f"Found {len(dbs)} Claude Code workspace database(s):")
    for db_path, wsid, cache in dbs:
        cc_count = sum(
            1 for e in cache
            if isinstance(e, dict) and "claude-code:/" in e.get("resource", "")
        )
        print(f"  [{wsid[:8]}...] {cc_count} session(s) in cache — {db_path}")
    print()

    # --- Build the set of UUIDs already in any cache ---
    all_cached = _cached_uuids(dbs)

    # --- Scan disk for all JSONL session files ---
    print("Scanning transcript files on disk...")
    inventory = build_transcript_path_inventory(
        projects_dir, lambda session_id: bool(_UUID_RE.match(session_id))
    )
    try:
        require_complete_transcript_inventory(inventory)
    except IncompleteTranscriptInventoryError as exc:
        print(
            "REFUSED: Transcript inventory is partial; "
            "no UUID-keyed cache entries were selected."
        )
        print(f"Inventory errors: {exc}")
        return 3
    disk_index = {
        session_id: paths[0]
        for session_id, paths in inventory.by_session_id.items()
        if len(paths) == 1
    }
    ambiguous_ids = sorted(
        session_id for session_id, paths in inventory.by_session_id.items()
        if len(paths) > 1
    )
    print(f"Found {inventory.physical_count} transcript file(s) on disk.")
    if ambiguous_ids:
        print(
            f"Skipped {len(ambiguous_ids)} ambiguous session ID(s); "
            "no UUID-keyed cache entry will be chosen for them."
        )
    print()

    # --- Find missing entries ---
    missing_uuids = sorted(set(disk_index.keys()) - all_cached)
    if not missing_uuids:
        print("All disk sessions are already present in the VS Code session cache.")
        print("Nothing to recover.")
        return

    print(f"{len(missing_uuids)} session(s) on disk are missing from the VS Code cache:")
    now_ms = int(time.time() * 1000)
    new_entries = []
    for sid in missing_uuids:
        jsonl_path = disk_index[sid]
        title, created_ms, last_message_ms = cache_metadata(jsonl_path)
        entry = _make_cache_entry(sid, title, created_ms, last_message_ms, now_ms)
        new_entries.append(entry)
        ts_display = ""
        if created_ms:
            import datetime
            ts_display = datetime.datetime.fromtimestamp(created_ms / 1000).strftime(
                "%Y-%m-%d %H:%M"
            )
        label = title or "(no title found)"
        print(f"  {sid[:12]}...  {ts_display:>16}  {label[:55]}")
    print()

    if not args.apply:
        print(
            f"DRY RUN — {len(new_entries)} entry/entries would be injected into "
            f"{len(dbs)} database(s)."
        )
        print("Run with --apply to write changes.")
        print()
        print(
            "NOTE: Close VS Code fully before running with --apply.\n"
            "      VS Code overwrites state.vscdb on startup and will discard\n"
            "      changes written while it is open."
        )
        return

    # --- Pre-apply checks ---
    if _vscode_running():
        print(
            "ERROR: VS Code appears to be running.\n"
            "Close VS Code fully before running with --apply — it overwrites\n"
            "state.vscdb on startup and will discard any changes written while open."
        )
        sys.exit(1)

    # --- Apply to each DB ---
    timestamp_s = str(int(time.time()))
    for db_path, wsid, _scan_cache in dbs:
        try:
            pre_write_cache, added, total = _apply_recovery(db_path, new_entries)
        except RuntimeError as exc:
            print(f"ERROR [{wsid[:8]}...]: {exc}")
            continue

        # Backup captures the pre-write state returned from inside the transaction.
        backup_file = _backup_path(wsid, timestamp_s)
        _write_backup(backup_file, wsid, db_path, pre_write_cache)
        print(f"Backup written : {backup_file}")
        if added < len(new_entries):
            skipped = len(new_entries) - added
            print(
                f"Injected {added} entry/entries into [{wsid[:8]}...] — "
                f"{skipped} already present — {total} total in cache."
            )
        else:
            print(
                f"Injected {added} entry/entries into [{wsid[:8]}...] — "
                f"{total} total in cache."
            )

    print()
    print("Done. Restart VS Code and check Local -> Session History.")
    print()
    print("Rollback: replace agentSessions.model.cache in state.vscdb with the")
    print("original_cache value from the backup JSON file(s) above.")
    print()
    print(
        "NOTE: If sessions disappear again after creating a new chat, the"
        " VS Code extension is overwriting the cache via listSessions(),"
        " which dereferences your mapped drive to its UNC path and finds"
        " an empty slug folder. Fix the path-mismatch root cause (e.g. the"
        " SessionStart junction hook in anthropics/claude-code #14088) so"
        " listSessions() resolves to the correct slug before relying on"
        " this rebuild."
    )


if __name__ == "__main__":
    sys.exit(main() or 0)
