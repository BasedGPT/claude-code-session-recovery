"""Platform-specific paths and Claude Desktop process probes.

The executable tools keep their command-line policy local, but they all need
the same platform facts.  Keeping those facts here prevents a live macOS run
from silently falling back to Windows' ``APPDATA`` layout and gives mutators a
single fail-closed Desktop probe.
"""

import os
import platform
import subprocess


def default_claude_appdata_dir():
    """Return Claude Desktop's platform-appropriate data directory."""
    system = platform.system()
    if system == "Darwin":
        return os.path.expanduser("~/Library/Application Support/Claude")
    if system == "Linux":
        return os.path.expanduser("~/.config/Claude")

    appdata_claude = os.path.join(
        os.environ.get("APPDATA", os.path.expanduser("~")), "Claude"
    )
    if not os.path.isdir(appdata_claude):
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        packages_dir = os.path.join(local_appdata, "Packages")
        if local_appdata and os.path.isdir(packages_dir):
            for package_name in sorted(os.listdir(packages_dir)):
                if not package_name.startswith(("Claude_", "AnthropicClaude_")):
                    continue
                candidate = os.path.join(
                    packages_dir,
                    package_name,
                    "LocalCache",
                    "Roaming",
                    "Claude",
                )
                if os.path.isdir(candidate):
                    return candidate
    return appdata_claude


def default_claude_paths():
    """Return ``(desktop_data_dir, transcript_projects_dir)``."""
    return default_claude_appdata_dir(), default_claude_sessions_index_dir()


def default_claude_sessions_index_dir():
    """Return the Claude project root containing ``sessions-index.json`` files."""
    if platform.system() in ("Darwin", "Linux"):
        return os.path.expanduser("~/.claude/projects")
    return os.path.join(os.path.expanduser("~"), ".claude", "projects")


def default_vscode_workspace_storage_dir():
    """Return VS Code's platform-appropriate ``workspaceStorage`` directory."""
    system = platform.system()
    if system == "Darwin":
        return os.path.expanduser(
            "~/Library/Application Support/Code/User/workspaceStorage"
        )
    if system == "Linux":
        return os.path.expanduser("~/.config/Code/User/workspaceStorage")
    return os.path.join(
        os.environ.get("APPDATA", os.path.expanduser("~")),
        "Code", "User", "workspaceStorage",
    )


def default_groupings_store():
    """Return Claude Desktop's Chromium Local Storage LevelDB directory."""
    return os.path.join(
        default_claude_appdata_dir(), "Local Storage", "leveldb"
    )


def desktop_process_check_command():
    """Return a copy/paste command that checks for Claude Desktop."""
    if platform.system() == "Darwin":
        return "pgrep -x Claude"
    if platform.system() == "Windows":
        return 'tasklist /FI "IMAGENAME eq claude.exe"'
    return "ps -axo comm=,args= | grep -i '[C]laude'"


def _run_probe(command):
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def _darwin_desktop_process_running():
    """Probe the native Claude process on macOS, failing closed if unknown."""
    unknown = False
    for command in (
        ["pgrep", "-x", "Claude"],
        ["pgrep", "-f", r"/Claude\.app/Contents/MacOS/Claude"],
    ):
        try:
            result = _run_probe(command)
        except (OSError, subprocess.TimeoutExpired):
            unknown = True
            continue
        if result.returncode == 0:
            return True
        if result.returncode not in (1,):
            unknown = True

    try:
        result = _run_probe(["ps", "-axo", "comm=,args="])
    except (OSError, subprocess.TimeoutExpired):
        return True
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            lowered = line.lower()
            if "/claude.app/contents/macos/claude" in lowered:
                return True
            command_name = line.strip().split(None, 1)[0] if line.strip() else ""
            if os.path.basename(command_name).lower() == "claude":
                return True
        return False
    return True if unknown else False


def _windows_desktop_process_running():
    """Probe Windows Desktop, treating an unavailable probe as live."""
    try:
        result = _run_probe(
            [
                "wmic",
                "process",
                "where",
                "name='claude.exe'",
                "get",
                "ExecutablePath",
                "/format:csv",
            ]
        )
        if result.returncode == 0 and any(
            "anthropicclaude" in line.lower() and "claude.exe" in line.lower()
            for line in result.stdout.splitlines()
        ):
            return True
    except (OSError, subprocess.TimeoutExpired):
        pass

    try:
        result = _run_probe(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-Process claude -ErrorAction SilentlyContinue | "
                "ForEach-Object { $_.Path }",
            ]
        )
        if result.returncode == 0 and any(
            "anthropicclaude" in line.lower()
            for line in result.stdout.splitlines()
        ):
            return True
    except (OSError, subprocess.TimeoutExpired):
        pass

    try:
        result = _run_probe(["tasklist", "/FI", "IMAGENAME eq claude.exe"])
        if result.returncode == 0:
            return "claude.exe" in result.stdout.lower()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return True


def desktop_process_running():
    """Return whether Claude Desktop is running, failing closed if unknown."""
    system = platform.system()
    if system == "Darwin":
        return _darwin_desktop_process_running()
    if system == "Windows":
        return _windows_desktop_process_running()
    return True
