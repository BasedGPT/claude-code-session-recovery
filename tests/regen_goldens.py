"""Regenerate all fixture golden files after a diagnose.py schema change.

Usage: python tests/regen_goldens.py
"""
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES_DIR = os.path.join(REPO_ROOT, "fixtures")

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

from fixture_scenarios import FixtureScenario, read_exit_contract

fixtures = sorted(
    os.path.join(FIXTURES_DIR, d)
    for d in os.listdir(FIXTURES_DIR)
    if os.path.isdir(os.path.join(FIXTURES_DIR, d)) and not d.startswith("_")
)
failures = 0

for fixture_dir in fixtures:
    scenario = FixtureScenario(fixture_dir, REPO_ROOT)
    paths = scenario.paths
    name = paths.name
    golden_json = paths.diagnose_json
    golden_txt = paths.diagnose_text
    dry_run_golden = paths.dry_run_text
    dry_run_exit = paths.dry_run_exit
    post_mutation_golden = paths.post_mutation_json

    if not os.path.isfile(golden_json):
        print(f"SKIP {name} (no golden/diagnose.json)")
        continue

    print(f"Updating {name}...")

    # 1. Regenerate diagnose.json
    diagnosis = scenario.diagnose_json()
    new_json = diagnosis.payload
    with open(golden_json, "w", encoding="utf-8", newline="\n") as f:
        json.dump(new_json, f, indent=2)
        f.write("\n")
    print(f"  diagnose.json updated (id: {new_json['diagnosis_id']})")

    # 2. Regenerate diagnose.txt if it exists
    if os.path.isfile(golden_txt):
        result_txt = scenario.diagnose_text()
        with open(golden_txt, "w", encoding="utf-8", newline="\n") as f:
            f.write(result_txt.stdout)
        print(f"  diagnose.txt updated")

    # 3. If mutator goldens exist, regenerate with the new diagnosis_id
    if not (os.path.isfile(dry_run_golden) or os.path.isfile(post_mutation_golden)):
        continue

    mutator_path = scenario.find_mutator(new_json)
    if not mutator_path:
        print(f"  SKIP mutator goldens (no mutator in updated diagnose.json)")
        continue

    diagnosis_id = new_json["diagnosis_id"]

    if os.path.isfile(dry_run_golden):
        if not os.path.isfile(dry_run_exit):
            print("  FAIL: missing dry-run.exit contract; dry-run.txt not updated")
            failures += 1
            continue
        try:
            expected_dry_exit = read_exit_contract(dry_run_exit)
        except ValueError as error:
            print(f"  FAIL: invalid dry-run.exit contract: {error}; dry-run.txt not updated")
            failures += 1
            continue

        r_dry = scenario.run_dry_mutator(
            mutator_path, diagnosis_id, temp_prefix="regen_dry_"
        )
        if not r_dry.state_unchanged:
            print("  FAIL: dry run modified its isolated state copy; dry-run.txt not updated")
            failures += 1
            continue
        if r_dry.returncode != expected_dry_exit:
            print(
                "  FAIL: dry-run exit changed (expected {}, got {}); dry-run.txt not updated".format(
                    expected_dry_exit, r_dry.returncode
                )
            )
            failures += 1
            continue
        with open(dry_run_golden, "w", encoding="utf-8", newline="\n") as f:
            f.write(r_dry.stdout)
        print(f"  dry-run.txt updated")

    if os.path.isfile(post_mutation_golden):
        applied = scenario.apply_and_diagnose(
            mutator_path, diagnosis_id, temp_prefix="regen_apply_"
        )
        new_post = applied.post_diagnosis.payload
        with open(post_mutation_golden, "w", encoding="utf-8", newline="\n") as f:
            json.dump(new_post, f, indent=2)
            f.write("\n")
        print(f"  post-mutation.json updated (id: {new_post['diagnosis_id']})")

if failures:
    print(f"\nStopped with {failures} fixture contract failure(s).")
    sys.exit(1)

print("\nDone.")
