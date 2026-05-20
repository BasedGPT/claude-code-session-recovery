"""
Shared stale-lock detection for scheduled scripts.

Problem: atexit handlers do not run when a process is forcibly killed
(for example, Windows Task Scheduler timeout or power loss). This leaves
stale lock files that block all future runs.

Solution: write the PID into the lock file. On startup, if a lock exists,
check whether that PID is still running. If the process is gone, the lock
is stale -- remove it and continue. Only block if the process is still alive.

Usage:
    from lock_utils import acquire_lock, release_lock

    LOCK_FILE = os.path.join(LOG_DIR, 'my_script.lock')
    acquire_lock(LOCK_FILE, 'my_script')       # exits if live lock found
    atexit.register(release_lock, LOCK_FILE)   # clean up on any normal exit
"""

import os
import sys


def _pid_running(pid):
    """Return True if the given PID is still running on Windows."""
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


def acquire_lock(lock_file, script_name):
    """Acquire a lock file, handling stale locks from previously killed processes.

    If the lock file exists:
      - If the stored PID is still running: warn and sys.exit(0) (another live instance).
      - If the stored PID is gone (stale): remove and continue.

    Always writes the current PID into the lock file on success.
    Pair with: atexit.register(release_lock, lock_file)
    """
    if os.path.exists(lock_file):
        try:
            locked_pid = int(open(lock_file, encoding="utf-8").read().strip())
        except (ValueError, OSError):
            locked_pid = None

        if locked_pid and _pid_running(locked_pid):
            print("[WARN] {}: lock file exists -- PID {} is still running. Exiting.".format(
                script_name, locked_pid))
            sys.exit(0)
        else:
            print("[WARN] {}: stale lock file (PID {} is gone) -- removing and continuing.".format(
                script_name, locked_pid))
            os.remove(lock_file)  # standards: log/temp rotation

    with open(lock_file, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))


def release_lock(lock_file):
    """Remove the lock file. Register with atexit.register(release_lock, LOCK_FILE)."""
    if os.path.exists(lock_file):
        os.remove(lock_file)  # standards: log/temp rotation
