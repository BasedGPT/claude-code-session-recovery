"""Shared layout-v2 contract for Claude Desktop metadata archives.

This module validates only the portable manifest and archive inventory.  Zip
envelope limits, strict JSON decoding, payload JSON validation, and publication
policy remain the responsibility of the backup/restore command adapters.
"""

from dataclasses import dataclass
import json
import math
from pathlib import PurePosixPath
import re
import unicodedata


LAYOUT_VERSION = 2
SOURCE_LAYER = "desktop-metadata"
MANIFEST_NAME = "manifest.json"
MAX_METADATA_FILE_BYTES = 16 * 1024 * 1024
MAX_JSON_INTEGER_DIGITS = 4096
MAX_JSON_DEPTH = 256

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
SAFE_SEGMENT_RE = re.compile(r"^[^<>:\"/\\|?*\x00-\x1f]+$")
METADATA_FILENAME_RE = re.compile(
    r"^local_[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.json$"
)
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *("COM{}".format(index) for index in range(1, 10)),
    *("LPT{}".format(index) for index in range(1, 10)),
}


class MetadataArchiveFormatError(ValueError):
    """The manifest cannot describe a compatible v2 metadata archive."""


@dataclass(frozen=True)
class MetadataArchivePair:
    account_uuid: str
    organisation_uuid: str
    archive_root: str
    file_count: int
    total_bytes: int


@dataclass(frozen=True)
class MetadataArchiveFile:
    archive_path: str
    size: int
    sha256: str
    account_uuid: str
    organisation_uuid: str


@dataclass(frozen=True)
class MetadataArchiveManifest:
    pairs: tuple[MetadataArchivePair, ...]
    files: tuple[MetadataArchiveFile, ...]


def filesystem_key(value: str) -> str:
    """Return a conservative cross-platform path collision key."""
    return unicodedata.normalize("NFC", value).casefold()


def safe_segment(value: object) -> bool:
    if not (
        isinstance(value, str)
        and value not in ("", ".", "..")
        and value == value.strip()
        and not value.endswith(".")
        and SAFE_SEGMENT_RE.fullmatch(value) is not None
    ):
        return False
    return value.split(".", 1)[0].upper() not in WINDOWS_RESERVED_NAMES


def canonical_uuid(value: object) -> bool:
    return (
        isinstance(value, str)
        and safe_segment(value)
        and UUID_RE.fullmatch(value) is not None
    )


def metadata_filename(value: object) -> bool:
    return (
        isinstance(value, str)
        and safe_segment(value)
        and METADATA_FILENAME_RE.fullmatch(value) is not None
    )


def safe_archive_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and str(path) == value
        and all(safe_segment(part) for part in path.parts)
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError("non-finite JSON number: " + value)


def _strict_json_int(value: str) -> int:
    digits = value[1:] if value.startswith("-") else value
    if len(digits) > MAX_JSON_INTEGER_DIGITS:
        raise ValueError("JSON integer exceeds the digit limit")
    return int(value)


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise MetadataArchiveFormatError("JSON contains a duplicate object key")
        result[key] = value
    return result


