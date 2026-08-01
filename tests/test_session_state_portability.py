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


class SessionStatePortabilityTests(unittest.TestCase):
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
