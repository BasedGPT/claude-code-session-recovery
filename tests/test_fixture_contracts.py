"""Focused tests for fixture dry-run safety and exit-status contracts."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

import run_fixture_tests  # noqa: E402


class FixtureContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_fixture_repo(
        self,
        *,
        dry_run_stdout: str = "dry run\n",
        dry_run_exit: int = 0,
        mutate_dry: bool = False,
    ) -> tuple[Path, Path]:
        repo_root = self.root / "repo"
        fixture_dir = repo_root / "fixtures" / "01-contract"
        state_dir = fixture_dir / "state"
        golden_dir = fixture_dir / "golden"
        state_dir.mkdir(parents=True)
        golden_dir.mkdir()
        (state_dir / "source.txt").write_text("source\n", encoding="utf-8")

        diagnosis = {
            "diagnosis_id": "fixture-diagnosis",
            "matched_problems": [{"mutator": "tools/mutator.py"}],
        }
        (golden_dir / "diagnose.json").write_text(
            json.dumps(diagnosis), encoding="utf-8"
        )
        (golden_dir / "dry-run.txt").write_text(
            dry_run_stdout, encoding="utf-8"
        )
        (golden_dir / "dry-run.exit").write_text("0\n", encoding="utf-8")

        tools_dir = repo_root / "tools"
        tools_dir.mkdir()
        (tools_dir / "diagnose.py").write_text(
            "import json\n"
            "print(json.dumps({"
            "'diagnosis_id': 'fixture-diagnosis', "
            "'matched_problems': [{'mutator': 'tools/mutator.py'}]}))\n",
            encoding="utf-8",
        )
        (tools_dir / "mutator.py").write_text(
            "import pathlib\n"
            "import sys\n"
            "state = pathlib.Path(sys.argv[sys.argv.index('--state') + 1])\n"
            "if " + repr(mutate_dry) + ":\n"
            "    (state / 'changed.txt').write_text('changed\\n', encoding='utf-8')\n"
            "print(" + repr(dry_run_stdout.rstrip("\n")) + ")\n"
            "sys.exit(" + repr(dry_run_exit) + ")\n",
            encoding="utf-8",
        )
        return repo_root, fixture_dir

    def test_runner_rejects_a_dry_run_exit_contract_mismatch(self) -> None:
        repo_root, fixture_dir = self._write_fixture_repo(dry_run_exit=17)

        with patch.object(run_fixture_tests, "REPO_ROOT", str(repo_root)):
            result = run_fixture_tests.run_fixture(str(fixture_dir))

        self.assertFalse(result)

    def test_runner_rejects_a_dry_run_state_change(self) -> None:
        repo_root, fixture_dir = self._write_fixture_repo(mutate_dry=True)

        with patch.object(run_fixture_tests, "REPO_ROOT", str(repo_root)):
            result = run_fixture_tests.run_fixture(str(fixture_dir))

        self.assertFalse(result)

    def test_regenerator_preserves_stdout_when_exit_contract_changes(self) -> None:
        repo_root, fixture_dir = self._write_fixture_repo(
            dry_run_stdout="keep this golden\n", dry_run_exit=17
        )
        tests_dir = repo_root / "tests"
        tests_dir.mkdir()
        for filename in ("fixture_scenarios.py", "regen_goldens.py"):
            shutil.copy2(Path(TESTS_DIR) / filename, tests_dir / filename)

        result = subprocess.run(
            [sys.executable, str(tests_dir / "regen_goldens.py")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=repo_root,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("dry-run exit changed", result.stdout)
        self.assertEqual(
            (fixture_dir / "golden" / "dry-run.txt").read_text(encoding="utf-8"),
            "keep this golden\n",
        )


if __name__ == "__main__":
    unittest.main()
