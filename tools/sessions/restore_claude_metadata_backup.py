"""Safely restore Claude Desktop metadata from a toolkit backup archive.

The command is dry-run by default.  It accepts layout-version 2 Desktop
metadata archives produced by ``backup_claude_state.py`` and can inspect older
flat, manifest-less metadata archives.  Only v2 archives are mutation-eligible,
and differing live files are never overwritten.

Exit statuses:
  0  dry-run completed, nothing needed restoring, or apply completed
  2  invalid command-line invocation
  3  safety refusal (stale diagnosis, invalid archive, live Desktop, collision)
  4  staging or publication failed (rollback is attempted)
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import PurePosixPath
import secrets
import stat as stat_module
import sys
import tempfile
import zipfile
from typing import BinaryIO, Callable


try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass


TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.dirname(TOOL_DIR)
sys.path.insert(0, TOOL_DIR)
sys.path.insert(0, TOOLS_DIR)

from lock_utils import acquire_lock, release_lock  # noqa: E402
from metadata_archive import (  # noqa: E402
    LAYOUT_VERSION,
    MANIFEST_NAME,
    MAX_METADATA_FILE_BYTES,
    SOURCE_LAYER,
    MetadataArchiveFormatError,
    canonical_uuid as _canonical_uuid_like,
    filesystem_key as _filesystem_key,
    metadata_filename as _metadata_filename,
    safe_archive_path as _safe_archive_path,
    safe_segment as _safe_segment,
    validate_metadata_payload_bytes,
    validate_v2_metadata_manifest,
)
from mutator_safety import (  # noqa: E402
    current_snapshot_and_diagnosis_id,
    resolve_state_paths,
)
from platform_support import default_claude_paths, desktop_process_running  # noqa: E402
from session_state import build_snapshot, make_diagnosis_id  # noqa: E402


HASH_CHUNK_SIZE = 1024 * 1024
MAX_ARCHIVE_FILES = 10_000
MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE_SOURCE_BYTES = 1024 * 1024 * 1024
MAX_GUARD_FILES = 100_000
MAX_GUARD_BYTES = 4 * 1024 * 1024 * 1024
MAX_JSON_INTEGER_DIGITS = 4096
MAX_JSON_DEPTH = 256


class RestoreRefusal(RuntimeError):
    """An input or live-state safety condition makes restoration unsafe."""


class RestoreFailure(RuntimeError):
    """Staging or publication failed after the restore plan was accepted."""


@dataclass(frozen=True)
class RestoreEntry:
    archive_path: str
    target_relative: str
    pair_label: str
    file_name: str
    size: int
    sha256: str


@dataclass(frozen=True)
class RestorePlan:
    layout: str
    pair_count: int
    entries: tuple[RestoreEntry, ...]
    eligible: tuple[RestoreEntry, ...]
    identical: tuple[RestoreEntry, ...]
    identical_file_ids: tuple[tuple[RestoreEntry, tuple[int, object]], ...]


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return ((info.external_attr >> 16) & 0o170000) == 0o120000


def _sha256_file(path: str) -> tuple[int, str]:
    if os.name == "nt":
        return _windows_sha256_file(path)
    digest = hashlib.sha256()
    total = 0
    with open(path, "rb") as source:
        while True:
            chunk = source.read(HASH_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
    return total, digest.hexdigest()


def _windows_sha256_file(path: str) -> tuple[int, str]:
    """Hash through an explicit read handle compatible with retained leases."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        os.path.abspath(path),
        0x80000000,  # GENERIC_READ
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,
        0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), "cannot open file for hashing")
    try:
        attributes, _volume, _file_id = _windows_handle_file_record(handle)
        if attributes & (0x10 | 0x400):
            raise OSError("hash source is not a regular non-reparse file")
        digest = hashlib.sha256()
        total = 0
        buffer = ctypes.create_string_buffer(HASH_CHUNK_SIZE)
        read_file = kernel32.ReadFile
        read_file.argtypes = (
            wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
        )
        read_file.restype = wintypes.BOOL
        while True:
            count = wintypes.DWORD()
            if not read_file(
                handle, buffer, len(buffer), ctypes.byref(count), None
            ):
                raise OSError(ctypes.get_last_error(), "cannot hash retained file")
            if count.value == 0:
                break
            digest.update(buffer.raw[:count.value])
            total += count.value
        return total, digest.hexdigest()
    finally:
        kernel32.CloseHandle(handle)


