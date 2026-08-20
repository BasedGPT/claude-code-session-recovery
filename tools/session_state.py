"""Read-only inspection of Claude Desktop session state.

The executable diagnostic and session tools share this module.  It owns the
metadata/transcript relationship and deterministic diagnosis token; command
parsing and human-facing output remain with their executable tools.
"""
import collections
import hashlib
import json
import os
import platform
import plistlib
import re
import subprocess

from transcript_files import (
    build_transcript_path_inventory,
    count_assistant_records,
)
from session_metadata import build_metadata_path_inventory
from platform_support import (
    default_claude_appdata_dir,
    default_claude_paths,
    desktop_process_check_command,
    desktop_process_running,
)


def find_metadata_directories(appdata_claude_dir):
    """Yield ``(account_uuid, organisation_uuid, directory)`` metadata roots."""
    sessions_root = os.path.join(appdata_claude_dir, "claude-code-sessions")
    if not os.path.isdir(sessions_root):
        return
    for account_uuid in sorted(os.listdir(sessions_root)):
        account_dir = os.path.join(sessions_root, account_uuid)
        if not os.path.isdir(account_dir):
            continue
        for organisation_uuid in sorted(os.listdir(account_dir)):
            metadata_dir = os.path.join(account_dir, organisation_uuid)
            if os.path.isdir(metadata_dir):
                yield account_uuid, organisation_uuid, metadata_dir


def slug_from_path(path):
    """Derive the junction-comparison slug from a cwd path string."""
    normalised = path.replace("\\", "/")
    if len(normalised) >= 2 and normalised[1] == ":":
        normalised = normalised[0] + normalised[2:]
    return "--".join(part for part in normalised.split("/") if part)


def slug_encode(cwd):
    """Encode a cwd path into the Claude Code projects-directory slug."""
    encoded = cwd
    for character in (":", "\\", "/", ".", " "):
        encoded = encoded.replace(character, "-")
    return encoded


def classify_cwd(cwd):
    """Classify a cwd path as junction, canonical, bare_root, or other."""
    if not cwd:
        return "other"
    try:
        real = os.path.realpath(cwd)
    except OSError:
        return "other"
    if not os.path.exists(cwd):
        return "other"
    if os.path.normcase(real) != os.path.normcase(cwd):
        return "junction"
    parts = cwd.replace("\\", "/").rstrip("/").split("/")
    clean_parts = [part for part in parts if part and part != ":"]
    if len(clean_parts) <= 2:
        return "bare_root"
    return "canonical"


def _detect_mapped_drive_slugs(projects_dir):
    """Return mapped-drive slug count and affected drive letters on Windows."""
    if platform.system() != "Windows" or not os.path.isdir(projects_dir):
        return 0, []
    affected_drives = set()
    for slug in os.listdir(projects_dir):
        slug_dir = os.path.join(projects_dir, slug)
        if not os.path.isdir(slug_dir):
            continue
        if len(slug) >= 3 and slug[1:3] == "--" and slug[0].isalpha():
            drive_letter = slug[0].upper()
            try:
                if os.path.realpath(drive_letter + ":\\").startswith("\\\\"):
                    affected_drives.add(drive_letter)
            except OSError:
                pass
    return len(affected_drives), sorted(affected_drives)


def _detect_desktop_version():
    if platform.system() == "Darwin":
        app_paths = (
            "/Applications/Claude.app",
            os.path.expanduser("~/Applications/Claude.app"),
        )
        for app_path in app_paths:
            info_path = os.path.join(app_path, "Contents", "Info.plist")
            try:
                with open(info_path, "rb") as handle:
                    info = plistlib.load(handle)
            except (OSError, plistlib.InvalidFileException, ValueError):
                continue
            version = info.get("CFBundleShortVersionString") or info.get("CFBundleVersion")
            if version:
                return str(version)
        return None

    local_app = os.environ.get("LOCALAPPDATA", "")
    anthropic_dir = os.path.join(local_app, "AnthropicClaude")
    if not os.path.isdir(anthropic_dir):
        return None
    try:
        for entry in sorted(os.listdir(anthropic_dir), reverse=True):
            if entry.startswith("app-"):
                return entry[4:]
    except OSError:
        pass
    return None


