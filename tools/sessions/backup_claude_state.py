"""
Daily snapshot of Claude Code's three stateful data layers.

Takes a compressed backup of:
  1. Desktop metadata  %APPDATA%/Claude/claude-code-sessions/<acct>/<org>/
  2. JSONL transcripts ~/.claude/projects/
  3. FTS5 index        <TRANSCRIPT_DB> (if configured)

Each is written to a dated zip (or copied DB) under BACKUPS_ROOT. Retains
the last KEEP_DAYS daily snapshots; older ones are sent to the Recycle Bin.

Safe to run while Claude Desktop is open -- all source operations are read-only.

Files read:
  - %APPDATA%/Claude/claude-code-sessions/<account-uuid>/<org-uuid>/*
  - %USERPROFILE%/.claude/projects/**
  - TRANSCRIPT_DB path if configured

Files written:
  - <BACKUPS_ROOT>/desktop-metadata/YYYY-MM-DD.zip
  - <BACKUPS_ROOT>/jsonl-projects/YYYY-MM-DD.zip
  - <BACKUPS_ROOT>/transcript-index/YYYY-MM-DD/transcripts.db  (if configured)
  - <BACKUPS_ROOT>/backup_log/YYYY-MM-DD.log

Task Scheduler setup (run daily):
  Program  : py
  Arguments: -3 "<absolute-path-to-this-script>"
  Start In : <your project root>
  Trigger  : Daily, 06:00 AM
  Run as   : your user account

Usage:
  py -3 backup_claude_state.py
  py -3 backup_claude_state.py --dry-run

Restoring from backup — read this first:
  This script preserves original filesystem mtimes inside the zip. Claude Code's
  cleanup (cleanupOldSessionFiles) deletes JSONLs based on mtime, not message
  timestamp. If you extract this backup while cleanupPeriodDays is at its
  default (30 days), any JSONL with an mtime older than 30 days will be
  re-deleted on the next Claude Desktop launch.

  Before extracting any backup zip:
    1. Open ~/.claude/settings.json
    2. Set "cleanupPeriodDays": 36500  (approximately 100 years)
    3. Extract the zip and run synth_session_metadata.py to restore visibility
    4. Revert cleanupPeriodDays to your preferred value once sessions are recovered

  This applies to any restore method that preserves original file timestamps
  (this script, manual zip extraction, or VSS/Time Machine restore tools).
"""

import argparse
import atexit
import ctypes
import io
import os
import re
import sqlite3
import sys
import time
import zipfile
from ctypes import wintypes
from datetime import datetime
from zoneinfo import ZoneInfo

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except AttributeError:
    pass  # no buffer when stdout is None (e.g. pythonw.exe or redirected NUL)

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOL_DIR)
from lock_utils import acquire_lock, release_lock  # noqa: E402

_DIAGNOSE_DIR = os.path.dirname(TOOL_DIR)
sys.path.insert(0, _DIAGNOSE_DIR)
try:
    from diagnose import build_snapshot, _find_meta_dirs
except ImportError:
    build_snapshot = None
    _find_meta_dirs = None

# --- Configuration ---
# Edit these paths before running. BACKUPS_ROOT is the only required change.

# Where to store backup zips and logs.
BACKUPS_ROOT = os.path.join(os.path.expanduser("~"), "claude-backups")

# ~/.claude/projects/ -- where JSONL transcripts live.
PROJECTS_ROOT = os.path.join(os.path.expanduser("~"), ".claude", "projects")

# Optional: path to the FTS5 transcript index DB. Set to None to skip.
TRANSCRIPT_DB = None  # e.g. r"C:\path\to\.transcript-index\transcripts.db"

# Timezone for the weekly stamp and log timestamps.
TZ = ZoneInfo("Australia/Melbourne")

# How many daily snapshots to keep before pruning.
KEEP_DAYS = 30

# --- End configuration ---

LOG_DIR = os.path.join(BACKUPS_ROOT, "backup_log")
LOCK_FILE = os.path.join(LOG_DIR, "backup_claude_state.lock")
DEST_METADATA = os.path.join(BACKUPS_ROOT, "desktop-metadata")
DEST_JSONL = os.path.join(BACKUPS_ROOT, "jsonl-projects")
DEST_TRANSCRIPT = os.path.join(BACKUPS_ROOT, "transcript-index")
SESSIONS_BASE = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")),
    "Claude", "claude-code-sessions",
)

# Matches YYYY-MM-DD and YYYY-MM-DD.zip -- used to identify daily snapshot entries.
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(\.zip)?$")


