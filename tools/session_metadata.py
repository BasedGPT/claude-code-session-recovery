"""Complete, read-only discovery of Claude Desktop session metadata.

Selectors must not infer uniqueness, absence, or mutation targets from a
visible subset.  This module therefore records every account, organisation,
and ``local_*.json`` discovery failure as an opaque partial-inventory error.
It never prints paths or metadata content.
"""

from dataclasses import dataclass
import json
import os


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

    @property
    def is_complete(self):
        return self.status == "complete"


def build_metadata_path_inventory(appdata_claude_dir):
    """Discover and parse all direct account/org ``local_*.json`` files.

    A missing ``claude-code-sessions`` root is a known complete-empty state.
    Existing but inaccessible account, organisation, or file boundaries make
    the result partial so mutation selectors can fail closed.
    """
    records = []
    directories = []
    error_codes = []
    physical_file_count = 0
    if not appdata_claude_dir:
        return MetadataPathInventory((), (), 0, "complete", ())

    sessions_root = os.path.join(
        os.path.abspath(appdata_claude_dir), "claude-code-sessions"
    )
    try:
        with os.scandir(sessions_root) as entries:
            accounts = sorted(entries, key=lambda entry: entry.name)
    except FileNotFoundError:
        return MetadataPathInventory((), (), 0, "complete", ())
    except OSError:
        error_codes.append("metadata_sessions_root_list_failed")
        accounts = []

    for account_entry in accounts:
        try:
            is_account_dir = account_entry.is_dir()
        except OSError:
            error_codes.append("metadata_account_stat_failed")
            continue
        if not is_account_dir:
            continue
        try:
            with os.scandir(account_entry.path) as entries:
                organisations = sorted(entries, key=lambda entry: entry.name)
        except OSError:
            error_codes.append("metadata_account_list_failed")
            continue

        for organisation_entry in organisations:
            try:
                is_organisation_dir = organisation_entry.is_dir()
            except OSError:
                error_codes.append("metadata_organisation_stat_failed")
                continue
            if not is_organisation_dir:
                continue
            metadata_dir = os.path.abspath(organisation_entry.path)
            directories.append((
                account_entry.name,
                organisation_entry.name,
                metadata_dir,
            ))
            try:
                with os.scandir(metadata_dir) as entries:
                    metadata_entries = sorted(entries, key=lambda entry: entry.name)
            except OSError:
                error_codes.append("metadata_organisation_list_failed")
                continue

            for entry in metadata_entries:
                if not (
                    entry.name.startswith("local_")
                    and entry.name.endswith(".json")
                ):
                    continue
                try:
                    is_file = entry.is_file()
                except OSError:
                    error_codes.append("metadata_file_stat_failed")
                    continue
                if not is_file:
                    error_codes.append("metadata_file_not_regular_file")
                    continue
                physical_file_count += 1
                path = os.path.abspath(entry.path)
                try:
                    with open(path, "r", encoding="utf-8") as handle:
                        data = json.load(handle)
                except OSError:
                    error_codes.append("metadata_file_read_failed")
                    continue
                except ValueError:
                    error_codes.append("metadata_file_parse_failed")
                    continue
                if not isinstance(data, dict):
                    error_codes.append("metadata_file_non_object")
                    continue
                records.append(MetadataRecord(
                    account_uuid=account_entry.name,
                    organisation_uuid=organisation_entry.name,
                    path=path,
                    data=data,
                ))

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
    )


def require_complete_metadata_inventory(inventory):
    """Return ``inventory`` or fail before metadata-dependent selection."""
    if not inventory.is_complete:
        raise IncompleteMetadataInventoryError(inventory.errors)
    return inventory
