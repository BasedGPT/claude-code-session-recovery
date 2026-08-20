"""
Daily snapshot of Claude Code's three stateful data layers.

Takes a compressed backup of:
  1. Desktop metadata  the platform-specific Claude claude-code-sessions/<acct>/<org>/
  2. JSONL transcripts ~/.claude/projects/
  3. FTS5 index        <TRANSCRIPT_DB> (if configured)

Each is written to a dated zip (or copied DB) under BACKUPS_ROOT. Retains
the last KEEP_DAYS daily snapshots; older ones are sent to the system Trash or
Recycle Bin.

Safe to run while Claude Desktop is open -- all source operations are read-only.

Files read:
  - Claude Desktop's platform-specific claude-code-sessions/<account-uuid>/<org-uuid>/*
  - ~/.claude/projects/**
  - TRANSCRIPT_DB path if configured

Files written:
  - <BACKUPS_ROOT>/desktop-metadata/YYYY-MM-DD.zip
  - <BACKUPS_ROOT>/jsonl-projects/YYYY-MM-DD.zip
  - <BACKUPS_ROOT>/transcript-index/YYYY-MM-DD/transcripts.db  (if configured)
  - <BACKUPS_ROOT>/backup_log/YYYY-MM-DD.log

Zip publication:
  - manifest.json is a reserved archive control path.
  - completed temp zips are CRC-, schema-, size-, and SHA-256-verified.
  - zero metadata pairs or publication failure leaves a prior final untouched.

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
import hashlib
import json
import os
import platform
import re
import sqlite3
import shutil
import sys
import time
import zipfile
from ctypes import wintypes
from datetime import datetime
from zoneinfo import ZoneInfo

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass  # no buffer when stdout is None (e.g. pythonw.exe or redirected NUL)

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOL_DIR)
from lock_utils import acquire_lock, release_lock  # noqa: E402

_DIAGNOSE_DIR = os.path.dirname(TOOL_DIR)
sys.path.insert(0, _DIAGNOSE_DIR)
try:
    from session_state import build_snapshot, default_claude_appdata_dir
except ImportError:
    build_snapshot = None

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

# Versioned manifest format for newly-created backup zips.  This archive
# evidence is intentionally independent of the diagnosis snapshot.
BACKUP_LAYOUT_VERSION = 2
HASH_CHUNK_SIZE = 1024 * 1024
MANIFEST_ARCHIVE_PATH = "manifest.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# --- End configuration ---

LOG_DIR = os.path.join(BACKUPS_ROOT, "backup_log")
LOCK_FILE = os.path.join(LOG_DIR, "backup_claude_state.lock")
DEST_METADATA = os.path.join(BACKUPS_ROOT, "desktop-metadata")
DEST_JSONL = os.path.join(BACKUPS_ROOT, "jsonl-projects")
DEST_TRANSCRIPT = os.path.join(BACKUPS_ROOT, "transcript-index")
SESSIONS_BASE = os.path.join(
    default_claude_appdata_dir(), "claude-code-sessions"
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
    """Move a pruned snapshot to the platform's recoverable trash."""
    if platform.system() == "Darwin":
        trash_dir = os.path.expanduser("~/.Trash")
        os.makedirs(trash_dir, exist_ok=True)
        name = os.path.basename(os.path.abspath(path))
        destination = os.path.join(trash_dir, name)
        counter = 1
        while os.path.exists(destination):
            stem, extension = os.path.splitext(name)
            destination = os.path.join(
                trash_dir, "{}-{}{}".format(stem, counter, extension)
            )
            counter += 1
        shutil.move(path, destination)
        return
    if platform.system() != "Windows":
        raise OSError("recoverable snapshot pruning is supported on Windows and macOS only")

    # Windows: send a file or directory to the Recycle Bin via SHFileOperation.
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


