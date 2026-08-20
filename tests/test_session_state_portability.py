"""Regression tests for live session-state platform detection."""

import os
import sys
import tempfile
import unittest
from unittest import mock


TOOLS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import session_state  # noqa: E402
import platform_support  # noqa: E402


class SessionStatePortabilityTests(unittest.TestCase):
    def test_shared_vscode_and_sessions_index_defaults_keep_platform_layouts(self):
        cases = (
            (
                "Windows",
                {"~": r"C:\\Users\\test"},
                r"C:\\Users\\test\\AppData\\Roaming",
                os.path.join(
                    r"C:\\Users\\test\\AppData\\Roaming",
                    "Code", "User", "workspaceStorage",
                ),
                os.path.join(r"C:\\Users\\test", ".claude", "projects"),
            ),
            (
                "Darwin",
                {
                    "~/Library/Application Support/Code/User/workspaceStorage": (
                        "/Users/test/Library/Application Support/Code/User/workspaceStorage"
                    ),
                    "~/.claude/projects": "/Users/test/.claude/projects",
                },
                "/Users/test/AppData/Roaming",
                "/Users/test/Library/Application Support/Code/User/workspaceStorage",
                "/Users/test/.claude/projects",
            ),
            (
                "Linux",
                {
                    "~/.config/Code/User/workspaceStorage": (
                        "/home/test/.config/Code/User/workspaceStorage"
                    ),
                    "~/.claude/projects": "/home/test/.claude/projects",
                },
                "/home/test/.config/Code/User/AppData/Roaming",
                "/home/test/.config/Code/User/workspaceStorage",
                "/home/test/.claude/projects",
            ),
        )

        for system, expanded, appdata, expected_workspace, expected_projects in cases:
            with self.subTest(system=system), \
                    mock.patch.object(
                        platform_support.platform, "system", return_value=system
                    ), \
                    mock.patch.object(
                        platform_support.os.path,
                        "expanduser",
                        side_effect=expanded.__getitem__,
                    ), \
                    mock.patch.dict(
                        platform_support.os.environ, {"APPDATA": appdata}, clear=False
                    ):
                self.assertEqual(
                    platform_support.default_vscode_workspace_storage_dir(),
                    expected_workspace,
                )
                self.assertEqual(
                    platform_support.default_claude_sessions_index_dir(),
                    expected_projects,
                )

    def test_msix_install_type_uses_the_shared_package_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            package_data = os.path.join(
                root, "LocalAppData", "Packages", "Claude_example",
                "LocalCache", "Roaming", "Claude",
            )
            os.makedirs(package_data)
            with mock.patch.object(session_state.platform, "system", return_value="Windows"), \
                    mock.patch.object(
                        session_state, "default_claude_appdata_dir", return_value=package_data
                    ):
                install_type, real_path = session_state._detect_install_type()

            self.assertEqual(install_type, "msix")
            self.assertEqual(os.path.normcase(real_path), os.path.normcase(package_data))


if __name__ == "__main__":
    unittest.main()
