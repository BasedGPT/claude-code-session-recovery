"""Regression tests for cross-platform paths and Desktop process probes."""

import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


TOOLS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import platform_support  # noqa: E402


class PlatformSupportTests(unittest.TestCase):
    def test_darwin_paths_use_application_support(self):
        expanded = {
            "~/Library/Application Support/Claude": "/Users/test/Library/Application Support/Claude",
            "~/.claude/projects": "/Users/test/.claude/projects",
        }

        with mock.patch.object(platform_support.platform, "system", return_value="Darwin"), \
                mock.patch.object(platform_support.os.path, "expanduser", side_effect=expanded.__getitem__):
            self.assertEqual(
                platform_support.default_claude_paths(),
                (
                    "/Users/test/Library/Application Support/Claude",
                    "/Users/test/.claude/projects",
                ),
            )
            self.assertEqual(
                platform_support.default_groupings_store(),
                os.path.join(
                    "/Users/test/Library/Application Support/Claude",
                    "Local Storage",
                    "leveldb",
                ),
            )

    def test_windows_msix_path_is_used_when_normal_appdata_is_missing(self):
        with tempfile.TemporaryDirectory() as root:
            local_appdata = os.path.join(root, "LocalAppData")
            package_claude = os.path.join(
                local_appdata, "Packages", "Claude_example", "LocalCache", "Roaming", "Claude"
            )
            os.makedirs(package_claude)
            with mock.patch.object(platform_support.platform, "system", return_value="Windows"), \
                    mock.patch.dict(
                        platform_support.os.environ,
                        {
                            "APPDATA": os.path.join(root, "missing-appdata"),
                            "LOCALAPPDATA": local_appdata,
                        },
                        clear=False,
                    ):
                self.assertEqual(platform_support.default_claude_appdata_dir(), package_claude)

    def test_darwin_desktop_probe_uses_claude_process_name(self):
        calls = []

        def fake_run(command, **_kwargs):
            calls.append(command)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(platform_support.platform, "system", return_value="Darwin"), \
                mock.patch.object(platform_support.subprocess, "run", side_effect=fake_run):
            self.assertTrue(platform_support.desktop_process_running())

        self.assertEqual(calls[0][:3], ["pgrep", "-x", "Claude"])

    def test_unavailable_windows_probe_fails_closed(self):
        with mock.patch.object(platform_support.platform, "system", return_value="Windows"), \
                mock.patch.object(
                    platform_support.subprocess, "run", side_effect=FileNotFoundError()
                ):
            self.assertTrue(platform_support.desktop_process_running())

    def test_windows_probe_falls_through_when_path_is_not_visible(self):
        calls = []

        def fake_run(command, **_kwargs):
            calls.append(command)
            if command[0] == "wmic":
                return mock.Mock(returncode=0, stdout="Node,ExecutablePath\n", stderr="")
            if command[0] == "powershell":
                return mock.Mock(returncode=0, stdout="", stderr="")
            return mock.Mock(
                returncode=0,
                stdout="claude.exe   123 Console  1,234 K\n",
                stderr="",
            )

        with mock.patch.object(platform_support.platform, "system", return_value="Windows"), \
                mock.patch.object(platform_support.subprocess, "run", side_effect=fake_run):
            self.assertTrue(platform_support._windows_desktop_process_running())
        self.assertEqual([command[0] for command in calls], ["wmic", "powershell", "tasklist"])

    def test_platform_specific_process_check_command(self):
        with mock.patch.object(platform_support.platform, "system", return_value="Darwin"):
            self.assertEqual(platform_support.desktop_process_check_command(), "pgrep -x Claude")
        with mock.patch.object(platform_support.platform, "system", return_value="Windows"):
            self.assertEqual(
                platform_support.desktop_process_check_command(),
                'tasklist /FI "IMAGENAME eq claude.exe"',
            )


if __name__ == "__main__":
    unittest.main()
