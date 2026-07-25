"""
Fixture test runner for CI.

For each fixture:
  1. Runs diagnose.py --state against the fixture state and diffs against golden/diagnose.json
     and golden/diagnose.txt.
  2. If golden/dry-run.txt exists: copies state to a temp dir, runs the mutator in dry-run
     mode, asserts the copy is unchanged, checks its exit-status contract, and diffs stdout
     against the golden file.
  3. If golden/post-mutation.json exists: copies state to a fresh temp dir, runs the mutator
     with --apply, runs diagnose.py against the mutated state, diffs against the golden file.
  4. Asserts state/ is byte-identical before and after all tests (originals never touched).

Usage:
    python tests/run_fixture_tests.py
    python tests/run_fixture_tests.py fixtures/01-healthy-baseline  # single fixture
"""
import json
import os
import sys

# Resolve paths relative to the repo root (this file lives in tests/)
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES_DIR = os.path.join(REPO_ROOT, "fixtures")

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

from fixture_scenarios import FixtureScenario, read_exit_contract, state_fingerprint


def _show_text_diff(label, expected, actual):
    exp_lines = expected.splitlines()
    act_lines = actual.splitlines()
    print("    FAIL: {} mismatch".format(label))
    for i, (e, a) in enumerate(zip(exp_lines, act_lines)):
        if e != a:
            print("    Line {}: expected {!r}".format(i + 1, e))
            print("            got      {!r}".format(a))
    if len(act_lines) != len(exp_lines):
        print("    Line count: expected {}, got {}".format(
            len(exp_lines), len(act_lines)
        ))


def run_fixture(fixture_dir):
    scenario = FixtureScenario(fixture_dir, REPO_ROOT)
    paths = scenario.paths
    name = paths.name
    state_dir = paths.state_dir
    golden_json = paths.diagnose_json
    golden_txt = paths.diagnose_text
    dry_run_golden = paths.dry_run_text
    dry_run_exit = paths.dry_run_exit
    post_mutation_golden = paths.post_mutation_json

    if not os.path.isfile(golden_json):
        print("  SKIP {} (no golden/diagnose.json)".format(name))
        return None  # skipped

    print("  Testing {}...".format(name))

    # Snapshot state before any test
    state_before = state_fingerprint(state_dir)

    # 1. diagnose.py --json
    diagnosis = scenario.diagnose_json()
    actual_json = diagnosis.payload
    with open(golden_json, encoding="utf-8") as f:
        expected_json = json.load(f)

    if actual_json != expected_json:
        print("    FAIL: diagnose.json mismatch")
        print("    Expected: {}".format(json.dumps(expected_json, indent=2)))
        print("    Got:      {}".format(json.dumps(actual_json, indent=2)))
        return False

    print("    diagnose.json OK")

    # 1b. diagnose.py human text
    if os.path.isfile(golden_txt):
        result_txt = scenario.diagnose_text()
        with open(golden_txt, encoding="utf-8") as f:
            expected_txt = f.read()

        if result_txt.stdout != expected_txt:
            _show_text_diff("diagnose.txt", expected_txt, result_txt.stdout)
            return False

        print("    diagnose.txt OK")

    # 2-3. Mutator dry-run test
    if os.path.isfile(dry_run_golden):
        if not os.path.isfile(dry_run_exit):
            print("    FAIL: missing dry-run.exit contract")
            return False
        try:
            expected_dry_exit = read_exit_contract(dry_run_exit)
        except ValueError as error:
            print("    FAIL: invalid dry-run.exit contract: {}".format(error))
            return False

        mutator_path = scenario.find_mutator(expected_json)
        if not mutator_path:
            print("    SKIP mutator tests (no mutator in golden/diagnose.json)")
        else:
            diagnosis_id = expected_json["diagnosis_id"]

            r_dry = scenario.run_dry_mutator(
                mutator_path, diagnosis_id, temp_prefix="fx_dry_"
            )
            with open(dry_run_golden, encoding="utf-8") as f:
                expected_dry = f.read()

            if not r_dry.state_unchanged:
                print("    FAIL: dry run modified its isolated state copy")
                return False

            if r_dry.returncode != expected_dry_exit:
                print(
                    "    FAIL: dry-run exit mismatch: expected {}, got {}".format(
                        expected_dry_exit, r_dry.returncode
                    )
                )
                if r_dry.stderr:
                    print("    stderr: {}".format(r_dry.stderr[:300]))
                return False

            if r_dry.stdout != expected_dry:
                _show_text_diff("dry-run.txt", expected_dry, r_dry.stdout)
                if r_dry.stderr:
                    print("    stderr: {}".format(r_dry.stderr[:300]))
                return False

            print("    dry-run.txt OK")

            # 4-6. Apply + post-mutation test
            if os.path.isfile(post_mutation_golden):
                applied = scenario.apply_and_diagnose(
                    mutator_path, diagnosis_id, temp_prefix="fx_apply_"
                )
                actual_post = applied.post_diagnosis.payload
                with open(post_mutation_golden, encoding="utf-8") as f:
                    expected_post = json.load(f)

                if actual_post != expected_post:
                    print("    FAIL: post-mutation.json mismatch")
                    print("    Expected: {}".format(json.dumps(expected_post, indent=2)))
                    print("    Got:      {}".format(json.dumps(actual_post, indent=2)))
                    return False

                print("    post-mutation.json OK")

    # Assert original state/ unchanged throughout
    state_after = state_fingerprint(state_dir)
    if state_before != state_after:
        print("    FAIL: state/ was modified during test")
        return False

    print("    state/ unchanged OK")
    return True


def main():
    # Allow running a specific fixture via CLI arg
    if len(sys.argv) > 1:
        fixtures = [os.path.abspath(sys.argv[1])]
    else:
        fixtures = sorted(
            os.path.join(FIXTURES_DIR, d)
            for d in os.listdir(FIXTURES_DIR)
            if os.path.isdir(os.path.join(FIXTURES_DIR, d))
            and not d.startswith("_")
        )

    passed = 0
    failed = 0
    skipped = 0

    print("Running fixture tests from {}".format(FIXTURES_DIR))
    print()

    for fixture_dir in fixtures:
        result = run_fixture(fixture_dir)
        if result is True:
            passed += 1
        elif result is False:
            failed += 1
        else:
            skipped += 1

    print()
    print("Results: {} passed, {} failed, {} skipped".format(passed, failed, skipped))

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