def _discover_meta_pairs():
    """Return every account/org metadata pair in deterministic order.

    Logout/login can leave more than one pair under
    ``claude-code-sessions``. A backup must preserve all of them rather than
    treating that state as an error. The filesystem path is retained for the
    caller; it is not written to the portable manifest.
    """
    if not os.path.isdir(SESSIONS_BASE):
        raise RuntimeError("claude-code-sessions dir not found: {}".format(SESSIONS_BASE))

    pairs = []
    for account_uuid in sorted(os.listdir(SESSIONS_BASE)):
        account_path = os.path.join(SESSIONS_BASE, account_uuid)
        if not os.path.isdir(account_path):
            continue
        for organisation_uuid in sorted(os.listdir(account_path)):
            metadata_dir = os.path.join(account_path, organisation_uuid)
            if not os.path.isdir(metadata_dir):
                continue
            pairs.append({
                "account_uuid": account_uuid,
                "organisation_uuid": organisation_uuid,
                "path": metadata_dir,
            })
    return pairs


def _discover_meta_dir():
    """Compatibility helper for callers that require exactly one pair."""
    pairs = _discover_meta_pairs()
    if len(pairs) != 1:
        raise RuntimeError(
            "Expected exactly one account/org pair under {}, found {}".format(
                SESSIONS_BASE, len(pairs)
            )
        )
    return pairs[0]["path"]


def _iter_source_files(src_dir):
    """Yield source files in stable order for hashing and archive creation."""
    for root, dirs, files in os.walk(src_dir):
        dirs.sort()
        for fname in sorted(files):
            full = os.path.join(root, fname)
            if os.path.isfile(full):
                yield full


def _dir_stats(src_dir):
    """Return (total_bytes, file_count) by walking src_dir."""
    total, count = 0, 0
    for full in _iter_source_files(src_dir):
        total += os.path.getsize(full)
        count += 1
    return total, count


def _manifest_pairs(src_dir, pairs):
    """Return portable manifest records for discovered metadata pairs."""
    records = []
    for pair in pairs or []:
        archive_root = os.path.relpath(pair["path"], src_dir).replace("\\", "/")
        if archive_root == "." or archive_root.startswith("../"):
            raise RuntimeError("metadata pair is outside backup root: {}".format(pair["path"]))
        records.append({
            "account_uuid": pair["account_uuid"],
            "organisation_uuid": pair["organisation_uuid"],
            "archive_root": archive_root,
            "file_count": 0,
            "total_bytes": 0,
        })
    return records


def _write_hashed_zip_entry(zip_file, source_path, archive_path):
    """Copy one file into a zip while returning its byte count and SHA-256."""
    info = zipfile.ZipInfo.from_file(source_path, archive_path)
    info.compress_type = zipfile.ZIP_DEFLATED
    digest = hashlib.sha256()
    byte_count = 0
    with open(source_path, "rb") as source, zip_file.open(info, "w") as target:
        while True:
            chunk = source.read(HASH_CHUNK_SIZE)
            if not chunk:
                break
            target.write(chunk)
            digest.update(chunk)
            byte_count += len(chunk)
    return byte_count, digest.hexdigest()