def _windows_file_identity(path: str) -> tuple[int, bytes]:
    import ctypes
    from ctypes import wintypes

    class FileId128(ctypes.Structure):
        _fields_ = [("identifier", ctypes.c_ubyte * 16)]

    class FileIdInformation(ctypes.Structure):
        _fields_ = [
            ("volume_serial", ctypes.c_ulonglong),
            ("file_id", FileId128),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        os.path.abspath(path),
        0x00000080,
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,
        0x00200000,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), "cannot inspect file identity")
    try:
        info = FileIdInformation()
        get_info = kernel32.GetFileInformationByHandleEx
        get_info.argtypes = (
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        )
        get_info.restype = wintypes.BOOL
        if not get_info(handle, 18, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError(
                ctypes.get_last_error(), "cannot read full file identity"
            )
        return int(info.volume_serial), bytes(info.file_id.identifier)
    finally:
        kernel32.CloseHandle(handle)


def _windows_handle_file_record(handle) -> tuple[int, int, bytes]:
    """Return attributes and the full identity for an already-open handle."""
    import ctypes
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("access_time", wintypes.FILETIME),
            ("write_time", wintypes.FILETIME),
            ("volume_serial", wintypes.DWORD),
            ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD),
            ("links", wintypes.DWORD),
            ("index_high", wintypes.DWORD),
            ("index_low", wintypes.DWORD),
        ]

    class FileId128(ctypes.Structure):
        _fields_ = [("identifier", ctypes.c_ubyte * 16)]

    class FileIdInformation(ctypes.Structure):
        _fields_ = [
            ("volume_serial", ctypes.c_ulonglong),
            ("file_id", FileId128),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    basic = ByHandleFileInformation()
    get_basic = kernel32.GetFileInformationByHandle
    get_basic.argtypes = (
        wintypes.HANDLE, ctypes.POINTER(ByHandleFileInformation),
    )
    get_basic.restype = wintypes.BOOL
    if not get_basic(handle, ctypes.byref(basic)):
        raise OSError(ctypes.get_last_error(), "cannot inspect retained file handle")
    full = FileIdInformation()
    get_full = kernel32.GetFileInformationByHandleEx
    get_full.argtypes = (
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    )
    get_full.restype = wintypes.BOOL
    if not get_full(handle, 18, ctypes.byref(full), ctypes.sizeof(full)):
        raise OSError(ctypes.get_last_error(), "cannot read retained file identity")
    return (
        int(basic.attributes),
        int(full.volume_serial),
        bytes(full.file_id.identifier),
    )


def _set_windows_delete_disposition(handle) -> None:
    """Mark the object bound to *handle* for deletion; never resolve a path."""
    import ctypes
    from ctypes import wintypes

    class FileDispositionInformation(ctypes.Structure):
        _fields_ = [("delete_file", wintypes.BOOLEAN)]

    disposition = FileDispositionInformation(True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_info = kernel32.SetFileInformationByHandle
    set_info.argtypes = (
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    )
    set_info.restype = wintypes.BOOL
    if not set_info(
        handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)
    ):
        raise OSError(ctypes.get_last_error(), "object-bound deletion failed")


class _WindowsTargetLease:
    """Retain a no-delete-share handle to one newly published metadata link."""

    def __init__(self, path: str, expected_identity: tuple[int, object]):
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        handle = create_file(
            os.path.abspath(path),
            0x00010000 | 0x00000080,  # DELETE | FILE_READ_ATTRIBUTES
            0x00000001,  # FILE_SHARE_READ; deny write/delete sharing
            None,
            3,
            0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_last_error(), "cannot retain created target")
        self.handle = handle
        self.path = os.path.abspath(path)
        self.expected_identity = expected_identity
        try:
            attributes, volume, file_id = _windows_handle_file_record(handle)
            if attributes & (0x10 | 0x400):
                raise OSError("created target is not a regular non-reparse file")
            if (volume, file_id) != expected_identity:
                raise OSError("created target does not match the staged object")
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        handle = getattr(self, "handle", None)
        if handle is not None:
            self.handle = None
            _close_windows_handle(handle)

    def delete_bound(
        self,
        temporary: str,
        anchors: "_DirectoryAnchors",
        entry: "RestoreEntry",
    ) -> None:
        """Delete only this leased object after re-proving its staged link."""
        handle = self.handle
        if handle is None:
            raise OSError("created target lease is no longer held")
        try:
            attributes, volume, file_id = _windows_handle_file_record(handle)
            if attributes & (0x10 | 0x400):
                raise OSError("leased target changed file type")
            if (volume, file_id) != self.expected_identity:
                raise OSError("leased target identity changed")
            if (
                not anchors.exists(temporary)
                or anchors.identity(temporary) != self.expected_identity
                or anchors.hash_file(temporary) != (entry.size, entry.sha256)
            ):
                raise OSError("staged identity cannot bind rollback safely")
            _set_windows_delete_disposition(handle)
        finally:
            self.close()


def _file_identity(path: str) -> tuple[int, object]:
    if os.name == "nt":
        return _windows_file_identity(path)
    stat_result = os.stat(path, follow_symlinks=False)
    return stat_result.st_dev, stat_result.st_ino


def _reject_json_constant(value: str) -> None:
    raise ValueError("non-finite JSON number: " + value)


def _strict_json_int(value: str) -> int:
    digits = value[1:] if value.startswith("-") else value
    if len(digits) > MAX_JSON_INTEGER_DIGITS:
        raise ValueError("JSON integer exceeds the digit limit")
    return int(value)


def _validate_finite_json(value: object, depth: int = 0) -> None:
    """Reject non-finite floats, including finite-looking exponent overflow."""
    if depth > MAX_JSON_DEPTH:
        raise RestoreRefusal("JSON exceeds the nesting-depth limit")
    if isinstance(value, float) and not math.isfinite(value):
        raise RestoreRefusal("JSON contains a non-finite number")
    if isinstance(value, dict):
        for item in value.values():
            _validate_finite_json(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _validate_finite_json(item, depth + 1)


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RestoreRefusal("manifest contains a duplicate object key")
        result[key] = value
    return result


def _strict_json_loads(raw: bytes, description: str) -> object:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
            parse_int=_strict_json_int,
        )
        _validate_finite_json(value)
        return value
    except RestoreRefusal:
        raise
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise RestoreRefusal(description) from exc


def _stat_signature(result: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        result.st_dev,
        result.st_ino,
        result.st_mode,
        result.st_size,
        result.st_mtime_ns,
        result.st_ctime_ns,
    )


def _hash_open_file(handle: BinaryIO, byte_cap: int) -> tuple[int, str]:
    handle.seek(0)
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = handle.read(HASH_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > byte_cap:
            raise RestoreRefusal("archive source exceeds the bounded size limit")
        digest.update(chunk)
    handle.seek(0)
    return total, digest.hexdigest()


def _open_archive_source(path: str) -> BinaryIO:
    """Open the exact archive, denying path replacement on Windows."""
    if os.name != "nt":
        return open(path, "rb")

    import ctypes
    import msvcrt
    from ctypes import wintypes

    create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        os.path.abspath(path),
        0x80000000,             # GENERIC_READ
        0x00000001,             # FILE_SHARE_READ; deny write and replacement
        None,
        3,                      # OPEN_EXISTING
        0x00000080,             # FILE_ATTRIBUTE_NORMAL
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle == invalid:
        raise OSError(ctypes.get_last_error(), "cannot open archive source")
    try:
        descriptor = msvcrt.open_osfhandle(int(handle), os.O_RDONLY | os.O_BINARY)
    except Exception:
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)
        raise
    return os.fdopen(descriptor, "rb")


class _ArchiveSourceGuard:
    """Bind apply to one bounded regular archive file and its exact bytes."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        path_stat = os.stat(self.path, follow_symlinks=False)
        if os.path.islink(self.path) or not stat_module.S_ISREG(path_stat.st_mode):
            raise RestoreRefusal("archive source is not a regular non-link file")
        if path_stat.st_size > MAX_ARCHIVE_SOURCE_BYTES:
            raise RestoreRefusal("archive source exceeds the bounded size limit")
        self.handle = _open_archive_source(self.path)
        held_stat = os.fstat(self.handle.fileno())
        if (
            not stat_module.S_ISREG(held_stat.st_mode)
            or (path_stat.st_dev, path_stat.st_ino) != (held_stat.st_dev, held_stat.st_ino)
        ):
            self.handle.close()
            raise RestoreRefusal("archive source identity changed while opening")
        self.signature = _stat_signature(held_stat)
        self.size, self.sha256 = _hash_open_file(
            self.handle, MAX_ARCHIVE_SOURCE_BYTES
        )
        if self.size != held_stat.st_size:
            self.handle.close()
            raise RestoreRefusal("archive source changed while hashing")

    def verify(self) -> None:
        try:
            path_stat = os.stat(self.path, follow_symlinks=False)
            held_before = os.fstat(self.handle.fileno())
            size, digest = _hash_open_file(self.handle, MAX_ARCHIVE_SOURCE_BYTES)
            held_after = os.fstat(self.handle.fileno())
        except (OSError, ValueError) as exc:
            raise RestoreRefusal("archive source cannot be revalidated") from exc
        if os.path.islink(self.path) or not stat_module.S_ISREG(path_stat.st_mode):
            raise RestoreRefusal("archive source path changed identity")
        if (
            (path_stat.st_dev, path_stat.st_ino)
            != (held_after.st_dev, held_after.st_ino)
            or _stat_signature(held_before) != self.signature
            or _stat_signature(held_after) != self.signature
            or size != self.size
            or digest != self.sha256
        ):
            raise RestoreRefusal("archive source identity or contents changed")

    def close(self) -> None:
        self.handle.close()

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.close()


def _normal_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _windows_directory_record(
    path: str, *, delete_access: bool = False, share_delete: bool = False
):
    import ctypes
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("access_time", wintypes.FILETIME),
            ("write_time", wintypes.FILETIME),
            ("volume_serial", wintypes.DWORD),
            ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD),
            ("links", wintypes.DWORD),
            ("index_high", wintypes.DWORD),
            ("index_low", wintypes.DWORD),
        ]

    class FileId128(ctypes.Structure):
        _fields_ = [("identifier", ctypes.c_ubyte * 16)]

    class FileIdInformation(ctypes.Structure):
        _fields_ = [
            ("volume_serial", ctypes.c_ulonglong),
            ("file_id", FileId128),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    desired_access = 0x00000001 | 0x00000080
    if delete_access:
        desired_access |= 0x00010000  # DELETE
    share_mode = 0x00000001 | 0x00000002
    if share_delete:
        share_mode |= 0x00000004
    handle = create_file(
        os.path.abspath(path),
        desired_access,  # FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES [+ DELETE]
        share_mode,
        None,
        3,
        0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), "cannot anchor destination directory")
    info = ByHandleFileInformation()
    get_info = kernel32.GetFileInformationByHandle
    get_info.argtypes = (wintypes.HANDLE, ctypes.POINTER(ByHandleFileInformation))
    get_info.restype = wintypes.BOOL
    if not get_info(handle, ctypes.byref(info)):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        raise OSError(error, "cannot inspect destination directory handle")
    file_id_info = FileIdInformation()
    get_info_ex = kernel32.GetFileInformationByHandleEx
    get_info_ex.argtypes = (
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    )
    get_info_ex.restype = wintypes.BOOL
    if not get_info_ex(
        handle, 18, ctypes.byref(file_id_info), ctypes.sizeof(file_id_info)
    ):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        raise OSError(error, "cannot read full destination directory identity")
    get_final = kernel32.GetFinalPathNameByHandleW
    get_final.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
    get_final.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    length = get_final(handle, buffer, len(buffer), 0)
    if not length or length >= len(buffer):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        raise OSError(error, "cannot resolve destination directory handle")
    final_path = buffer.value
    if final_path.startswith("\\\\?\\UNC\\"):
        final_path = "\\\\" + final_path[8:]
    elif final_path.startswith("\\\\?\\"):
        final_path = final_path[4:]
    record = (
        int(info.attributes),
        int(file_id_info.volume_serial),
        bytes(file_id_info.file_id.identifier),
        _normal_path(final_path),
    )
    return handle, record


def _close_windows_handle(handle) -> None:
    import ctypes
    ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)


def _delete_windows_directory_record_bound(
    path: str, handle, expected: tuple[int, int, bytes, str]
) -> bool:
    """Delete one retained empty directory object without resolving deletion."""
    try:
        attributes, volume, file_id = _windows_handle_file_record(handle)
        if (
            not attributes & 0x10
            or attributes & 0x400
            or (volume, file_id) != (expected[1], expected[2])
        ):
            return False
        with os.scandir(path) as entries:
            if next(entries, None) is not None:
                return False
        _set_windows_delete_disposition(handle)
        return True
    except OSError:
        return False


def _cleanup_windows_created_directory_records(
    records: dict[str, tuple[object, tuple[int, int, bytes, str]]],
    created_dirs: list[str],
) -> bool:
    """Clean pre-anchor created directories through their retained handles."""
    if not created_dirs:
        return False
    incomplete = False
    for path in sorted(
        created_dirs, key=lambda value: value.count(os.sep), reverse=True
    ):
        key = _normal_path(path)
        held = records.pop(key, None)
        if held is None:
            incomplete = True
            continue
        handle, expected = held
        if not _delete_windows_directory_record_bound(path, handle, expected):
            incomplete = True
        _close_windows_handle(handle)
    for handle, _expected in records.values():
        _close_windows_handle(handle)
        incomplete = True
    records.clear()
    return incomplete


class _DirectoryAnchors:
    """Retain verified destination directory identities for the transaction."""

    def __init__(
        self,
        sessions_root: str,
        plan: RestorePlan,
        created_dirs: list[str],
        created_windows_dirs: dict[
            str, tuple[object, tuple[int, int, bytes, str]]
        ] | None = None,
    ):
        self.sessions_root = os.path.abspath(sessions_root)
        self.created_dir_keys = {_normal_path(path) for path in created_dirs}
        paths = {self.sessions_root}
        for entry in plan.entries:
            current = os.path.dirname(
                _contained_target(self.sessions_root, entry.target_relative)
            )
            while True:
                paths.add(current)
                if _normal_path(current) == _normal_path(self.sessions_root):
                    break
                current = os.path.dirname(current)
        self.paths = tuple(sorted(paths, key=lambda value: (value.count(os.sep), value)))
        self.records = {}
        try:
            if os.name == "nt":
                for path in self.paths:
                    key = _normal_path(path)
                    if key in self.created_dir_keys:
                        if created_windows_dirs is None or key not in created_windows_dirs:
                            raise RestoreRefusal(
                                "created directory has no retained object anchor"
                            )
                        handle, record = created_windows_dirs.pop(key)
                    else:
                        handle, record = _windows_directory_record(path)
                    self.records[key] = (handle, record)
            else:
                required = all(hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW"))
                if (
                    not required
                    or os.link not in os.supports_dir_fd
                    or os.unlink not in os.supports_dir_fd
                    or os.stat not in os.supports_dir_fd
                ):
                    raise RestoreRefusal(
                        "platform cannot anchor destination directories safely"
                    )
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                for path in self.paths:
                    if _normal_path(path) == _normal_path(self.sessions_root):
                        descriptor = os.open(path, flags)
                    else:
                        parent_key = _normal_path(os.path.dirname(path))
                        if parent_key not in self.records:
                            raise RestoreRefusal(
                                "destination anchor chain is incomplete"
                            )
                        parent_fd, _parent_record = self.records[parent_key]
                        descriptor = os.open(
                            os.path.basename(path), flags, dir_fd=parent_fd
                        )
                    result = os.fstat(descriptor)
                    if not stat_module.S_ISDIR(result.st_mode):
                        os.close(descriptor)
                        raise RestoreRefusal("destination anchor is not a directory")
                    self.records[_normal_path(path)] = (
                        descriptor, (result.st_dev, result.st_ino, result.st_mode)
                    )
            self.verify()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        for handle, _record in list(self.records.values()):
            try:
                if os.name == "nt":
                    _close_windows_handle(handle)
                else:
                    os.close(handle)
            except OSError:
                pass
        self.records.clear()

    def verify(self) -> None:
        root_key = _normal_path(self.sessions_root)
        root_record = self.records.get(root_key)
        if root_record is None:
            raise RestoreRefusal("metadata root has no retained directory anchor")
        for path in self.paths:
            key = _normal_path(path)
            held, expected = self.records[key]
            if os.name == "nt":
                try:
                    probe, current = _windows_directory_record(
                        path, share_delete=True
                    )
                except OSError as exc:
                    raise RestoreRefusal(
                        "destination directory anchor cannot be revalidated"
                    ) from exc
                try:
                    if current != expected:
                        raise RestoreRefusal("destination directory anchor changed")
                    attributes, _volume, _index, final_path = current
                    if not attributes & 0x10 or attributes & 0x400:
                        raise RestoreRefusal("destination directory is a reparse point")
                    root_final = root_record[1][3]
                    try:
                        if os.path.commonpath((root_final, final_path)) != root_final:
                            raise RestoreRefusal("destination directory escapes metadata root")
                    except ValueError as exc:
                        raise RestoreRefusal("destination directory changed filesystem root") from exc
                finally:
                    _close_windows_handle(probe)
                # The original held handle remains open without FILE_SHARE_DELETE.
                del held
            else:
                held_stat = os.fstat(held)
                path_stat = os.stat(path, follow_symlinks=False)
                if (
                    stat_module.S_ISLNK(path_stat.st_mode)
                    or not stat_module.S_ISDIR(path_stat.st_mode)
                    or (held_stat.st_dev, held_stat.st_ino, held_stat.st_mode) != expected
                    or (path_stat.st_dev, path_stat.st_ino) != (held_stat.st_dev, held_stat.st_ino)
                ):
                    raise RestoreRefusal("destination directory anchor changed")

    def _parent_record(self, path: str):
        parent = _normal_path(os.path.dirname(os.path.abspath(path)))
        if parent not in self.records:
            raise RestoreRefusal("file is outside the anchored destination set")
        return self.records[parent]

    def stat_file(self, path: str) -> os.stat_result:
        if os.name == "nt":
            return os.stat(path, follow_symlinks=False)
        descriptor, _record = self._parent_record(path)
        return os.stat(os.path.basename(path), dir_fd=descriptor, follow_symlinks=False)

    def exists(self, path: str) -> bool:
        try:
            self.stat_file(path)
            return True
        except FileNotFoundError:
            return False

    def hash_file(self, path: str) -> tuple[int, str]:
        if os.name == "nt":
            result = os.stat(path, follow_symlinks=False)
            if not stat_module.S_ISREG(result.st_mode):
                raise RestoreRefusal("anchored file is not regular")
            return _sha256_file(path)
        parent_fd, _record = self._parent_record(path)
        flags = os.O_RDONLY | os.O_NOFOLLOW
        descriptor = os.open(os.path.basename(path), flags, dir_fd=parent_fd)
        try:
            result = os.fstat(descriptor)
            if not stat_module.S_ISREG(result.st_mode):
                raise RestoreRefusal("anchored file is not regular")
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = os.read(descriptor, HASH_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                digest.update(chunk)
            return total, digest.hexdigest()
        finally:
            os.close(descriptor)

    def identity(self, path: str) -> tuple[int, object]:
        if os.name == "nt":
            return _windows_file_identity(path)
        result = self.stat_file(path)
        return result.st_dev, result.st_ino

    def same_file(self, first: str, second: str) -> bool:
        return self.identity(first) == self.identity(second)

    def create_temp(self, parent: str) -> tuple[int, str]:
        if os.name == "nt":
            return tempfile.mkstemp(prefix=".r-", suffix="", dir=parent)
        parent_fd, _record = self.records[_normal_path(parent)]
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        for _attempt in range(128):
            name = ".r-" + secrets.token_hex(8)
            try:
                descriptor = os.open(name, flags, 0o600, dir_fd=parent_fd)
                return descriptor, os.path.join(parent, name)
            except FileExistsError:
                continue
        raise RestoreFailure("could not allocate a staged metadata file")

    def atomic_link(self, source: str, target: str) -> None:
        if os.name == "nt":
            os.link(source, target)
            return
        source_fd, _record = self._parent_record(source)
        target_fd, _record = self._parent_record(target)
        os.link(
            os.path.basename(source), os.path.basename(target),
            src_dir_fd=source_fd, dst_dir_fd=target_fd, follow_symlinks=False,
        )

    def unlink(self, path: str) -> None:
        if os.name == "nt":
            os.unlink(path)
            return
        parent_fd, _record = self._parent_record(path)
        os.unlink(os.path.basename(path), dir_fd=parent_fd)

    def _delete_created_directory_bound(self, path: str) -> bool:
        """Delete one empty Windows directory through its retained handle."""
        if os.name != "nt":
            return False
        key = _normal_path(path)
        record = self.records.get(key)
        if key not in self.created_dir_keys or record is None:
            return False
        handle, expected = record
        if not _delete_windows_directory_record_bound(path, handle, expected):
            return False
        self.records.pop(key, None)
        self.created_dir_keys.discard(key)
        _close_windows_handle(handle)
        return True

    def cleanup_created_directories(self, created_dirs: list[str]) -> bool:
        """Object-bound cleanup, or an explicit safe incomplete rollback."""
        if not created_dirs:
            return False
        if os.name != "nt":
            # There is no portable inode-bound conditional rmdir primitive.
            return True
        incomplete = False
        try:
            self.verify()
        except (OSError, RestoreRefusal):
            return True
        for path in sorted(
            created_dirs, key=lambda value: value.count(os.sep), reverse=True
        ):
            if not self._delete_created_directory_bound(path):
                incomplete = True
        return incomplete


def _validate_metadata_payload(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
) -> tuple[int, str]:
    """Validate one bounded UTF-8 metadata JSON object without exposing it."""
    if info.file_size > MAX_METADATA_FILE_BYTES:
        raise RestoreRefusal("metadata payload exceeds the bounded size limit")
    try:
        raw = archive.read(info)
        if len(raw) != info.file_size:
            raise RestoreRefusal("metadata payload size validation failed")
        validate_metadata_payload_bytes(raw)
    except RestoreRefusal:
        raise
    except MetadataArchiveFormatError as exc:
        raise RestoreRefusal(str(exc)) from exc
    except OSError as exc:
        raise RestoreRefusal("metadata payload cannot be read") from exc
    return len(raw), hashlib.sha256(raw).hexdigest()


def _load_manifest(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    max_manifest_bytes: int,
) -> dict:
    if info.file_size > max_manifest_bytes:
        raise RestoreRefusal("manifest exceeds the bounded size limit")
    try:
        raw = archive.read(info)
        manifest = _strict_json_loads(
            raw, "manifest is not valid bounded strict UTF-8 JSON"
        )
    except RestoreRefusal:
        raise
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise RestoreRefusal("manifest is not valid bounded UTF-8 JSON") from exc
    if not isinstance(manifest, dict):
        raise RestoreRefusal("manifest root must be an object")
    return manifest


def _validate_archive_envelope(
    archive: zipfile.ZipFile, max_files: int, max_bytes: int
) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > max_files + 1:
        raise RestoreRefusal("archive exceeds the file-count cap")
    if sum(info.file_size for info in infos) > max_bytes:
        raise RestoreRefusal("archive exceeds the uncompressed-byte cap")

    names: dict[str, zipfile.ZipInfo] = {}
    folded = set()
    for info in infos:
        if info.is_dir() or _is_symlink(info) or not _safe_archive_path(info.filename):
            raise RestoreRefusal("archive contains an unsafe or non-file entry")
        folded_name = _filesystem_key(info.filename)
        if info.filename in names or folded_name in folded:
            raise RestoreRefusal("archive contains duplicate or colliding entries")
        names[info.filename] = info
        folded.add(folded_name)

    if any(name.casefold() == MANIFEST_NAME for name in names) and MANIFEST_NAME not in names:
        raise RestoreRefusal("archive uses a non-canonical reserved manifest name")
    payload_count = len(infos) - (1 if MANIFEST_NAME in names else 0)
    if payload_count > max_files:
        raise RestoreRefusal("archive exceeds the file-count cap")

    bad_crc = archive.testzip()
    if bad_crc is not None:
        raise RestoreRefusal("archive CRC validation failed")
    return names


def _validate_v2(
    archive: zipfile.ZipFile,
    names: dict[str, zipfile.ZipInfo],
    max_manifest_bytes: int,
) -> tuple[int, tuple[RestoreEntry, ...]]:
    manifest_info = names.get(MANIFEST_NAME)
    if manifest_info is None:
        raise RestoreRefusal("layout v2 manifest is missing")
    manifest = _load_manifest(archive, manifest_info, max_manifest_bytes)
    archive_sizes = {
        name: info.file_size for name, info in names.items() if name != MANIFEST_NAME
    }
    try:
        validated = validate_v2_metadata_manifest(manifest, archive_sizes)
    except MetadataArchiveFormatError as exc:
        raise RestoreRefusal(str(exc)) from exc

    identities = sorted(
        (pair.account_uuid, pair.organisation_uuid) for pair in validated.pairs
    )
    labels = {
        identity: "pair-{:02d}".format(index)
        for index, identity in enumerate(identities, start=1)
    }
    entries = []
    for record in validated.files:
        info = names[record.archive_path]
        actual_size, actual_digest = _validate_metadata_payload(archive, info)
        if actual_size != record.size or actual_digest != record.sha256:
            raise RestoreRefusal("archive entry hash validation failed")
        identity = (record.account_uuid, record.organisation_uuid)
        entries.append(RestoreEntry(
            archive_path=record.archive_path,
            target_relative=record.archive_path,
            pair_label=labels[identity],
            file_name=PurePosixPath(record.archive_path).name,
            size=record.size,
            sha256=record.sha256,
        ))
    return len(validated.pairs), tuple(entries)


def _validate_legacy(
    archive: zipfile.ZipFile,
    names: dict[str, zipfile.ZipInfo],
    account: str | None,
    organisation: str | None,
) -> tuple[int, tuple[RestoreEntry, ...]]:
    if not account or not organisation:
        raise RestoreRefusal(
            "legacy archives require explicit target account and organisation UUIDs"
        )
    if not _canonical_uuid_like(account) or not _canonical_uuid_like(organisation):
        raise RestoreRefusal("legacy target pair must contain canonical UUIDs")
    if not names:
        raise RestoreRefusal("legacy archive contains no metadata files")
    entries = []
    for archive_path, info in names.items():
        path = PurePosixPath(archive_path)
        if len(path.parts) != 1 or not _metadata_filename(archive_path):
            raise RestoreRefusal("legacy archive entries must be safe flat metadata files")
        size, digest = _validate_metadata_payload(archive, info)
        if size != info.file_size:
            raise RestoreRefusal("legacy archive entry size validation failed")
        entries.append(RestoreEntry(
            archive_path=archive_path,
            target_relative=account + "/" + organisation + "/" + archive_path,
            pair_label="pair-01",
            file_name=archive_path,
            size=size,
            sha256=digest,
        ))
    return 1, tuple(sorted(entries, key=lambda item: item.target_relative))


def validate_archive(
    archive_source,
    *,
    target_account: str | None = None,
    target_organisation: str | None = None,
    max_files: int = MAX_ARCHIVE_FILES,
    max_bytes: int = MAX_UNCOMPRESSED_BYTES,
    max_manifest_bytes: int = MAX_MANIFEST_BYTES,
) -> tuple[str, int, tuple[RestoreEntry, ...]]:
    if not 1 <= max_files <= MAX_ARCHIVE_FILES:
        raise RestoreRefusal("archive file cap is outside the allowed range")
    if not 1 <= max_bytes <= MAX_UNCOMPRESSED_BYTES:
        raise RestoreRefusal("archive byte cap is outside the allowed range")
    if not 1 <= max_manifest_bytes <= MAX_MANIFEST_BYTES:
        raise RestoreRefusal("manifest byte cap is outside the allowed range")
    try:
        if hasattr(archive_source, "seek"):
            archive_source.seek(0)
        with zipfile.ZipFile(archive_source, "r") as archive:
            names = _validate_archive_envelope(archive, max_files, max_bytes)
            if MANIFEST_NAME in names:
                if target_account is not None or target_organisation is not None:
                    raise RestoreRefusal(
                        "target pair arguments are only valid for legacy archives"
                    )
                pair_count, entries = _validate_v2(
                    archive, names, max_manifest_bytes
                )
                return "v2", pair_count, entries
            pair_count, entries = _validate_legacy(
                archive, names, target_account, target_organisation
            )
            return "legacy", pair_count, entries
    except RestoreRefusal:
        raise
    except (
        OSError,
        RuntimeError,
        EOFError,
        NotImplementedError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ) as exc:
        raise RestoreRefusal("archive cannot be read as a valid zip") from exc


def _contained_target(root: str, relative: str) -> str:
    target = os.path.abspath(os.path.join(root, *PurePosixPath(relative).parts))
    root_abs = os.path.abspath(root)
    if os.path.lexists(root_abs):
        if os.path.islink(root_abs) or not os.path.isdir(root_abs):
            raise RestoreRefusal("metadata root is not a safe directory")
        if os.path.normcase(os.path.realpath(root_abs)) != os.path.normcase(root_abs):
            raise RestoreRefusal("metadata root redirects to another path")
    try:
        if os.path.commonpath((root_abs, target)) != root_abs:
            raise RestoreRefusal("restore target escapes the metadata root")
    except ValueError as exc:
        raise RestoreRefusal("restore target is on an unexpected filesystem root") from exc

    current = root_abs
    for part in PurePosixPath(relative).parts[:-1]:
        current = os.path.join(current, part)
        if os.path.lexists(current):
            if os.path.islink(current) or not os.path.isdir(current):
                raise RestoreRefusal("restore target has an unsafe parent")
            real_root = os.path.realpath(root_abs)
            real_current = os.path.realpath(current)
            try:
                if os.path.commonpath((real_root, real_current)) != real_root:
                    raise RestoreRefusal("restore target parent redirects outside the metadata root")
            except ValueError as exc:
                raise RestoreRefusal("restore target parent is on another filesystem root") from exc
    return target


def build_plan(
    sessions_root: str,
    layout: str,
    pair_count: int,
    entries: tuple[RestoreEntry, ...],
) -> RestorePlan:
    eligible = []
    identical = []
    identical_file_ids = []
    for entry in entries:
        target = _contained_target(sessions_root, entry.target_relative)
        if os.path.lexists(target):
            if os.path.islink(target) or not os.path.isfile(target):
                raise RestoreRefusal("a restore target is not a regular file")
            size, digest = _sha256_file(target)
            if size == entry.size and digest == entry.sha256:
                identical.append(entry)
                identical_file_ids.append((entry, _file_identity(target)))
            else:
                raise RestoreRefusal(
                    "a differing live metadata file blocks the entire restore"
                )
        else:
            eligible.append(entry)
    return RestorePlan(
        layout=layout,
        pair_count=pair_count,
        entries=entries,
        eligible=tuple(eligible),
        identical=tuple(identical),
        identical_file_ids=tuple(identical_file_ids),
    )


def _revalidate_transaction_targets(
    sessions_root: str,
    plan: RestorePlan,
    anchors: _DirectoryAnchors,
    staged_by_entry: dict[RestoreEntry, str],
    created_entries: set[RestoreEntry],
) -> None:
    """Validate absent, identical, and transaction-created targets together."""
    identical_ids = dict(plan.identical_file_ids)
    for entry in plan.eligible:
        target = _contained_target(sessions_root, entry.target_relative)
        if entry not in created_entries:
            if anchors.exists(target):
                raise RestoreRefusal(
                    "an absent restore target changed during publication"
                )
            continue
        temporary = staged_by_entry.get(entry)
        if (
            not temporary
            or not anchors.exists(temporary)
            or not anchors.exists(target)
        ):
            raise RestoreRefusal("a created restore target changed identity")
        try:
            same_file = anchors.same_file(temporary, target)
        except OSError:
            same_file = False
        if not same_file:
            raise RestoreRefusal("a created restore target changed identity")
        size, digest = anchors.hash_file(target)
        if size != entry.size or digest != entry.sha256:
            raise RestoreRefusal("a created restore target changed contents")

    for entry in plan.identical:
        target = _contained_target(sessions_root, entry.target_relative)
        if not anchors.exists(target):
            raise RestoreRefusal(
                "an identical restore target changed during publication"
            )
        if anchors.identity(target) != identical_ids[entry]:
            raise RestoreRefusal(
                "an identical restore target changed identity during publication"
            )
        size, digest = anchors.hash_file(target)
        if size != entry.size or digest != entry.sha256:
            raise RestoreRefusal(
                "an identical restore target changed during publication"
            )


def _revalidate_staged_files(
    plan: RestorePlan,
    anchors: _DirectoryAnchors,
    staged_by_entry: dict[RestoreEntry, str],
    staged_file_ids: dict[RestoreEntry, tuple[int, object]],
) -> None:
    for entry in plan.eligible:
        temporary = staged_by_entry.get(entry)
        if (
            not temporary
            or not anchors.exists(temporary)
            or anchors.identity(temporary) != staged_file_ids.get(entry)
            or anchors.hash_file(temporary) != (entry.size, entry.sha256)
        ):
            raise RestoreRefusal("a verified staged file changed before publication")


def _ensure_parent(
    parent: str,
    sessions_root: str,
    created_dirs: list[str],
    created_windows_dirs: dict[
        str, tuple[object, tuple[int, int, bytes, str]]
    ],
) -> None:
    missing = []
    current = parent
    root_abs = os.path.abspath(sessions_root)
    while not os.path.exists(current):
        missing.append(current)
        next_current = os.path.dirname(current)
        if next_current == current:
            raise RestoreFailure("could not establish a contained target directory")
        current = next_current
    if os.path.islink(current) or not os.path.isdir(current):
        raise RestoreFailure("target parent is not a safe directory")
    if os.path.normcase(os.path.realpath(current)) != os.path.normcase(os.path.abspath(current)):
        raise RestoreFailure("target parent redirects to another path")
    try:
        if os.path.commonpath((root_abs, os.path.abspath(parent))) != root_abs:
            raise RestoreFailure("target parent escapes the metadata root")
    except ValueError as exc:
        raise RestoreFailure("target parent is on another filesystem root") from exc
    for directory in reversed(missing):
        os.mkdir(directory)
        created_dirs.append(directory)
        if os.name == "nt":
            handle = None
            try:
                handle, record = _windows_directory_record(
                    directory, delete_access=True
                )
                attributes, _volume, _file_id, final_path = record
                if (
                    not attributes & 0x10
                    or attributes & 0x400
                    or final_path != _normal_path(directory)
                ):
                    raise RestoreFailure(
                        "created destination directory cannot be object-bound"
                    )
                created_windows_dirs[_normal_path(directory)] = (handle, record)
                handle = None
            finally:
                if handle is not None:
                    _close_windows_handle(handle)


def _created_pair_identities(
    sessions_root: str,
    plan: RestorePlan,
    created_dirs: list[str],
) -> set[tuple[str, str]]:
    created = {os.path.normcase(os.path.abspath(path)) for path in created_dirs}
    identities = set()
    for entry in plan.eligible:
        parts = PurePosixPath(entry.target_relative).parts
        pair_path = os.path.join(sessions_root, parts[0], parts[1])
        if os.path.normcase(os.path.abspath(pair_path)) in created:
            identities.add((parts[0], parts[1]))
    return identities


def apply_plan(
    archive_guard: _ArchiveSourceGuard,
    sessions_root: str,
    plan: RestorePlan,
    transaction_check: Callable[
        [set[tuple[str, str]], list[str], set[str], set[str]], None
    ],
) -> int:
    staged: list[tuple[RestoreEntry, str, str]] = []
    staged_by_entry: dict[RestoreEntry, str] = {}
    staged_file_ids: dict[RestoreEntry, tuple[int, object]] = {}
    created_entries: set[RestoreEntry] = set()
    target_leases: dict[RestoreEntry, _WindowsTargetLease] = {}
    created_dirs: list[str] = []
    created_windows_dirs: dict[
        str, tuple[object, tuple[int, int, bytes, str]]
    ] = {}
    anchors = None
    try:
        # Complete parent setup first, then anchor every destination directory
        # before creating sibling staging files or publishing metadata names.
        for entry in plan.eligible:
            target = _contained_target(sessions_root, entry.target_relative)
            _ensure_parent(
                os.path.dirname(target), sessions_root, created_dirs,
                created_windows_dirs,
            )

        try:
            anchors = _DirectoryAnchors(
                sessions_root, plan, created_dirs, created_windows_dirs
            )
        except OSError as exc:
            raise RestoreRefusal(
                "platform cannot anchor destination directories safely"
            ) from exc
        archive_guard.verify()
        archive_guard.handle.seek(0)
        with zipfile.ZipFile(archive_guard.handle, "r") as archive:
            for entry in plan.eligible:
                target = _contained_target(sessions_root, entry.target_relative)
                fd, temporary = anchors.create_temp(os.path.dirname(target))
                try:
                    with os.fdopen(fd, "wb") as output, archive.open(entry.archive_path) as source:
                        digest = hashlib.sha256()
                        total = 0
                        while True:
                            chunk = source.read(HASH_CHUNK_SIZE)
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > entry.size:
                                raise RestoreFailure("staged entry exceeded its verified size")
                            output.write(chunk)
                            digest.update(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    if total != entry.size or digest.hexdigest() != entry.sha256:
                        raise RestoreFailure("staged entry failed integrity verification")
                except Exception:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    if anchors.exists(temporary):
                        anchors.unlink(temporary)
                    raise
                staged.append((entry, temporary, target))
                staged_by_entry[entry] = temporary
                staged_file_ids[entry] = anchors.identity(temporary)

        created_pairs = _created_pair_identities(sessions_root, plan, created_dirs)
        staged_paths = set(staged_by_entry.values())

        def gate() -> None:
            anchors.verify()
            _revalidate_staged_files(
                plan, anchors, staged_by_entry, staged_file_ids
            )
            _revalidate_transaction_targets(
                sessions_root, plan, anchors, staged_by_entry, created_entries
            )
            archive_guard.verify()
            created_targets = {
                _contained_target(sessions_root, entry.target_relative)
                for entry in created_entries
            }
            transaction_check(
                created_pairs, created_dirs, staged_paths, created_targets
            )

        def bind_created_target(
            entry: RestoreEntry, temporary: str, target: str
        ) -> None:
            """Record a create and immediately bind its Windows object lease."""
            created_entries.add(entry)
            if os.name != "nt":
                return
            if entry in target_leases:
                return
            try:
                target_leases[entry] = _WindowsTargetLease(
                    target, staged_file_ids[entry]
                )
            except OSError as exc:
                raise RestoreRefusal(
                    "created target could not be retained for safe rollback"
                ) from exc

        def publication_became_visible(
            entry: RestoreEntry, temporary: str, target: str
        ) -> bool:
            try:
                return (
                    anchors.exists(target)
                    and anchors.same_file(temporary, target)
                    and anchors.hash_file(target) == (entry.size, entry.sha256)
                )
            except (OSError, RestoreRefusal):
                return False

        # This is after all parent creation and staging and immediately before
        # the first metadata create. It also covers an all-identical plan.
        gate()

        for entry, temporary, target in staged:
            # Fresh full gate immediately before every atomic create.
            gate()
            try:
                anchors.atomic_link(temporary, target)
            except FileExistsError as exc:
                if publication_became_visible(entry, temporary, target):
                    bind_created_target(entry, temporary, target)
                    gate()
                raise RestoreRefusal(
                    "a restore target appeared during atomic creation"
                ) from exc
            except OSError as exc:
                # A platform wrapper can report failure after the link became
                # visible. Bind that exact object into rollback before refusing.
                if publication_became_visible(entry, temporary, target):
                    bind_created_target(entry, temporary, target)
                    gate()
                raise RestoreRefusal(
                    "atomic create-no-replace is unavailable or failed"
                ) from exc
            bind_created_target(entry, temporary, target)
            # Detect parent, archive, diagnosis, target, or Desktop drift that
            # occurred inside the create call before another publication.
            gate()

        # Repeat all invariants after the final create, even though the loop's
        # post-create gate has just passed.
        gate()

        for lease in target_leases.values():
            lease.close()
        target_leases.clear()
        for _entry, temporary, _target in staged:
            anchors.unlink(temporary)
        anchors.close()
        anchors = None
        return len(created_entries)
    except Exception as exc:
        rollback_incomplete = False
        if anchors is not None:
            for entry, temporary, _target in reversed(staged):
                if entry not in created_entries:
                    continue
                if os.name != "nt":
                    # No portable inode-bound conditional unlink exists.
                    rollback_incomplete = True
                    continue
                lease = target_leases.get(entry)
                if lease is None:
                    rollback_incomplete = True
                    continue
                try:
                    lease.delete_bound(temporary, anchors, entry)
                except (OSError, RestoreRefusal):
                    rollback_incomplete = True

            for lease in target_leases.values():
                lease.close()
            target_leases.clear()

            for entry, temporary, _target in staged:
                try:
                    if not anchors.exists(temporary):
                        continue
                    if anchors.identity(temporary) != staged_file_ids.get(entry):
                        rollback_incomplete = True
                        continue
                    anchors.unlink(temporary)
                except (OSError, RestoreRefusal):
                    rollback_incomplete = True
            if anchors.cleanup_created_directories(created_dirs):
                rollback_incomplete = True
            anchors.close()
            anchors = None
        else:
            for lease in target_leases.values():
                lease.close()
            target_leases.clear()
            if os.name == "nt":
                if _cleanup_windows_created_directory_records(
                    created_windows_dirs, created_dirs
                ):
                    rollback_incomplete = True
            elif created_dirs:
                rollback_incomplete = True
        suffix = " (rollback incomplete)" if rollback_incomplete else ""
        if isinstance(exc, RestoreRefusal):
            raise RestoreRefusal(str(exc) + suffix) from exc
        if isinstance(exc, RestoreFailure):
            raise RestoreFailure(str(exc) + suffix) from exc
        raise RestoreFailure("restore publication failed" + suffix) from exc
    finally:
        if anchors is not None:
            anchors.close()
        for handle, _record in created_windows_dirs.values():
            _close_windows_handle(handle)
        created_windows_dirs.clear()


def _print_plan(
    archive_path: str,
    sessions_root: str,
    plan: RestorePlan,
    *,
    include_paths: bool,
    apply: bool,
) -> None:
    print("Claude Desktop Metadata Restore")
    print("-" * 60)
    print("Archive       : {}".format(os.path.basename(archive_path)))
    print("Layout        : {}".format(plan.layout))
    print("Pairs         : {}".format(plan.pair_count))
    print("Files         : {}".format(len(plan.entries)))
    print("Eligible      : {}".format(len(plan.eligible)))
    print("Identical     : {}".format(len(plan.identical)))
    print("Mode          : {}".format("APPLY" if apply else "DRY RUN"))
    print()
    for entry in plan.entries:
        state = "SKIP identical" if entry in plan.identical else "RESTORE"
        line = "  {}  {}  {}".format(entry.pair_label, state, entry.file_name)
        if include_paths:
            line += " -> " + _contained_target(sessions_root, entry.target_relative)
        print(line)


def _lock_path(appdata_dir: str) -> str:
    identity = hashlib.sha256(os.path.abspath(appdata_dir).encode("utf-8")).hexdigest()[:16]
    return os.path.join(
        tempfile.gettempdir(),
        "claude-code-session-recovery-locks",
        "restore-desktop-metadata-{}.lock".format(identity),
    )


def _guard_file_record(path: str, relative: str, budget: dict) -> tuple:
    before = os.stat(path, follow_symlinks=False)
    if not stat_module.S_ISREG(before.st_mode):
        raise RestoreRefusal("guarded state contains an unsupported file type")
    budget["files"] += 1
    budget["bytes"] += before.st_size
    if budget["files"] > MAX_GUARD_FILES or budget["bytes"] > MAX_GUARD_BYTES:
        raise RestoreRefusal("live-state transaction guard exceeds bounded limits")
    size, digest = _sha256_file(path)
    after = os.stat(path, follow_symlinks=False)
    if _stat_signature(before) != _stat_signature(after) or size != after.st_size:
        raise RestoreRefusal("live state changed while its transaction guard was built")
    return (
        "F", relative.replace(os.sep, "/"), before.st_dev, before.st_ino,
        before.st_size, before.st_mtime_ns, digest,
    )


def _guard_tree_records(
    root: str,
    *,
    excluded_files: set[str],
    excluded_dirs: set[str],
    transcript_only: bool,
    budget: dict,
) -> list[tuple]:
    if not os.path.lexists(root):
        return []

    def visit(path: str, relative: str) -> list[tuple]:
        key = _normal_path(path)
        result = os.stat(path, follow_symlinks=False)
        if stat_module.S_ISLNK(result.st_mode):
            if transcript_only and not relative.lower().endswith(".jsonl"):
                return []
            budget["files"] += 1
            if budget["files"] > MAX_GUARD_FILES:
                raise RestoreRefusal("live-state transaction guard exceeds bounded limits")
            return [("L", relative.replace(os.sep, "/"), os.readlink(path))]
        if stat_module.S_ISDIR(result.st_mode):
            children = []
            try:
                entries = sorted(os.scandir(path), key=lambda item: item.name)
            except OSError as exc:
                raise RestoreRefusal("live state cannot be enumerated safely") from exc
            for entry in entries:
                child_relative = os.path.join(relative, entry.name) if relative else entry.name
                children.extend(visit(entry.path, child_relative))
            after = os.stat(path, follow_symlinks=False)
            if _stat_signature(result) != _stat_signature(after):
                raise RestoreRefusal(
                    "live state changed while its transaction guard was built"
                )
            if key in excluded_dirs and not children:
                return []
            if transcript_only and not children:
                return []
            return [(
                "D", relative.replace(os.sep, "/"), result.st_dev, result.st_ino,
            )] + children
        if key in excluded_files:
            return []
        if transcript_only and not relative.lower().endswith(".jsonl"):
            return []
        return [_guard_file_record(path, relative, budget)]

    return visit(os.path.abspath(root), "")


def _live_state_fingerprint(
    sessions_root: str,
    projects_dir: str,
    *,
    excluded_files=(),
    excluded_dirs=(),
) -> str:
    budget = {"files": 0, "bytes": 0}
    excluded_file_keys = {_normal_path(path) for path in excluded_files}
    excluded_dir_keys = {_normal_path(path) for path in excluded_dirs}
    records = [
        ("metadata",) + record
        for record in _guard_tree_records(
            sessions_root,
            excluded_files=excluded_file_keys,
            excluded_dirs=excluded_dir_keys,
            transcript_only=False,
            budget=budget,
        )
    ]
    records.extend(
        ("transcript",) + record
        for record in _guard_tree_records(
            projects_dir,
            excluded_files=set(),
            excluded_dirs=set(),
            transcript_only=True,
            budget=budget,
        )
    )
    encoded = json.dumps(records, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class _LiveStateGuard:
    """Bind publication to the caller's diagnosis and all unrelated state."""

    def __init__(
        self,
        appdata_dir: str,
        projects_dir: str,
        sessions_root: str,
        expected_diagnosis_id: str,
        fixture_mode: bool,
    ):
        self.appdata_dir = appdata_dir
        self.projects_dir = projects_dir
        self.sessions_root = sessions_root
        self.expected_diagnosis_id = expected_diagnosis_id
        self.fixture_mode = fixture_mode
        snapshot = build_snapshot(
            appdata_dir,
            projects_dir,
            fixture_mode=fixture_mode,
            include_inventory_status=True,
        )
        refusal = _mutation_refusal_reason(snapshot, "v2")
        if refusal:
            raise RestoreRefusal(refusal)
        if make_diagnosis_id(snapshot) != expected_diagnosis_id:
            raise RestoreRefusal("diagnosis ID changed before transaction setup")
        try:
            self.fingerprint = _live_state_fingerprint(sessions_root, projects_dir)
        except OSError as exc:
            raise RestoreRefusal("live state cannot be fingerprinted safely") from exc

    def verify(
        self,
        created_pairs: set[tuple[str, str]],
        created_dirs: list[str],
        staged_paths: set[str],
        created_targets: set[str],
    ) -> None:
        if desktop_process_running():
            raise RestoreRefusal(
                "Claude Desktop started or its state became unknown during publication"
            )
        snapshot = build_snapshot(
            self.appdata_dir,
            self.projects_dir,
            fixture_mode=self.fixture_mode,
            excluded_metadata_paths=created_targets,
            excluded_metadata_pairs=created_pairs,
            include_inventory_status=True,
        )
        refusal = _mutation_refusal_reason(snapshot, "v2")
        if refusal:
            raise RestoreRefusal(refusal)
        if make_diagnosis_id(snapshot) != self.expected_diagnosis_id:
            raise RestoreRefusal("diagnosis state changed during publication")
        try:
            fingerprint = _live_state_fingerprint(
                self.sessions_root,
                self.projects_dir,
                excluded_files=staged_paths | created_targets,
                excluded_dirs=created_dirs,
            )
        except OSError as exc:
            raise RestoreRefusal("live state cannot be fingerprinted safely") from exc
        if fingerprint != self.fingerprint:
            raise RestoreRefusal("unrelated session state changed during publication")
        if desktop_process_running():
            raise RestoreRefusal(
                "Claude Desktop started or its state became unknown during publication"
            )


def _mutation_refusal_reason(snapshot: dict, layout: str) -> str | None:
    """Return the gate-4 reason that makes mutation unavailable, if any."""
    if layout != "v2":
        return "mutation requires a layout v2 manifest-backed archive"
    if not snapshot.get("_metadata_inventory_complete", False):
        return "live metadata inventory is incomplete"
    if not snapshot.get("_transcript_inventory_complete", False):
        return "live transcript inventory is incomplete"
    if snapshot.get("schema_version") != "recognised":
        return "live state schema is unrecognised"
    return None


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Restore a Claude Desktop metadata backup safely (dry-run by default)."
    )
    parser.add_argument("archive", metavar="ARCHIVE")
    parser.add_argument("--diagnosis-id", required=True, metavar="HEX")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--state", metavar="DIR", help=argparse.SUPPRESS)
    parser.add_argument("--target-account-uuid", metavar="UUID")
    parser.add_argument("--target-organisation-uuid", metavar="UUID")
    parser.add_argument("--include-paths", action="store_true")
    parser.add_argument("--max-files", type=int, default=MAX_ARCHIVE_FILES, help=argparse.SUPPRESS)
    parser.add_argument("--max-uncompressed-bytes", type=int, default=MAX_UNCOMPRESSED_BYTES, help=argparse.SUPPRESS)
    parser.add_argument("--max-manifest-bytes", type=int, default=MAX_MANIFEST_BYTES, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if bool(args.target_account_uuid) != bool(args.target_organisation_uuid):
        parser.error("legacy targets require both UUID arguments")

    live_appdata, live_projects = default_claude_paths()
    appdata_dir, projects_dir = resolve_state_paths(
        args.state, live_appdata, live_projects
    )
    sessions_root = os.path.join(appdata_dir, "claude-code-sessions")
    lock_file = _lock_path(appdata_dir)
    acquired = False
    try:
        try:
            acquire_lock(lock_file, "restore_claude_metadata_backup")
        except SystemExit as exc:
            raise RestoreRefusal(
                "restore lock is held or its owner cannot be verified"
            ) from exc
        acquired = True
        snapshot = build_snapshot(
            appdata_dir,
            projects_dir,
            fixture_mode=bool(args.state),
            include_inventory_status=True,
        )
        fresh_id = make_diagnosis_id(snapshot)
        if args.diagnosis_id != fresh_id:
            raise RestoreRefusal(
                "diagnosis ID is stale; run diagnose.py again before restoring"
            )

        layout, pair_count, entries = validate_archive(
            args.archive,
            target_account=args.target_account_uuid,
            target_organisation=args.target_organisation_uuid,
            max_files=args.max_files,
            max_bytes=args.max_uncompressed_bytes,
            max_manifest_bytes=args.max_manifest_bytes,
        )
        plan = build_plan(sessions_root, layout, pair_count, entries)
        _print_plan(
            args.archive, sessions_root, plan,
            include_paths=args.include_paths, apply=args.apply,
        )
        mutation_refusal = _mutation_refusal_reason(snapshot, layout)
        if not args.apply:
            print()
            print("DRY RUN — no files or directories were changed.")
            if mutation_refusal:
                print("Apply unavailable: {}.".format(mutation_refusal))
            else:
                print("Re-run with --apply after fully quitting Claude Desktop.")
            return 0
        if mutation_refusal:
            raise RestoreRefusal(mutation_refusal)
        if desktop_process_running():
            raise RestoreRefusal(
                "Claude Desktop is running or its state is unknown; quit it fully"
            )
        try:
            archive_guard_context = _ArchiveSourceGuard(args.archive)
        except OSError as exc:
            raise RestoreRefusal("archive source cannot be opened safely") from exc
        with archive_guard_context as archive_guard:
            refreshed_layout, refreshed_pair_count, refreshed_entries = validate_archive(
                archive_guard.handle,
                target_account=args.target_account_uuid,
                target_organisation=args.target_organisation_uuid,
                max_files=args.max_files,
                max_bytes=args.max_uncompressed_bytes,
                max_manifest_bytes=args.max_manifest_bytes,
            )
            archive_guard.verify()
            if (
                refreshed_layout != layout
                or refreshed_pair_count != pair_count
                or refreshed_entries != entries
            ):
                raise RestoreRefusal("archive changed after the restore plan was built")
            plan = build_plan(
                sessions_root, refreshed_layout, refreshed_pair_count, refreshed_entries
            )
            live_guard = _LiveStateGuard(
                appdata_dir,
                projects_dir,
                sessions_root,
                args.diagnosis_id,
                bool(args.state),
            )
            restored = apply_plan(
                archive_guard, sessions_root, plan, live_guard.verify
            )
        print()
        if not plan.eligible:
            print("Nothing to restore; every target is already byte-identical.")
        else:
            print("Restored      : {}".format(restored))
            print("Skipped       : {}".format(len(plan.identical)))
        return 0
    except RestoreRefusal as exc:
        print("REFUSED: {}".format(exc))
        return 3
    except RestoreFailure as exc:
        print("ERROR: {}".format(exc))
        return 4
    except OSError as exc:
        print("ERROR: restore I/O failed: {}".format(exc.__class__.__name__))
        return 4
    finally:
        if acquired:
            release_lock(lock_file)
        try:
            os.rmdir(os.path.dirname(lock_file))
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(run())
