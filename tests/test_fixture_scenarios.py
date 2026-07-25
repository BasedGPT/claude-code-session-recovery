"""Focused tests for isolated fixture scenario execution."""

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

from fixture_scenarios import (  # noqa: E402
    FixtureScenario,
    read_exit_contract,
    state_fingerprint,
)


def _fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    for file_path in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        digest.update(str(file_path.relative_to(path)).encode("utf-8"))
        digest.update(file_path.read_bytes())
    return digest.hexdigest()


class FixtureScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.repo_root = self.root / "repo"
        self.fixture_dir = self.root / "fixture"
        self.temp_parent = self.root / "operations"
        self.temp_parent.mkdir(parents=True)
        state = self.fixture_dir / "state"
        state.mkdir(parents=True)
        (state / "source.txt").write_text("original\n", encoding="utf-8")
        self._write_diagnose()
        self._write_mutator()
        self.scenario = FixtureScenario(
            str(self.fixture_dir), str(self.repo_root), temp_parent=str(self.temp_parent)
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_diagnose(self, *, exits_with: int = 0) -> None:
        diagnose_path = self.repo_root / "tools" / "diagnose.py"
        diagnose_path.parent.mkdir(parents=True, exist_ok=True)
        diagnose_path.write_text(
            "import json\n"
            "import pathlib\n"
            "import sys\n"
            "if " + repr(exits_with) + ":\n"
            "    sys.exit(" + repr(exits_with) + ")\n"
            "state = pathlib.Path(sys.argv[sys.argv.index('--state') + 1])\n"
            "payload = {\n"
            "    'diagnosis_id': 'fixture-diagnosis',\n"
            "    'mutated': (state / 'mutated.txt').exists(),\n"
            "    'matched_problems': [{'mutator': 'tools/mutator.py'}],\n"
            "}\n"
            "if '--json' in sys.argv:\n"
            "    print(json.dumps(payload))\n"
            "else:\n"
            "    print('human diagnosis')\n",
            encoding="utf-8",
        )

    def _write_mutator(
        self,
        *,
        fail_dry: bool = False,
        fail_apply: bool = False,
        mutate_dry: bool = False,
    ) -> None:
        mutator_path = self.repo_root / "tools" / "mutator.py"
        mutator_path.write_text(
            "import pathlib\n"
            "import sys\n"
            "state = pathlib.Path(sys.argv[sys.argv.index('--state') + 1])\n"
            "apply = '--apply' in sys.argv\n"
            "if apply and " + repr(fail_apply) + ":\n"
            "    sys.exit(19)\n"
            "if not apply and " + repr(fail_dry) + ":\n"
            "    sys.exit(17)\n"
            "if not apply and " + repr(mutate_dry) + ":\n"
            "    (state / 'mutated.txt').write_text('changed\\n', encoding='utf-8')\n"
            "if apply:\n"
            "    (state / 'mutated.txt').write_text('changed\\n', encoding='utf-8')\n"
            "    print('applied')\n"
            "else:\n"
            "    print('dry run')\n",
            encoding="utf-8",
        )

    def test_dry_and_apply_use_copies_cleanup_and_preserve_fixture_state(self) -> None:
        before = _fingerprint(self.fixture_dir / "state")
        diagnosis = self.scenario.diagnose_json()
        mutator = self.scenario.find_mutator(diagnosis.payload)

        dry_run = self.scenario.run_dry_mutator(
            mutator, diagnosis.diagnosis_id, temp_prefix="fixture_dry_"
        )
        applied = self.scenario.apply_and_diagnose(
            mutator, diagnosis.diagnosis_id, temp_prefix="fixture_apply_"
        )

        self.assertEqual(dry_run.stdout, "dry run\n")
        self.assertTrue(dry_run.state_unchanged)
        self.assertEqual(applied.command.stdout, "applied\n")
        self.assertTrue(applied.post_diagnosis.payload["mutated"])
        self.assertEqual(before, _fingerprint(self.fixture_dir / "state"))
        self.assertEqual(list(self.temp_parent.iterdir()), [])

    def test_dry_run_subprocess_failure_is_captured_for_golden_comparison(self) -> None:
        self._write_mutator(fail_dry=True)
        diagnosis = self.scenario.diagnose_json()
        mutator = self.scenario.find_mutator(diagnosis.payload)

        outcome = self.scenario.run_dry_mutator(
            mutator, diagnosis.diagnosis_id, temp_prefix="fixture_dry_fail_"
        )

        self.assertEqual(outcome.returncode, 17)
        self.assertEqual(list(self.temp_parent.iterdir()), [])

    def test_dry_run_reports_an_isolated_state_mutation(self) -> None:
        self._write_mutator(mutate_dry=True)
        diagnosis = self.scenario.diagnose_json()
        mutator = self.scenario.find_mutator(diagnosis.payload)

        outcome = self.scenario.run_dry_mutator(
            mutator, diagnosis.diagnosis_id, temp_prefix="fixture_dry_mutates_"
        )

        self.assertFalse(outcome.state_unchanged)
        self.assertEqual(list(self.temp_parent.iterdir()), [])
        self.assertFalse((self.fixture_dir / "state" / "mutated.txt").exists())

    def test_state_fingerprint_includes_empty_directories(self) -> None:
        state = self.fixture_dir / "state"
        before = state_fingerprint(str(state))
        (state / "empty").mkdir()
        self.assertNotEqual(before, state_fingerprint(str(state)))

    def test_exit_contract_requires_a_non_negative_integer(self) -> None:
        contract = self.root / "dry-run.exit"
        contract.write_text("17\n", encoding="utf-8")
        self.assertEqual(read_exit_contract(str(contract)), 17)

        contract.write_text("-1\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            read_exit_contract(str(contract))

    def test_checked_diagnosis_and_apply_failures_raise_and_cleanup(self) -> None:
        self._write_diagnose(exits_with=7)
        with self.assertRaises(subprocess.CalledProcessError) as diagnosis_error:
            self.scenario.diagnose_json()
        self.assertEqual(diagnosis_error.exception.returncode, 7)

        self._write_diagnose()
        self._write_mutator(fail_apply=True)
        diagnosis = self.scenario.diagnose_json()
        mutator = self.scenario.find_mutator(diagnosis.payload)
        with self.assertRaises(subprocess.CalledProcessError) as apply_error:
            self.scenario.apply_and_diagnose(
                mutator, diagnosis.diagnosis_id, temp_prefix="fixture_apply_fail_"
            )
        self.assertEqual(apply_error.exception.returncode, 19)
        self.assertEqual(list(self.temp_parent.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
