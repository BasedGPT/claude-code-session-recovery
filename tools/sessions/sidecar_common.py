"""Shared bounded and privacy-safe helpers for read-only intelligence sidecars."""

import hashlib
import json
import os
import stat


def _stat_path(path, *, follow_symlinks=True):
    """Test seam for portable access-error simulation."""
    return os.stat(path, follow_symlinks=follow_symlinks)


def _scandir_path(path):
    """Test seam for portable directory-access error simulation."""
    return os.scandir(path)


def opaque_id(namespace, value):
    """Return a deterministic identifier without exposing the source value."""
    payload = "{}\0{}".format(namespace, value).encode("utf-8", errors="replace")
    return "{}-{}".format(namespace, hashlib.sha256(payload).hexdigest()[:16])


class ScanState:
    """Track bounded-scan completeness without printing private exception data."""

    def __init__(self, max_errors=100):
        self.partial = False
        self.errors = []
        self.max_errors = max_errors
        self.error_count = 0

    def error(self, code, subject=None):
        self.partial = True
        self.error_count += 1
        if len(self.errors) >= self.max_errors:
            return
        item = {"code": code}
        if subject is not None:
            item["subject"] = subject
        self.errors.append(item)

    def cap(self, code, subject=None):
        self.error(code, subject)

    def fields(self):
        return {
            "status": "partial" if self.partial else "complete",
            "partial": self.partial,
            "error_count": self.error_count,
            "reported_error_count": len(self.errors),
            "errors": list(self.errors),
        }


def path_kind(path, state, *, subject_namespace, follow_symlinks=True):
    """Return directory/file/other/absent/error without suppressing access errors."""
    subject = opaque_id(subject_namespace, path)
    try:
        metadata = _stat_path(path, follow_symlinks=follow_symlinks)
    except FileNotFoundError:
        return "absent"
    except OSError:
        state.error("path_access_error", subject)
        return "error"
    if stat.S_ISDIR(metadata.st_mode):
        return "directory"
    if stat.S_ISREG(metadata.st_mode):
        return "file"
    return "other"


def expected_path_kind(path, state, *, expected, subject_namespace,
                       follow_symlinks=True):
    """Probe a path and mark an existing unexpected type as an explicit error."""
    kind = path_kind(
        path,
        state,
        subject_namespace=subject_namespace,
        follow_symlinks=follow_symlinks,
    )
    if kind in ("absent", "error") or kind == expected:
        return kind
    state.error("path_type_mismatch", opaque_id(subject_namespace, path))
    return "error"


def entry_kind(entry, state, *, subject_namespace, follow_symlinks=False):
    """Return an entry type while surfacing races and access failures."""
    subject = opaque_id(subject_namespace, entry.name)
    try:
        metadata = entry.stat(follow_symlinks=follow_symlinks)
    except FileNotFoundError:
        state.error("entry_disappeared", subject)
        return "error"
    except OSError:
        state.error("entry_access_error", subject)
        return "error"
    if stat.S_ISDIR(metadata.st_mode):
        return "directory"
    if stat.S_ISREG(metadata.st_mode):
        return "file"
    return "other"


def path_size(path, state, *, subject_namespace, follow_symlinks=True):
    """Return a file size while surfacing disappearance and access errors."""
    subject = opaque_id(subject_namespace, path)
    try:
        return _stat_path(path, follow_symlinks=follow_symlinks).st_size
    except FileNotFoundError:
        state.error("path_disappeared", subject)
    except OSError:
        state.error("path_access_error", subject)
    return None


def file_stat_identity(path, state, *, subject_namespace, required=False,
                       follow_symlinks=True):
    """Return ``(status, identity)`` for stable-copy comparison.

    Identity contains device/inode, size, and nanosecond mtime. A missing
    optional file is not an error; a missing required file or any access/type
    failure is explicit and privacy-safe.
    """
    subject = opaque_id(subject_namespace, path)
    try:
        metadata = _stat_path(path, follow_symlinks=follow_symlinks)
    except FileNotFoundError:
        if required:
            state.error("path_disappeared", subject)
            return "error", None
        return "absent", None
    except OSError:
        state.error("path_access_error", subject)
        return "error", None
    if not stat.S_ISREG(metadata.st_mode):
        state.error("path_type_mismatch", subject)
        return "error", None
    mtime_ns = getattr(
        metadata, "st_mtime_ns", int(metadata.st_mtime * 1_000_000_000)
    )
    identity = (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        mtime_ns,
    )
    return "file", identity