def _validate_finite_json(value: object, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise MetadataArchiveFormatError("JSON exceeds the nesting-depth limit")
    if isinstance(value, float) and not math.isfinite(value):
        raise MetadataArchiveFormatError("JSON contains a non-finite number")
    if isinstance(value, dict):
        for item in value.values():
            _validate_finite_json(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _validate_finite_json(item, depth + 1)


def validate_metadata_payload_bytes(raw: bytes) -> dict:
    """Return a strict bounded metadata object accepted by layout-v2 restore."""
    if len(raw) > MAX_METADATA_FILE_BYTES:
        raise MetadataArchiveFormatError("metadata payload exceeds the bounded size limit")
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
            parse_int=_strict_json_int,
        )
        _validate_finite_json(payload)
    except MetadataArchiveFormatError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise MetadataArchiveFormatError(
            "metadata payload is not valid strict UTF-8 JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise MetadataArchiveFormatError(
            "metadata payload root must be a JSON object"
        )
    return payload


def validate_v2_metadata_manifest(
    manifest: object,
    archive_sizes: dict[str, int],
) -> MetadataArchiveManifest:
    """Validate the exact shared v2 metadata manifest/inventory contract."""
    if not isinstance(manifest, dict):
        raise MetadataArchiveFormatError("manifest root must be an object")
    if set(manifest) != {"layout_version", "source_layer", "pairs", "files"}:
        raise MetadataArchiveFormatError("manifest top-level schema is invalid")
    if manifest.get("layout_version") != LAYOUT_VERSION:
        raise MetadataArchiveFormatError("unsupported manifest layout_version")
    if manifest.get("source_layer") != SOURCE_LAYER:
        raise MetadataArchiveFormatError("archive is not a Desktop metadata backup")

    raw_pairs = manifest.get("pairs")
    raw_files = manifest.get("files")
    if not isinstance(raw_pairs, list) or not raw_pairs:
        raise MetadataArchiveFormatError("manifest must declare at least one metadata pair")
    if not isinstance(raw_files, list) or not raw_files:
        raise MetadataArchiveFormatError("manifest must declare at least one metadata file")

    pairs = []
    pair_by_identity = {}
    roots = set()
    for raw_pair in raw_pairs:
        if not isinstance(raw_pair, dict) or set(raw_pair) != {
            "account_uuid", "organisation_uuid", "archive_root",
            "file_count", "total_bytes",
        }:
            raise MetadataArchiveFormatError("manifest pair schema is invalid")
        account = raw_pair["account_uuid"]
        organisation = raw_pair["organisation_uuid"]
        archive_root = raw_pair["archive_root"]
        if not canonical_uuid(account) or not canonical_uuid(organisation):
            raise MetadataArchiveFormatError("manifest pair identity is not a canonical UUID")
        if archive_root != account + "/" + organisation or not safe_archive_path(archive_root):
            raise MetadataArchiveFormatError("manifest pair root does not match its identity")
        file_count = raw_pair["file_count"]
        total_bytes = raw_pair["total_bytes"]
        if (
            not isinstance(file_count, int) or isinstance(file_count, bool) or file_count < 0
            or not isinstance(total_bytes, int) or isinstance(total_bytes, bool)
            or total_bytes < 0
        ):
            raise MetadataArchiveFormatError("manifest pair counts are invalid")
        identity = (account, organisation)
        root_key = filesystem_key(archive_root)
        if identity in pair_by_identity or root_key in roots:
            raise MetadataArchiveFormatError("manifest contains duplicate metadata pairs")
        pair = MetadataArchivePair(
            account, organisation, archive_root, file_count, total_bytes
        )
        pairs.append(pair)
        pair_by_identity[identity] = pair
        roots.add(root_key)

    files = []
    declared_names = set()
    target_names = set()
    totals = {
        identity: {"file_count": 0, "total_bytes": 0}
        for identity in pair_by_identity
    }
    for raw_file in raw_files:
        if not isinstance(raw_file, dict) or set(raw_file) != {
            "archive_path", "size", "sha256", "account_uuid", "organisation_uuid",
        }:
            raise MetadataArchiveFormatError("manifest file schema is invalid")
        archive_path = raw_file["archive_path"]
        size = raw_file["size"]
        digest = raw_file["sha256"]
        identity = (raw_file["account_uuid"], raw_file["organisation_uuid"])
        parts = PurePosixPath(archive_path).parts if safe_archive_path(archive_path) else ()
        if (
            identity not in pair_by_identity
            or len(parts) != 3
            or parts[0] != identity[0]
            or parts[1] != identity[1]
            or not metadata_filename(parts[2])
            or archive_path == MANIFEST_NAME
            or archive_path in declared_names
        ):
            raise MetadataArchiveFormatError("manifest declares an unsafe or duplicate file")
        target_key = filesystem_key(archive_path)
        if target_key in target_names:
            raise MetadataArchiveFormatError("manifest files collide at the restore target")
        if (
            not isinstance(size, int) or isinstance(size, bool) or size < 0
            or not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None
        ):
            raise MetadataArchiveFormatError("manifest file integrity fields are invalid")
        if archive_sizes.get(archive_path) != size:
            raise MetadataArchiveFormatError("manifest entries or sizes do not match the archive")
        files.append(MetadataArchiveFile(
            archive_path, size, digest, identity[0], identity[1]
        ))
        declared_names.add(archive_path)
        target_names.add(target_key)
        totals[identity]["file_count"] += 1
        totals[identity]["total_bytes"] += size

    if set(archive_sizes) != declared_names:
        raise MetadataArchiveFormatError("archive entries do not exactly match the manifest")
    for identity, pair in pair_by_identity.items():
        if totals[identity]["file_count"] != pair.file_count:
            raise MetadataArchiveFormatError("manifest pair file_count does not match")
        if totals[identity]["total_bytes"] != pair.total_bytes:
            raise MetadataArchiveFormatError("manifest pair total_bytes does not match")

    return MetadataArchiveManifest(
        pairs=tuple(sorted(pairs, key=lambda pair: pair.archive_root)),
        files=tuple(sorted(files, key=lambda item: item.archive_path)),
    )