class _SHFILEOPSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hwnd",                    wintypes.HWND),
        ("wFunc",                   wintypes.UINT),
        ("pFrom",                   wintypes.LPCWSTR),
        ("pTo",                     wintypes.LPCWSTR),
        ("fFlags",                  ctypes.c_uint16),
        ("fAnyOperationsAborted",   wintypes.BOOL),
        ("hNameMappings",           ctypes.c_void_p),
        ("lpszProgressTitle",       wintypes.LPCWSTR),
    ]


def _recycle(path):
    """Send a file or directory to the Windows Recycle Bin via SHFileOperation."""
    FO_DELETE          = 0x0003
    FOF_ALLOWUNDO      = 0x0040
    FOF_NOCONFIRMATION = 0x0010
    FOF_SILENT         = 0x0004
    op = _SHFILEOPSTRUCT()
    op.wFunc  = FO_DELETE
    op.pFrom  = os.path.abspath(path) + "\0"
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0:
        raise OSError("SHFileOperationW failed (code {}) for {}".format(rc, path))


def _discover_meta_dir():
    """Return the single account/org metadata dir; fail loudly if ambiguous."""
    if not os.path.isdir(SESSIONS_BASE):
        raise RuntimeError("claude-code-sessions dir not found: {}".format(SESSIONS_BASE))
    accounts = [e for e in os.listdir(SESSIONS_BASE)
                if os.path.isdir(os.path.join(SESSIONS_BASE, e))]
    if len(accounts) != 1:
        raise RuntimeError(
            "Expected 1 account dir under {}, found {}: {}".format(
                SESSIONS_BASE, len(accounts), accounts))
    acct_path = os.path.join(SESSIONS_BASE, accounts[0])
    orgs = [e for e in os.listdir(acct_path)
            if os.path.isdir(os.path.join(acct_path, e))]
    if len(orgs) != 1:
        raise RuntimeError(
            "Expected 1 org dir under {}, found {}: {}".format(
                acct_path, len(orgs), orgs))
    return os.path.join(acct_path, orgs[0])


def _dir_stats(src_dir):
    """Return (total_bytes, file_count) by walking src_dir."""
    total, count = 0, 0
    for root, _, files in os.walk(src_dir):
        for f in files:
            total += os.path.getsize(os.path.join(root, f))
            count += 1
    return total, count


def _backup_zip(src_dir, dest_zip, log, dry_run):
    """Zip src_dir into dest_zip (atomic write via .tmp). Returns source bytes."""
    if not os.path.isdir(src_dir):
        raise RuntimeError("Source dir not found: {}".format(src_dir))
    src_bytes, src_files = _dir_stats(src_dir)
    log("  Source : {}".format(src_dir))
    log("  Size   : {:,} bytes ({} files)".format(src_bytes, src_files))
    log("  Dest   : {}".format(dest_zip))
    if dry_run:
        log("  [DRY-RUN] Would create zip.")
        return src_bytes
    os.makedirs(os.path.dirname(dest_zip), exist_ok=True)
    tmp = dest_zip + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(src_dir):
            for fname in files:
                full    = os.path.join(root, fname)
                arcname = os.path.relpath(full, src_dir).replace("\\", "/")
                zf.write(full, arcname)
    try:
        os.replace(tmp, dest_zip)
    except PermissionError:
        # Cloud sync tools that register a Windows shell extension (OneDrive,
        # Dropbox, Sync.com) may hold dest_zip open during upload. Unlink +
        # rename works when the sync client uses FILE_SHARE_DELETE.
        os.unlink(dest_zip)  # standards: log/temp rotation
        os.replace(tmp, dest_zip)
    log("  Written: {:,} bytes (compressed)".format(os.path.getsize(dest_zip)))
    return src_bytes


def _backup_sqlite(src_db, dest_dir, log, dry_run):
    """Copy SQLite DB via sqlite3 backup API (WAL-mode safe). Returns source bytes."""
    if not os.path.isfile(src_db):
        raise RuntimeError("Source DB not found: {}".format(src_db))
    dest_path = os.path.join(dest_dir, "transcripts.db")
    src_bytes = os.path.getsize(src_db)
    log("  Source : {}".format(src_db))
    log("  Size   : {:,} bytes".format(src_bytes))
    log("  Dest   : {}".format(dest_path))
    if dry_run:
        log("  [DRY-RUN] Would copy via sqlite3 backup API.")
        return src_bytes
    os.makedirs(dest_dir, exist_ok=True)
    tmp = dest_path + ".tmp"
    src_con = sqlite3.connect("file:{}?mode=ro".format(src_db), uri=True)
    dst_con = sqlite3.connect(tmp)
    try:
        src_con.backup(dst_con)
    finally:
        dst_con.close()
        src_con.close()
    os.replace(tmp, dest_path)
    log("  Written: {:,} bytes".format(os.path.getsize(dest_path)))
    return src_bytes