def entry_size(entry, state, *, subject_namespace, follow_symlinks=False):
    """Return an entry size while surfacing disappearance and access errors."""
    subject = opaque_id(subject_namespace, entry.name)
    try:
        return entry.stat(follow_symlinks=follow_symlinks).st_size
    except FileNotFoundError:
        state.error("entry_disappeared", subject)
    except OSError:
        state.error("entry_access_error", subject)
    return None


def bounded_directory_entries(path, state, *, cap, subject_namespace):
    """Return sorted direct entries, bounded by ``cap`` and privacy-safe errors."""
    try:
        with _scandir_path(path) as iterator:
            entries = []
            for entry in iterator:
                entries.append(entry)
                if len(entries) > cap:
                    state.cap(
                        "directory_entry_cap_reached",
                        opaque_id(subject_namespace, path),
                    )
                    # Avoid a filesystem-order-dependent partial result.
                    return []
    except OSError:
        state.error("directory_unreadable", opaque_id(subject_namespace, path))
        return []
    return sorted(entries, key=lambda entry: entry.name)


def iter_bounded_jsonl(path, state, *, subject, max_lines, max_line_bytes,
                       max_file_bytes):
    """Yield object JSON records while bounding retained and scanned bytes.

    Oversized lines are drained in fixed-size chunks and never accumulated.
    Reaching any cap marks the scan partial. Malformed rows are surfaced as a
    countable error without retaining or printing their content.
    """
    line_count = 0
    bytes_scanned = 0
    try:
        handle = open(path, "rb")
    except OSError:
        state.error("file_unreadable", subject)
        return

    with handle:
        while True:
            if line_count >= max_lines:
                if handle.read(1):
                    state.cap("line_cap_reached", subject)
                return
            if bytes_scanned >= max_file_bytes:
                if handle.read(1):
                    state.cap("file_byte_cap_reached", subject)
                return

            remaining = min(max_line_bytes + 1, max_file_bytes - bytes_scanned)
            if remaining <= 0:
                state.cap("file_byte_cap_reached", subject)
                return
            raw = handle.readline(remaining)
            if not raw:
                return
            bytes_scanned += len(raw)
            line_count += 1

            oversized = len(raw) > max_line_bytes or (
                not raw.endswith(b"\n") and len(raw) == remaining
            )
            if oversized:
                state.error("line_byte_cap_reached", subject)
                while raw and not raw.endswith(b"\n"):
                    if bytes_scanned >= max_file_bytes:
                        state.cap("file_byte_cap_reached", subject)
                        return
                    chunk_size = min(65536, max_file_bytes - bytes_scanned)
                    raw = handle.readline(chunk_size)
                    bytes_scanned += len(raw)
                continue

            try:
                text = raw.decode("utf-8")
                record = json.loads(text)
            except (UnicodeDecodeError, json.JSONDecodeError):
                state.error("record_invalid", subject)
                continue
            if isinstance(record, dict):
                yield record
            else:
                state.error("record_not_object", subject)


def load_bounded_json_object(path, state, *, subject, max_bytes):
    """Load one bounded JSON object, returning ``None`` on any safe failure."""
    try:
        size = os.path.getsize(path)
        if size > max_bytes:
            state.cap("file_byte_cap_reached", subject)
            return None
        with open(path, "rb") as handle:
            raw = handle.read(max_bytes + 1)
    except OSError:
        state.error("file_unreadable", subject)
        return None
    if len(raw) > max_bytes:
        state.cap("file_byte_cap_reached", subject)
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        state.error("record_invalid", subject)
        return None
    if not isinstance(value, dict):
        state.error("record_not_object", subject)
        return None
    return value


def write_json(payload):
    print(json.dumps(payload, sort_keys=True, indent=2))