def _verify_backup_zip(archive_path):
    """Validate CRCs, manifest schema, entries, sizes, and SHA-256 hashes."""
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise RuntimeError("duplicate archive entry")
            if MANIFEST_ARCHIVE_PATH not in names:
                raise RuntimeError("manifest is missing")
            corrupt_entry = archive.testzip()
            if corrupt_entry is not None:
                raise RuntimeError("CRC failure for {}".format(corrupt_entry))

            try:
                manifest = json.loads(archive.read(MANIFEST_ARCHIVE_PATH))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise RuntimeError("manifest is not valid JSON") from exc
            if not isinstance(manifest, dict):
                raise RuntimeError("manifest root must be an object")
            if manifest.get("layout_version") != BACKUP_LAYOUT_VERSION:
                raise RuntimeError("unsupported manifest layout_version")
            if not isinstance(manifest.get("source_layer"), str):
                raise RuntimeError("manifest source_layer must be a string")
            pairs = manifest.get("pairs")
            files = manifest.get("files")
            if not isinstance(pairs, list) or not isinstance(files, list):
                raise RuntimeError("manifest pairs and files must be lists")

            pair_records = {}
            archive_roots = set()
            for pair in pairs:
                if not isinstance(pair, dict):
                    raise RuntimeError("manifest pair must be an object")
                required = (
                    "account_uuid", "organisation_uuid", "archive_root",
                    "file_count", "total_bytes",
                )
                if any(key not in pair for key in required):
                    raise RuntimeError("manifest pair is missing required fields")
                if not all(isinstance(pair[key], str) for key in required[:3]):
                    raise RuntimeError("manifest pair identity fields must be strings")
                if (
                    not isinstance(pair["file_count"], int)
                    or pair["file_count"] < 0
                    or not isinstance(pair["total_bytes"], int)
                    or pair["total_bytes"] < 0
                ):
                    raise RuntimeError("manifest pair counts must be non-negative integers")
                archive_root = pair["archive_root"].strip("/")
                if not archive_root or archive_root != pair["archive_root"] or ".." in archive_root.split("/"):
                    raise RuntimeError("manifest pair archive_root is unsafe")
                identity = (pair["account_uuid"], pair["organisation_uuid"])
                if identity in pair_records or archive_root in archive_roots:
                    raise RuntimeError("duplicate manifest pair")
                pair_records[identity] = pair
                archive_roots.add(archive_root)

            declared_files = {}
            pair_totals = {
                identity: {"file_count": 0, "total_bytes": 0}
                for identity in pair_records
            }
            for file_record in files:
                if not isinstance(file_record, dict):
                    raise RuntimeError("manifest file must be an object")
                archive_name = file_record.get("archive_path")
                size = file_record.get("size")
                digest = file_record.get("sha256")
                if not isinstance(archive_name, str) or not archive_name:
                    raise RuntimeError("manifest archive_path must be a non-empty string")
                if archive_name == MANIFEST_ARCHIVE_PATH or archive_name in declared_files:
                    raise RuntimeError("reserved or duplicate manifest archive_path")
                if archive_name.startswith("/") or ".." in archive_name.split("/"):
                    raise RuntimeError("manifest archive_path is unsafe")
                if not isinstance(size, int) or size < 0:
                    raise RuntimeError("manifest file size must be a non-negative integer")
                if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
                    raise RuntimeError("manifest file sha256 is invalid")

                if manifest["source_layer"] == "desktop-metadata":
                    account_uuid = file_record.get("account_uuid")
                    organisation_uuid = file_record.get("organisation_uuid")
                    if account_uuid is not None or organisation_uuid is not None:
                        identity = (account_uuid, organisation_uuid)
                        if identity not in pair_records:
                            raise RuntimeError("desktop metadata file has an unknown pair")
                        prefix = pair_records[identity]["archive_root"].rstrip("/") + "/"
                        if not archive_name.startswith(prefix):
                            raise RuntimeError("desktop metadata file is outside its pair root")
                        pair_totals[identity]["file_count"] += 1
                        pair_totals[identity]["total_bytes"] += size
                declared_files[archive_name] = file_record

            expected_names = set(declared_files) | {MANIFEST_ARCHIVE_PATH}
            if set(names) != expected_names:
                raise RuntimeError("archive entries do not match manifest")

            for identity, pair in pair_records.items():
                if pair_totals[identity]["file_count"] != pair["file_count"]:
                    raise RuntimeError("manifest pair file_count mismatch")
                if pair_totals[identity]["total_bytes"] != pair["total_bytes"]:
                    raise RuntimeError("manifest pair total_bytes mismatch")

            for archive_name, file_record in declared_files.items():
                info = archive.getinfo(archive_name)
                if info.is_dir() or info.file_size != file_record["size"]:
                    raise RuntimeError("archive entry size mismatch for {}".format(archive_name))
                digest = hashlib.sha256()
                byte_count = 0
                with archive.open(info, "r") as source:
                    while True:
                        chunk = source.read(HASH_CHUNK_SIZE)
                        if not chunk:
                            break
                        digest.update(chunk)
                        byte_count += len(chunk)
                if byte_count != file_record["size"]:
                    raise RuntimeError("archive entry byte count mismatch for {}".format(archive_name))
                if digest.hexdigest() != file_record["sha256"]:
                    raise RuntimeError("archive entry sha256 mismatch for {}".format(archive_name))
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise RuntimeError("backup verification failed: {}".format(exc)) from exc


