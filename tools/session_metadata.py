"""Complete, read-only discovery of Claude Desktop session metadata.

Selectors must not infer uniqueness, absence, or mutation targets from a
visible subset.  This module therefore records every account, organisation,
and ``local_*.json`` discovery failure as an opaque partial-inventory error.
It never prints paths or metadata content.
"""

from dataclasses import dataclass
import json
import os
import re

from metadata_archive import MAX_METADATA_FILE_BYTES


# These limits are far above normal Desktop metadata inventories, but make the
# discovery contract explicit and testable under hostile directory trees or
# metadata files.  Focused tests monkeypatch them to small values.
MAX_METADATA_ENTRIES_PER_DIRECTORY = 50_000
MAX_METADATA_TRAVERSAL_ENTRIES = 500_000
MAX_METADATA_DIRECTORIES = 10_000
MAX_METADATA_FILES = 250_000
MAX_METADATA_TOTAL_RETAINED_BYTES = 128 * 1024 * 1024
MAX_METADATA_RECORDS = 100_000
MAX_METADATA_JSON_DEPTH = 64


_EMBEDDED_UUID_RE = re.compile(
    r"(?<![0-9a-fA-F])"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    r"(?![0-9a-fA-F])"
)


def referenced_transcript_id(data):
    """Return the transcript ID referenced by one Desktop metadata record."""
    cli_session_id = data.get("cliSessionId")
    if cli_session_id:
        return cli_session_id
    if (
        "cliSessionId" not in data
        or data["cliSessionId"] is not None
        or data.get("isArchived")
    ):
        return None
    session_id = data.get("sessionId")
    if not isinstance(session_id, str):
        return None
    match = _EMBEDDED_UUID_RE.search(session_id)
    return match.group(1) if match else None


class IncompleteMetadataInventoryError(RuntimeError):
    """Raised when metadata-dependent selection cannot prove completeness."""

    def __init__(self, errors):
        self.errors = tuple(errors)
        codes = sorted({error.code for error in self.errors})
        detail = ", ".join(codes[:5])
        if len(codes) > 5:
            detail += ", ..."
        super().__init__("metadata inventory is partial: " + detail)


@dataclass(frozen=True)
class MetadataInventoryError:
    """Opaque discovery error safe for default command output."""

    reference: str
    code: str


@dataclass(frozen=True)
class MetadataRecord:
    """One successfully parsed physical metadata file."""

    account_uuid: str
    organisation_uuid: str
    path: str
    data: dict


@dataclass(frozen=True)
class MetadataPathInventory:
    """Complete or partial inventory of physical Desktop metadata."""

    records: tuple
    directories: tuple
    physical_file_count: int
    status: str
    errors: tuple
    retained_bytes: int = 0

    @property
    def is_complete(self):
        return self.status == "complete"


def _bounded_scandir(path, traversal_state):
    """Return a sorted bounded prefix and whether either listing cap fired."""
    retained = []
    entry_cap_exceeded = False
    traversal_cap_exceeded = False
    with os.scandir(path) as entries:
        for entry in entries:
            if traversal_state[0] >= MAX_METADATA_TRAVERSAL_ENTRIES:
                traversal_cap_exceeded = True
                break
            if len(retained) >= MAX_METADATA_ENTRIES_PER_DIRECTORY:
                entry_cap_exceeded = True
                break
            retained.append(entry)
            traversal_state[0] += 1
    retained.sort(key=lambda entry: entry.name)
    return retained, entry_cap_exceeded, traversal_cap_exceeded


def _json_depth_exceeds(raw, max_depth):
    """Check JSON container depth from bounded UTF-8 bytes before parsing."""
    depth = 0
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # backslash
                escaped = True
            elif byte == 0x22:  # quote
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x7B, 0x5B):  # { or [
            depth += 1
            if depth > max_depth:
                return True
        elif byte in (0x7D, 0x5D) and depth:
            depth -= 1
    return False