def _detect_cli_version():
    try:
        result = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True,
            timeout=5, check=False,
        )
        if result.returncode == 0:
            match = re.search(r"\d+\.\d+\.\d+", result.stdout.strip())
            if match:
                return match.group(0)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def _detect_desktop_running():
    """Return whether Claude Desktop is running on the current platform."""
    return desktop_process_running()


def _detect_install_type():
    """Return ``(install_type, msix_real_path)`` for the live Desktop install."""
    if platform.system() != "Windows":
        return "not_windows", None
    claude_appdata = default_claude_appdata_dir()
    if not os.path.exists(claude_appdata):
        return "unknown", None
    try:
        real_path = os.path.realpath(claude_appdata)
        real_lower = real_path.lower()
        if "packages" in real_lower and "claude_" in real_lower:
            return "msix", real_path
        if os.path.normcase(real_path) != os.path.normcase(claude_appdata):
            return "unknown", None
    except OSError:
        pass
    return "exe", None


def _detect_running_inside_desktop():
    """Return whether the current process is a descendant of Claude Desktop."""
    if platform.system() == "Darwin":
        try:
            result = subprocess.run(
                ["ps", "-axo", "pid=,ppid=,comm=,args="],
                capture_output=True, text=True, timeout=10, check=False,
            )
            processes = {}
            for line in result.stdout.splitlines():
                parts = line.strip().split(None, 3)
                if len(parts) < 3:
                    continue
                try:
                    processes[int(parts[0])] = (int(parts[1]), " ".join(parts[2:]).lower())
                except (ValueError, IndexError):
                    continue
            current = os.getpid()
            visited = set()
            while current and current not in visited:
                visited.add(current)
                if current not in processes:
                    break
                parent, command = processes[current]
                if (
                    "/claude.app/contents/macos/claude" in command
                    or os.path.basename(command.split(None, 1)[0]) == "claude"
                ):
                    return True
                current = parent
        except (OSError, subprocess.TimeoutExpired):
            pass
        return False

    try:
        result = subprocess.run(
            ["wmic", "process", "get", "ProcessId,ParentProcessId,Name", "/format:csv"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        processes = {}
        for line in result.stdout.splitlines():
            parts = line.strip().split(",")
            if len(parts) < 4:
                continue
            try:
                processes[int(parts[3])] = (parts[1].lower(), int(parts[2]))
            except (ValueError, IndexError):
                continue
        current = os.getpid()
        visited = set()
        while current and current not in visited:
            visited.add(current)
            if current not in processes:
                break
            name, parent = processes[current]
            if "claude" in name:
                return True
            current = parent
    except Exception:
        return False
    return False


def build_snapshot(appdata_claude_dir, projects_dir, fixture_mode=False):
    """Probe state and return the deterministic diagnosis snapshot."""
    metadata_inventory = build_metadata_path_inventory(appdata_claude_dir)
    metadata_files = [
        (record.path, record.data) for record in metadata_inventory.records
    ]
    desktop_session_pairs = []
    if metadata_inventory.is_complete:
        metadata_counts = collections.Counter(
            (record.account_uuid, record.organisation_uuid)
            for record in metadata_inventory.records
        )
        desktop_session_pairs = [
            {
                "account_uuid": account_uuid,
                "organisation_uuid": organisation_uuid,
                "local_metadata_count": metadata_counts[
                    (account_uuid, organisation_uuid)
                ],
            }
            for account_uuid, organisation_uuid, _path
            in metadata_inventory.directories
        ]

    transcript_inventory = build_transcript_path_inventory(projects_dir)
    transcript_paths = transcript_inventory.by_session_id
    cross_store_complete = (
        metadata_inventory.is_complete and transcript_inventory.is_complete
    )
    total_metadata_count = len(metadata_files)
    missing_cli_count = sum(
        1 for _path, data in metadata_files
        if not data.get("cliSessionId") and not data.get("isArchived")
    )
    with_cli_count = sum(1 for _path, data in metadata_files if data.get("cliSessionId"))
    dangling_cli_count = 0
    if cross_store_complete:
        dangling_cli_count = sum(
            1 for _path, data in metadata_files
            if data.get("cliSessionId") and data["cliSessionId"] not in transcript_paths
        )
    cli_counts = collections.Counter(
        data.get("cliSessionId") for _path, data in metadata_files if data.get("cliSessionId")
    )
    duplicate_cli_count = sum(1 for count in cli_counts.values() if count > 1)
    null_timestamp_count = sum(
        1 for _path, data in metadata_files
        if not data.get("isArchived")
        and any(key in data and data[key] is None for key in ("createdAt", "updatedAt", "lastActivityAt"))
    )

    cwd_prefix_types = {"junction": 0, "canonical": 0, "bare_root": 0, "other": 0}
    junction_mismatch_count = 0
    for _path, data in metadata_files:
        cwd = data.get("cwd", "")
        cwd_type = classify_cwd(cwd)
        cwd_prefix_types[cwd_type] = cwd_prefix_types.get(cwd_type, 0) + 1
        cli_session_id = data.get("cliSessionId")
        if (
            cross_store_complete
            and cwd_type == "junction"
            and cli_session_id in transcript_paths
        ):
            try:
                if slug_from_path(os.path.realpath(cwd)) != slug_from_path(cwd):
                    junction_mismatch_count += 1
            except OSError:
                pass

    all_transcript_ids = set(transcript_paths)
    referenced_ids = {
        data.get("cliSessionId") for _path, data in metadata_files if data.get("cliSessionId")
    }
    orphan_count = (
        len(all_transcript_ids - referenced_ids) if cross_store_complete else 0
    )

    cwd_slug_mismatch_count = 0
    if cross_store_complete:
        for _path, data in metadata_files:
            cli_session_id = data.get("cliSessionId")
            cwd = data.get("cwd", "")
            if not cli_session_id or not cwd or cli_session_id not in transcript_paths:
                continue
            actual_slugs = {
                os.path.basename(os.path.dirname(path))
                for path in transcript_paths[cli_session_id]
            }
            if slug_encode(cwd) not in actual_slugs:
                cwd_slug_mismatch_count += 1

    truncated_count = 0
    if not fixture_mode and cross_store_complete:
        for _path, data in metadata_files:
            cli_session_id = data.get("cliSessionId")
            completed_turns = data.get("completedTurns")
            if (
                not cli_session_id
                or not completed_turns
                or cli_session_id not in transcript_paths
                or len(transcript_paths[cli_session_id]) != 1
            ):
                continue
            if completed_turns <= 0:
                continue
            if count_assistant_records(
                transcript_paths[cli_session_id][0], stop_at=completed_turns
            ) < completed_turns:
                truncated_count += 1

    schema_version = "unrecognised"
    if metadata_inventory.is_complete and total_metadata_count > 0:
        known_fields = {"sessionId", "cwd", "createdAt", "model", "title"}
        if any(known_fields.intersection(data.keys()) for _path, data in metadata_files[:5]):
            schema_version = "recognised"

    mapped_drive_count, mapped_drive_letters = _detect_mapped_drive_slugs(projects_dir)
    if fixture_mode:
        desktop_version = None
        cli_version = None
        desktop_running = False
        running_inside_desktop = False
        install_type = None
        msix_real_path = None
    else:
        desktop_version = _detect_desktop_version()
        cli_version = _detect_cli_version()
        desktop_running = _detect_desktop_running()
        running_inside_desktop = _detect_running_inside_desktop() if desktop_running else False
        install_type, msix_real_path = _detect_install_type()

    snapshot = {
        "total_metadata_count": total_metadata_count,
        "metadata_with_cli_count": with_cli_count,
        "metadata_missing_cli_count": missing_cli_count,
        "metadata_dangling_cli_count": dangling_cli_count,
        "metadata_duplicate_cli_count": duplicate_cli_count,
        "metadata_null_timestamp_count": null_timestamp_count,
        "cwd_junction_mismatch_count": junction_mismatch_count,
        "jsonl_orphan_count": orphan_count,
        "cwd_slug_mismatch_count": cwd_slug_mismatch_count,
        "truncated_jsonl_count": truncated_count,
        "cwd_prefix_types": cwd_prefix_types,
        "jsonl_count": transcript_inventory.unique_session_id_count,
        "schema_version": schema_version,
        "desktop_version": desktop_version,
        "cli_version": cli_version,
        "desktop_running": desktop_running,
        "running_inside_desktop": running_inside_desktop,
        "install_type": install_type,
        "msix_real_path": msix_real_path,
        "mapped_drive_unc_mismatch_count": mapped_drive_count,
        "mapped_drive_affected_drives": mapped_drive_letters,
        # Private token input: pair identity must invalidate a stale mutator
        # token even when exactly one pair exists. diagnose.py omits this
        # internal field from human and JSON output for compatibility.
        "_desktop_session_pair_identities": [
            {
                "account_uuid": pair["account_uuid"],
                "organisation_uuid": pair["organisation_uuid"],
            }
            for pair in desktop_session_pairs
        ],
    }
    if len(desktop_session_pairs) > 1:
        # Public diagnostics expose stable opaque labels and useful counts,
        # never the account/organisation directory names. Labels follow the
        # already-sorted hidden identities, so repeated scans of the same
        # state produce the same output without disclosing either UUID.
        snapshot["desktop_session_pairs"] = [
            {
                "pair_label": f"pair-{index:02d}",
                "local_metadata_count": pair["local_metadata_count"],
            }
            for index, pair in enumerate(desktop_session_pairs, start=1)
        ]
        # More than one populated pair is just as ambiguous as a populated
        # pair alongside a newly-created empty pair. Never treat the former
        # as a healthy state merely because every pair has metadata.
        if sum(
            pair["local_metadata_count"] > 0 for pair in desktop_session_pairs
        ) >= 1:
            snapshot["account_uuid_rotation_count"] = 1
    return snapshot


def make_diagnosis_id(snapshot):
    """Return the deterministic eight-hex diagnosis token for a snapshot."""
    structural_keys = (
        "total_metadata_count",
        "metadata_with_cli_count",
        "metadata_missing_cli_count",
        "metadata_dangling_cli_count",
        "metadata_duplicate_cli_count",
        "metadata_null_timestamp_count",
        "cwd_junction_mismatch_count",
        "cwd_slug_mismatch_count",
        "truncated_jsonl_count",
        "jsonl_orphan_count",
        "cwd_prefix_types",
        "jsonl_count",
        "schema_version",
        "mapped_drive_unc_mismatch_count",
        "_desktop_session_pair_identities",
        "desktop_session_pairs",
        "account_uuid_rotation_count",
    )
    structural = {key: snapshot[key] for key in structural_keys if key in snapshot}
    canonical = json.dumps(structural, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]


def scan_vscode_dropped_sessions(projects_dir):
    """Return ``(dropped_count, database_count)`` for the VS Code cache scan."""
    try:
        import pathlib
        import sqlite3
    except ImportError:
        return 0, 0

    uuid_pattern = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        re.IGNORECASE,
    )
    if platform.system() == "Darwin":
        workspace_dir = os.path.expanduser("~/Library/Application Support/Code/User/workspaceStorage")
    elif platform.system() == "Linux":
        workspace_dir = os.path.expanduser("~/.config/Code/User/workspaceStorage")
    else:
        workspace_dir = os.path.join(
            os.environ.get("APPDATA", os.path.expanduser("~")), "Code", "User", "workspaceStorage"
        )
    if not os.path.isdir(workspace_dir):
        return 0, 0

    cached_ids = set()
    database_count = 0
    for workspace_id in os.listdir(workspace_dir):
        database_path = os.path.join(workspace_dir, workspace_id, "state.vscdb")
        if not os.path.isfile(database_path):
            continue
        try:
            database_uri = pathlib.Path(database_path).resolve().as_uri() + "?mode=ro"
            connection = sqlite3.connect(database_uri, uri=True)
            try:
                row = connection.execute(
                    "SELECT value FROM ItemTable WHERE key='agentSessions.model.cache'"
                ).fetchone()
            finally:
                connection.close()
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
        if not any(isinstance(entry, dict) and "claude-code:/" in entry.get("resource", "") for entry in cache):
            continue
        database_count += 1
        for entry in cache:
            resource = entry.get("resource", "") if isinstance(entry, dict) else ""
            if resource.startswith("claude-code:/"):
                session_id = resource[len("claude-code:/"):]
                if uuid_pattern.match(session_id):
                    cached_ids.add(session_id)

    if database_count == 0:
        return 0, 0
    inventory = build_transcript_path_inventory(
        projects_dir, lambda value: bool(uuid_pattern.match(value))
    )
    if not inventory.is_complete:
        return 0, database_count
    dropped_count = sum(
        1 for session_id in inventory.by_session_id if session_id not in cached_ids
    )
    return dropped_count, database_count
