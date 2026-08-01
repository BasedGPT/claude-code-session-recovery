"""
Shared stale-lock detection for scheduled scripts.

Problem: atexit handlers do not run when a process is forcibly killed
(for example, Windows Task Scheduler timeout or power loss). This leaves
stale lock files that block all future runs.

Solution: publish a directory containing the PID as one atomic rename. On
startup, if a lock exists, check whether that PID is still running. If the
process is gone, the lock is stale -- remove it and continue. Only block if
the process is still alive or its owner cannot be verified.

Usage:
    from lock_utils import acquire_lock, release_lock

    LOCK_FILE = os.path.join(LOG_DIR, 'my_script.lock')
    acquire_lock(LOCK_FILE, 'my_script')       # exits if live lock found
    atexit.register(release_lock, LOCK_FILE)   # clean up on any normal exit
"""

import os
import shutil
import sys
import uuid


def _pid_running(pid):
    """Return True if the given PID is still running."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True
    try:
        import ctypes
        PROCESS_QUERY_INFORMATION = 0x0400
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
        if handle == 0:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    except Exception:
        return False  # can't determine -- treat as not running


def _pid_path(lock_file):
    return os.path.join(lock_file, "pid") if os.path.isdir(lock_file) else lock_file


def _read_pid(lock_file):
    try:
        with open(_pid_path(lock_file), encoding="utf-8") as handle:
            return int(handle.read().strip())
    except (ValueError, OSError):
        return None


def _remove_lock_tree(lock_file):
    if os.path.isdir(lock_file):
        shutil.rmtree(lock_file)
    else:
        os.remove(lock_file)


def acquire_lock(lock_file, script_name):
    """Acquire a lock file, handling stale locks from previously killed processes.

    If the lock file exists:
      - If the stored PID is still running: warn and sys.exit(0) (another live instance).
      - If the stored PID is gone (stale): remove and continue.

    Always writes the current PID into the lock file on success.
    Pair with: atexit.register(release_lock, lock_file)
    """
    os.makedirs(os.path.dirname(os.path.abspath(lock_file)), exist_ok=True)
    while True:
        temporary_lock = "{}.new.{}".format(lock_file, uuid.uuid4().hex)
        try:
            os.mkdir(temporary_lock)
            with open(os.path.join(temporary_lock, "pid"), "w", encoding="utf-8") as handle:
                handle.write(str(os.getpid()))
            os.rename(temporary_lock, lock_file)
        except FileExistsError:
            try:
                _remove_lock_tree(temporary_lock)
            except FileNotFoundError:
                pass
            locked_pid = _read_pid(lock_file)
            if locked_pid and _pid_running(locked_pid):
                print("[WARN] {}: lock exists -- PID {} is still running. Exiting.".format(
                    script_name, locked_pid))
                sys.exit(0)

            if locked_pid is None:
                print("[WARN] {}: lock exists but its owner cannot be verified. Exiting.".format(
                    script_name))
                sys.exit(0)

            print("[WARN] {}: stale lock (PID {} is gone) -- reclaiming and continuing.".format(
                script_name, locked_pid))
            stale_path = "{}.stale.{}".format(lock_file, uuid.uuid4().hex)
            try:
                os.rename(lock_file, stale_path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                print("[WARN] {}: could not reclaim stale lock: {}. Exiting.".format(
                    script_name, exc))
                sys.exit(0)
            try:
                _remove_lock_tree(stale_path)
            except OSError as exc:
                print("[WARN] {}: could not remove stale lock: {}. Exiting.".format(
                    script_name, exc))
                sys.exit(0)
            continue
        except OSError:
            try:
                _remove_lock_tree(temporary_lock)
            except FileNotFoundError:
                pass
            raise
        return


def release_lock(lock_file):
    """Remove the lock file. Register with atexit.register(release_lock, LOCK_FILE)."""
    if not os.path.exists(lock_file):
        return
    if _read_pid(lock_file) != os.getpid():
        return
    try:
        _remove_lock_tree(lock_file)
    except FileNotFoundError:
        pass
