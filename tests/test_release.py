from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from tools import release


class ReleasePolicyTests(unittest.TestCase):
    def _git(self, root: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def _repo(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        self._git(root, "init", "-q")
        self._git(root, "config", "user.email", "test@example.invalid")
        self._git(root, "config", "user.name", "Release Policy Test")
        return temporary, root

    def _commit(self, root: Path, message: str) -> str:
        self._git(root, "add", "--", ".")
        self._git(root, "commit", "-q", "-m", message)
        return self._git(root, "rev-parse", "HEAD")

    def _write_release(
        self, root: Path, version: str, *, tool_text: str = "print('ok')\n"
    ) -> None:
        (root / "VERSION").write_text(version + "\n", encoding="utf-8")
        note = root / "docs" / "releases" / f"v{version}.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(f"# v{version}\n\nRelease note.\n", encoding="utf-8")
        tool = root / "tools" / "example.py"
        tool.parent.mkdir(parents=True, exist_ok=True)
        tool.write_text(tool_text, encoding="utf-8")

    def test_parse_version_is_strict_and_orderable(self) -> None:
        self.assertEqual(str(release.parse_version("1.2.3")), "1.2.3")
        self.assertLess(
            release.parse_version("1.2.3"), release.parse_version("1.2.4")
        )
        for value in ("1.2", "01.2.3", "1.2.3-beta", "1.2.3.4"):
            with self.assertRaises(release.ReleasePolicyError):
                release.parse_version(value)

    def test_bootstrap_transition_requires_version_and_matching_note(self) -> None:
        temporary, root = self._repo()
        with temporary:
            (root / "README.md").write_text("before\n", encoding="utf-8")
            base = self._commit(root, "pre-version baseline")
            self._write_release(root, "1.0.0")
            head = self._commit(root, "establish version policy")

            result = release.check_transition(root, base, head)

            self.assertIsNone(result.base_version)
            self.assertEqual(result.head_version, "1.0.0")
            self.assertEqual(result.release_note, "docs/releases/v1.0.0.md")

    def test_future_transition_requires_prior_annotated_tag_and_increasing_version(
        self,
    ) -> None:
        temporary, root = self._repo()
        with temporary:
            self._write_release(root, "1.0.0")
            base = self._commit(root, "baseline")
            self._git(root, "tag", "-a", "v1.0.0", "-m", "Release v1.0.0")
            self._write_release(root, "1.0.1", tool_text="print('changed')\n")
            head = self._commit(root, "patch release")

            result = release.check_transition(root, base, head)

            self.assertEqual(result.base_version, "1.0.0")
            self.assertEqual(result.head_version, "1.0.1")

            (root / "VERSION").write_text("1.0.0\n", encoding="utf-8")
            same_version = self._commit(root, "unversioned change")
            with self.assertRaisesRegex(release.ReleasePolicyError, "must increase"):
                release.check_transition(root, head, same_version)

    def test_transition_rejects_missing_prior_tag(self) -> None:
        temporary, root = self._repo()
        with temporary:
            self._write_release(root, "1.0.0")
            base = self._commit(root, "baseline")
            self._write_release(root, "1.0.1")
            head = self._commit(root, "patch without baseline tag")

            with self.assertRaisesRegex(release.ReleasePolicyError, "must be annotated"):
                release.check_transition(root, base, head)

    def test_tag_check_rejects_lightweight_tag_and_accepts_annotated_tag(self) -> None:
        temporary, root = self._repo()
        with temporary:
            self._write_release(root, "1.0.0")
            commit = self._commit(root, "baseline")
            self._git(root, "tag", "v1.0.0")
            with self.assertRaisesRegex(release.ReleasePolicyError, "annotated"):
                release.check_tag(root, "v1.0.0", commit)

            self._git(root, "tag", "-d", "v1.0.0")
            self._git(root, "tag", "-a", "v1.0.0", "-m", "Release v1.0.0")
            result = release.check_tag(root, "v1.0.0", commit)
            self.assertEqual(result.commit, commit)

    def test_manifest_is_deterministic_and_hashes_committed_tool_bytes(self) -> None:
        temporary, root = self._repo()
        with temporary:
            self._write_release(root, "1.0.0", tool_text="print('raw bytes')\n")
            commit = self._commit(root, "baseline")
            manifest = release.build_manifest(root, commit)
            repeated = release.build_manifest(root, commit)

            self.assertEqual(manifest, repeated)
            self.assertEqual(manifest["commit"], commit)
            files = {item["path"]: item for item in manifest["files"]}
            self.assertIn("tools/example.py", files)
            self.assertEqual(
                files["tools/example.py"]["sha256"],
                hashlib.sha256(b"print('raw bytes')\n").hexdigest(),
            )
            self.assertEqual(json.loads(release.manifest_text(manifest)), manifest)


if __name__ == "__main__":
    unittest.main()