def _backup_zip(src_dir, dest_zip, log, dry_run, *, pairs=None, source_layer=None):
    """Zip ``src_dir`` atomically and include a portable integrity manifest."""
    if not os.path.isdir(src_dir):
        raise RuntimeError("Source dir not found: {}".format(src_dir))
    if source_layer == "desktop-metadata" and not pairs:
        raise RuntimeError(
            "No account/organisation metadata pairs found; preserving prior backup"
        )
    src_bytes, src_files = _dir_stats(src_dir)
    log("  Source : {}".format(src_dir))
    log("  Size   : {:,} bytes ({} files)".format(src_bytes, src_files))
    log("  Dest   : {}".format(dest_zip))
    if dry_run:
        log(
            "  [DRY-RUN] Would create zip with manifest.json "
            "(layout_version={}, pairs={}).".format(
                BACKUP_LAYOUT_VERSION, len(pairs or [])
            )
        )
        return src_bytes
    os.makedirs(os.path.dirname(dest_zip), exist_ok=True)
    tmp = dest_zip + ".tmp"
    try:
        manifest_pairs = _manifest_pairs(src_dir, pairs)
        manifest_files = []
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            for full in _iter_source_files(src_dir):
                arcname = os.path.relpath(full, src_dir).replace("\\", "/")
                if arcname == MANIFEST_ARCHIVE_PATH:
                    raise RuntimeError(
                        "source path collides with reserved archive control path: {}".format(
                            MANIFEST_ARCHIVE_PATH
                        )
                    )
                if any(item["archive_path"] == arcname for item in manifest_files):
                    raise RuntimeError("archive path collision: {}".format(arcname))
                byte_count, digest = _write_hashed_zip_entry(zf, full, arcname)
                file_record = {
                    "archive_path": arcname,
                    "size": byte_count,
                    "sha256": digest,
                }
                for pair in manifest_pairs:
                    prefix = pair["archive_root"].rstrip("/") + "/"
                    if arcname.startswith(prefix):
                        file_record["account_uuid"] = pair["account_uuid"]
                        file_record["organisation_uuid"] = pair["organisation_uuid"]
                        pair["file_count"] += 1
                        pair["total_bytes"] += byte_count
                        break
                manifest_files.append(file_record)

            manifest = {
                "layout_version": BACKUP_LAYOUT_VERSION,
                "source_layer": source_layer or "directory",
                "pairs": manifest_pairs,
                "files": manifest_files,
            }
            zf.writestr(
                MANIFEST_ARCHIVE_PATH,
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            )
        _verify_backup_zip(tmp)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise

    try:
        # Atomic replacement either publishes the verified temp archive or
        # leaves an existing same-day final untouched. Never unlink the final.
        os.replace(tmp, dest_zip)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
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
                appdata_claude_dir = default_claude_appdata_dir()
                snap = build_snapshot(appdata_claude_dir, PROJECTS_ROOT)
                if snap["schema_version"] == "unrecognised":
                    log("WARNING: state schema not recognised -- backup proceeds but "
                        "run diagnose.py to investigate the unrecognised state.")
            except Exception:
                pass

        errors, total_bytes, done = 0, 0, 0

        log("--- [1/3] Desktop metadata ---")
        try:
            meta_pairs = _discover_meta_pairs()
            dest_zip = os.path.join(DEST_METADATA, "{}.zip".format(day_stamp))
            total_bytes += _backup_zip(
                SESSIONS_BASE,
                dest_zip,
                log,
                DRY_RUN,
                pairs=meta_pairs,
                source_layer="desktop-metadata",
            )
            log("  Pairs  : {}".format(len(meta_pairs)))
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
            total_bytes += _backup_zip(
                PROJECTS_ROOT,
                dest_zip,
                log,
                DRY_RUN,
                source_layer="jsonl-projects",
            )
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
