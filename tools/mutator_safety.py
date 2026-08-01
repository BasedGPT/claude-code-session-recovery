"""Policy-free safety mechanics shared by session-state mutators.

This module deliberately does not choose what a mutator may repair, where its
backups belong, or how it reports a failure.  Those decisions remain local to
each executable.  It only provides the repeated mechanics needed to perform
the already-approved mutation safely.
"""
import json
import os
import shutil
import tempfile

from platform_support import desktop_process_running
from session_state import build_snapshot, make_diagnosis_id


def diagnosis_mode(diagnosis_id, force_diagnosis_id, apply):
    """Return ``(force_mode, error_kind)`` for the common CLI safety rule.

    ``error_kind`` is ``"missing"`` when neither a token nor audit-only force
    mode was supplied, ``"force_apply"`` when force mode is paired with apply,
    and ``None`` when the invocation may proceed to a fresh token check.
    Callers own their established human-facing error messages and exit codes.
    """
    force_mode = force_diagnosis_id == "audit-only"
    if not diagnosis_id and not force_mode:
        return force_mode, "missing"
    if apply and force_mode:
        return force_mode, "force_apply"
    return force_mode, None


def resolve_state_paths(state_path, live_appdata_claude_dir, live_projects_dir):
    """Return app-data and projects paths for fixture or live execution."""
    if state_path:
        state_abs = os.path.abspath(state_path)
        return (
            os.path.join(state_abs, "appdata", "Claude"),
            os.path.join(state_abs, "projects"),
        )
    return live_appdata_claude_dir, live_projects_dir


def current_snapshot_and_diagnosis_id(appdata_claude_dir, projects_dir, fixture_mode):
    """Build the fresh state snapshot and its deterministic diagnosis token."""
    snapshot = build_snapshot(
        appdata_claude_dir, projects_dir, fixture_mode=fixture_mode,
    )
    return snapshot, make_diagnosis_id(snapshot)


def verified_backup(source_path, backup_path):
    """Publish a byte-verified backup without exposing a partial final file."""
    directory = os.path.dirname(os.path.abspath(backup_path))
    os.makedirs(directory, exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(
        prefix="." + os.path.basename(backup_path) + ".",
        suffix=".tmp",
        dir=directory,
    )
    os.close(fd)
    try:
        shutil.copy2(source_path, temporary_path)
        with open(temporary_path, "r+b") as temporary:
            temporary.flush()
            os.fsync(temporary.fileno())
        _verify_same_bytes(source_path, temporary_path)
        os.replace(temporary_path, backup_path)
        return backup_path
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)


def _verify_same_bytes(source_path, candidate_path):
    """Raise if two paths differ in size or contents."""
    if os.path.getsize(source_path) != os.path.getsize(candidate_path):
        raise OSError("backup verification failed: sizes differ")
    with open(source_path, "rb") as source, open(candidate_path, "rb") as backup:
        while True:
            source_chunk = source.read(1024 * 1024)
            backup_chunk = backup.read(1024 * 1024)
            if source_chunk != backup_chunk:
                raise OSError("backup verification failed: contents differ")
            if not source_chunk:
                return


def write_json_in_place(path, payload, *, indent=2, trailing_newline=False):
    """Write JSON directly to an existing file, preserving its file identity."""
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=indent)
        if trailing_newline:
            handle.write("\n")


def atomic_write_json(path, payload, *, indent=2, trailing_newline=False):
    """Write JSON via a sibling temporary file, leaving the original intact on failure."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, temporary_path = tempfile.mkstemp(
        prefix="." + os.path.basename(path) + ".",
        suffix=".tmp",
        dir=directory,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=indent)
            if trailing_newline:
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)


def atomic_copy_file(source_path, destination_path):
    """Copy a file via a sibling temporary file and then atomically replace."""
    directory = os.path.dirname(os.path.abspath(destination_path))
    fd, temporary_path = tempfile.mkstemp(
        prefix="." + os.path.basename(destination_path) + ".",
        suffix=".tmp",
        dir=directory,
    )
    os.close(fd)
    try:
        shutil.copy2(source_path, temporary_path)
        os.replace(temporary_path, destination_path)
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)


def metadata_backup_path(source_path, appdata_claude_dir, backup_dir):
    """Return a collision-free backup path preserving account/org folders."""
    metadata_root = os.path.abspath(
        os.path.join(appdata_claude_dir, "claude-code-sessions")
    )
    source_abs = os.path.abspath(source_path)
    relative = os.path.relpath(source_abs, metadata_root)
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        raise ValueError("metadata path is outside the Claude sessions root")
    return os.path.join(backup_dir, relative)