def build_metadata_path_inventory(appdata_claude_dir, *, excluded_paths=None):
    """Discover and parse all direct account/org ``local_*.json`` files.

    A missing ``claude-code-sessions`` root is a known complete-empty state.
    Existing but inaccessible account, organisation, or file boundaries make
    the result partial so mutation selectors can fail closed.  A caller may
    exclude invocation-owned paths from a transaction-normalized snapshot;
    excluded names are ignored before stat/open so a retained object lease
    cannot make that normalized inventory spuriously partial.
    """
    records = []
    directories = []
    error_codes = []
    physical_file_count = 0
    retained_bytes = 0
    directory_count = 0
    traversal_state = [0]
    stop_inventory = False

    def add_error(code, *, unique=False):
        if not unique or code not in error_codes:
            error_codes.append(code)

    def scan_directory(path):
        nonlocal stop_inventory
        entries, entry_cap, traversal_cap = _bounded_scandir(
            path, traversal_state
        )
        if entry_cap:
            add_error("metadata_directory_entry_cap_exceeded", unique=True)
        if traversal_cap:
            add_error("metadata_traversal_cap_exceeded", unique=True)
            stop_inventory = True
        return entries

    excluded = {
        os.path.normcase(os.path.abspath(path))
        for path in (excluded_paths or ())
    }
    if not appdata_claude_dir:
        return MetadataPathInventory((), (), 0, "complete", ())

    sessions_root = os.path.join(
        os.path.abspath(appdata_claude_dir), "claude-code-sessions"
    )
    try:
        accounts = scan_directory(sessions_root)
    except FileNotFoundError:
        return MetadataPathInventory((), (), 0, "complete", ())
    except OSError:
        error_codes.append("metadata_sessions_root_list_failed")
        accounts = []

    for account_entry in accounts:
        if stop_inventory:
            break
        try:
            is_account_dir = account_entry.is_dir()
        except OSError:
            error_codes.append("metadata_account_stat_failed")
            continue
        if not is_account_dir:
            continue
        if directory_count >= MAX_METADATA_DIRECTORIES:
            add_error("metadata_directory_cap_exceeded", unique=True)
            break
        directory_count += 1
        try:
            organisations = scan_directory(account_entry.path)
        except OSError:
            error_codes.append("metadata_account_list_failed")
            continue

        for organisation_entry in organisations:
            if stop_inventory:
                break
            try:
                is_organisation_dir = organisation_entry.is_dir()
            except OSError:
                error_codes.append("metadata_organisation_stat_failed")
                continue
            if not is_organisation_dir:
                continue
            if directory_count >= MAX_METADATA_DIRECTORIES:
                add_error("metadata_directory_cap_exceeded", unique=True)
                stop_inventory = True
                break
            directory_count += 1
            metadata_dir = os.path.abspath(organisation_entry.path)
            directories.append((
                account_entry.name,
                organisation_entry.name,
                metadata_dir,
            ))
            try:
                metadata_entries = scan_directory(metadata_dir)
            except OSError:
                error_codes.append("metadata_organisation_list_failed")
                continue

            for entry in metadata_entries:
                if stop_inventory:
                    break
                if not (
                    entry.name.startswith("local_")
                    and entry.name.endswith(".json")
                ):
                    continue
                path = os.path.abspath(entry.path)
                if os.path.normcase(path) in excluded:
                    continue
                try:
                    is_file = entry.is_file()
                except OSError:
                    error_codes.append("metadata_file_stat_failed")
                    continue
                if not is_file:
                    error_codes.append("metadata_file_not_regular_file")
                    continue
                if physical_file_count >= MAX_METADATA_FILES:
                    add_error("metadata_file_cap_exceeded", unique=True)
                    stop_inventory = True
                    break
                physical_file_count += 1
                if len(records) >= MAX_METADATA_RECORDS:
                    add_error("metadata_record_cap_exceeded", unique=True)
                    stop_inventory = True
                    break
                try:
                    with open(path, "rb") as handle:
                        raw = handle.read(MAX_METADATA_FILE_BYTES + 1)
                except OSError:
                    error_codes.append("metadata_file_read_failed")
                    continue
                if len(raw) > MAX_METADATA_FILE_BYTES:
                    add_error("metadata_file_byte_cap_exceeded")
                    continue
                if _json_depth_exceeds(raw, MAX_METADATA_JSON_DEPTH):
                    add_error("metadata_json_depth_exceeded")
                    continue
                try:
                    data = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError, RecursionError):
                    error_codes.append("metadata_file_parse_failed")
                    continue
                if not isinstance(data, dict):
                    error_codes.append("metadata_file_non_object")
                    continue
                if retained_bytes + len(raw) > MAX_METADATA_TOTAL_RETAINED_BYTES:
                    add_error("metadata_total_byte_cap_exceeded", unique=True)
                    stop_inventory = True
                    break
                records.append(MetadataRecord(
                    account_uuid=account_entry.name,
                    organisation_uuid=organisation_entry.name,
                    path=path,
                    data=data,
                ))
                retained_bytes += len(raw)

    records.sort(key=lambda record: record.path)
    directories.sort()
    errors = tuple(
        MetadataInventoryError(
            reference="metadata-scan-entry-{:04d}".format(index),
            code=code,
        )
        for index, code in enumerate(error_codes, start=1)
    )
    return MetadataPathInventory(
        records=tuple(records),
        directories=tuple(directories),
        physical_file_count=physical_file_count,
        status="partial" if errors else "complete",
        errors=errors,
        retained_bytes=retained_bytes,
    )


def require_complete_metadata_inventory(inventory):
    """Return ``inventory`` or fail before metadata-dependent selection."""
    if not inventory.is_complete:
        raise IncompleteMetadataInventoryError(inventory.errors)
    return inventory