def _prune_snapshots(dest_dir, log, dry_run):
    """Keep KEEP_DAYS most recent daily snapshots; recycle the rest."""
    if not os.path.isdir(dest_dir):
        return 0
    entries = sorted(e for e in os.listdir(dest_dir) if DAY_RE.match(e))
    to_prune = entries[:-KEEP_DAYS] if len(entries) > KEEP_DAYS else []
    for name in to_prune:
        full = os.path.join(dest_dir, name)
        log("  Pruning: {}".format(full))
        if not dry_run:
            _recycle(full)
    return len(to_prune)


def main():
    parser = argparse.ArgumentParser(
        description="Daily snapshot of Claude Code metadata, transcripts, and index"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Print planned actions without writing anything")
    args    = parser.parse_args()
    DRY_RUN = args.dry_run  # noqa: N806

    if DRY_RUN:
        pass  # schema probe skipped in dry-run; no state needed

    now        = datetime.now(TZ)
    week_stamp = now.strftime("%G-W%V")
    day_stamp  = now.strftime("%Y-%m-%d")
    t_start    = time.monotonic()

    os.makedirs(LOG_DIR, exist_ok=True)
    acquire_lock(LOCK_FILE, "backup_claude_state")
    atexit.register(release_lock, LOCK_FILE)

    log_path = os.path.join(LOG_DIR, "{}.log".format(day_stamp))
    log_fh   = open(log_path, "a", encoding="utf-8")

    def log(msg=""):
        line = str(msg)
        print(line)
        print(line, file=log_fh, flush=True)

    try:
        log("=== backup_claude_state  {}  day={} ===".format(now.isoformat(), day_stamp))
        if DRY_RUN:
            log("Mode: DRY-RUN -- no files will be written")
        log("")

        # Schema probe (informational; backup proceeds regardless of schema version)
        if build_snapshot is not None and not DRY_RUN:
            try:
                appdata_claude_dir = os.path.join(
                    os.environ.get("APPDATA", os.path.expanduser("~")), "Claude"
                )
                snap = build_snapshot(appdata_claude_dir, PROJECTS_ROOT)
                if snap["schema_version"] == "unrecognised":
                    log("WARNING: state schema not recognised -- backup proceeds but "
                        "run diagnose.py to investigate the unrecognised state.")
            except Exception:
                pass

        errors, total_bytes, done = 0, 0, 0

        log("--- [1/3] Desktop metadata ---")
        try:
            meta_dir = _discover_meta_dir()
            dest_zip = os.path.join(DEST_METADATA, "{}.zip".format(day_stamp))
            total_bytes += _backup_zip(meta_dir, dest_zip, log, DRY_RUN)
            pruned = _prune_snapshots(DEST_METADATA, log, DRY_RUN)
            if pruned:
                log("  Pruned {} old snapshot(s)".format(pruned))
            done += 1
        except Exception as exc:
            log("  ERROR: {}".format(exc))
            errors += 1
        log("")

        log("--- [2/3] JSONL transcripts ---")
        try:
            dest_zip = os.path.join(DEST_JSONL, "{}.zip".format(day_stamp))
            total_bytes += _backup_zip(PROJECTS_ROOT, dest_zip, log, DRY_RUN)
            pruned = _prune_snapshots(DEST_JSONL, log, DRY_RUN)
            if pruned:
                log("  Pruned {} old snapshot(s)".format(pruned))
            done += 1
        except Exception as exc:
            log("  ERROR: {}".format(exc))
            errors += 1
        log("")

        log("--- [3/3] FTS5 transcript index ---")
        if TRANSCRIPT_DB is None:
            log("  Skipped -- TRANSCRIPT_DB not configured.")
            done += 1
        else:
            try:
                dest_dir = os.path.join(DEST_TRANSCRIPT, day_stamp)
                total_bytes += _backup_sqlite(TRANSCRIPT_DB, dest_dir, log, DRY_RUN)
                pruned = _prune_snapshots(DEST_TRANSCRIPT, log, DRY_RUN)
                if pruned:
                    log("  Pruned {} old snapshot(s)".format(pruned))
                done += 1
            except Exception as exc:
                log("  ERROR: {}".format(exc))
                errors += 1
        log("")

        duration = time.monotonic() - t_start
        status   = "OK" if errors == 0 else "ERRORS={}".format(errors)
        log("=== SUMMARY ===")
        log("  Sources done   : {}/3".format(done))
        log("  Source bytes   : {:,}".format(total_bytes))
        log("  Duration       : {:.1f}s".format(duration))
        log("  Status         : {}".format(status))
        log("  Log            : {}".format(log_path))

        if errors:
            sys.exit(1)

        log("[DONE]")

    finally:
        log_fh.close()


if __name__ == "__main__":
    main()
