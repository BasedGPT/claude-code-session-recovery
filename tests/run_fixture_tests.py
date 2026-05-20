"""
Fixture test runner for CI.

For each fixture:
  1. Runs diagnose.py --state against the fixture state and diffs against golden/diagnose.json
     and golden/diagnose.txt.
  2. If golden/dry-run.txt exists: copies state to a temp dir, runs the mutator in dry-run
     mode, diffs stdout against the golden file.
  3. If golden/post-mutation.json exists: copies state to a fresh temp dir, runs the mutator
     with --apply, runs diagnose.py against the mutated state, diffs against the golden file.
  4. Asserts state/ is byte-identical before and after all tests (originals never touched).

Usage:
    python tests/run_fixture_tests.py
    python tests/run_fixture_tests.py fixtures/01-healthy-baseline  # single fixture
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

# Resolve paths relative to the repo root (this file lives in tests/)
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES_DIR = os.path.join(REPO_ROOT, "fixtures")
DIAGNOSE = os.path.join(REPO_ROOT, "tools", "diagnose.py")


def _dir_fingerprint(path):
    """SHA256 of all file contents under path, sorted by relative path."""
    h = hashlib.sha256()
    for dirpath, _, filenames in sorted(os.walk(path)):
        for fname in sorted(filenames):
            fpath = os.path.join(dirpath, fname)
            rel = os.path.relpath(fpath, path)
            h.update(rel.encode("utf-8"))
            with open(fpath, "rb") as f:
                h.update(f.read())
    return h.hexdigest()


def _show_text_diff(label, expected, actual):
    exp_lines = expected.splitlines()
    act_lines = actual.splitlines()
    print("    FAIL: {} mismatch".format(label))
    for i, (e, a) in enumerate(zip(exp_lines, act_lines)):
        if e != a:
            print("    Line {}: expected {!r}".format(i + 1, e))
            print("            got      {!r}".format(i + 1, a))
    if len(act_lines) != len(exp_lines):
        print("    Line count: expected {}, got {}".format(
            len(exp_lines), len(act_lines)
        ))


def run_fixture(fixture_dir):
    name = os.path.basename(fixture_dir)
    state_dir = os.path.join(fixture_dir, "state")
    golden_dir = os.path.join(fixture_dir, "golden")
    golden_json = os.path.join(golden_dir, "diagnose.json")
    golden_txt = os.path.join(golden_dir, "diagnose.txt")
    dry_run_golden = os.path.join(golden_dir, "dry-run.txt")
    post_mutation_golden = os.path.join(golden_dir, "post-mutation.json")

    if not os.path.isfile(golden_json):
        print("  SKIP {} (no golden/diagnose.json)".format(name))
        return None  # skipped

    print("  Testing {}...".format(name))

    # Snapshot state before any test
    state_before = _dir_fingerprint(state_dir)

    # 1. diagnose.py --json
    result = subprocess.run(
        [sys.executable, DIAGNOSE, "--state", state_dir, "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    actual_json = json.loads(result.stdout)
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
        result_txt = subprocess.run(
            [sys.executable, DIAGNOSE, "--state", state_dir],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        with open(golden_txt, encoding="utf-8") as f:
            expected_txt = f.read()

        if result_txt.stdout != expected_txt:
            _show_text_diff("diagnose.txt", expected_txt, result_txt.stdout)
            return False

        print("    diagnose.txt OK")

    # 2-3. Mutator dry-run test
    if os.path.isfile(dry_run_golden):
        mutator_rel = None
        for problem in expected_json.get("matched_problems", []):
            if problem.get("mutator"):
                mutator_rel = problem["mutator"]
                break

        if not mutator_rel:
            print("    SKIP mutator tests (no mutator in golden/diagnose.json)")
        else:
            mutator_path = os.path.join(REPO_ROOT, os.path.normpath(mutator_rel))
            diagnosis_id = expected_json["diagnosis_id"]

            tmp1 = tempfile.mkdtemp(prefix="fx_dry_")
            try:
                shutil.copytree(state_dir, os.path.join(tmp1, "state"))
                tmp_state1 = os.path.join(tmp1, "state")

                r_dry = subprocess.run(
                    [sys.executable, mutator_path,
                     "--state", tmp_state1,
                     "--diagnosis-id", diagnosis_id],
                    capture_output=True, text=True, encoding="utf-8",
                )
                with open(dry_run_golden, encoding="utf-8") as f:
                    expected_dry = f.read()

                if r_dry.stdout != expected_dry:
                    _show_text_diff("dry-run.txt", expected_dry, r_dry.stdout)
                    if r_dry.stderr:
                        print("    stderr: {}".format(r_dry.stderr[:300]))
                    return False

                print("    dry-run.txt OK")
            finally:
                shutil.rmtree(tmp1, ignore_errors=True)

            # 4-6. Apply + post-mutation test
            if os.path.isfile(post_mutation_golden):
                tmp2 = tempfile.mkdtemp(prefix="fx_apply_")
                try:
                    shutil.copytree(state_dir, os.path.join(tmp2, "state"))
                    tmp_state2 = os.path.join(tmp2, "state")

                    subprocess.run(
                        [sys.executable, mutator_path,
                         "--state", tmp_state2,
                         "--diagnosis-id", diagnosis_id, "--apply"],
                        capture_output=True, text=True, encoding="utf-8",
                        check=True,
                    )

                    r_post = subprocess.run(
                        [sys.executable, DIAGNOSE,
                         "--state", tmp_state2, "--json"],
                        capture_output=True, text=True, encoding="utf-8",
                        check=True,
                    )
                    actual_post = json.loads(r_post.stdout)
                    with open(post_mutation_golden, encoding="utf-8") as f:
                        expected_post = json.load(f)

                    if actual_post != expected_post:
                        print("    FAIL: post-mutation.json mismatch")
                        print("    Expected: {}".format(json.dumps(expected_post, indent=2)))
                        print("    Got:      {}".format(json.dumps(actual_post, indent=2)))
                        return False

                    print("    post-mutation.json OK")
                finally:
                    shutil.rmtree(tmp2, ignore_errors=True)

    # Assert original state/ unchanged throughout
    state_after = _dir_fingerprint(state_dir)
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
