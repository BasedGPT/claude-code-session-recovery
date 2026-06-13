"""Regenerate all fixture golden files after a diagnose.py schema change.

Usage: python tests/regen_goldens.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES_DIR = os.path.join(REPO_ROOT, "fixtures")
DIAGNOSE = os.path.join(REPO_ROOT, "tools", "diagnose.py")

fixtures = sorted(
    os.path.join(FIXTURES_DIR, d)
    for d in os.listdir(FIXTURES_DIR)
    if os.path.isdir(os.path.join(FIXTURES_DIR, d)) and not d.startswith("_")
)

for fixture_dir in fixtures:
    name = os.path.basename(fixture_dir)
    state_dir = os.path.join(fixture_dir, "state")
    golden_dir = os.path.join(fixture_dir, "golden")
    golden_json = os.path.join(golden_dir, "diagnose.json")
    golden_txt = os.path.join(golden_dir, "diagnose.txt")
    dry_run_golden = os.path.join(golden_dir, "dry-run.txt")
    post_mutation_golden = os.path.join(golden_dir, "post-mutation.json")

    if not os.path.isfile(golden_json):
        print(f"SKIP {name} (no golden/diagnose.json)")
        continue

    print(f"Updating {name}...")

    # 1. Regenerate diagnose.json
    result = subprocess.run(
        [sys.executable, DIAGNOSE, "--state", state_dir, "--json"],
        capture_output=True, text=True, encoding="utf-8", check=True,
    )
    new_json = json.loads(result.stdout)
    with open(golden_json, "w", encoding="utf-8", newline="\n") as f:
        json.dump(new_json, f, indent=2)
        f.write("\n")
    print(f"  diagnose.json updated (id: {new_json['diagnosis_id']})")

    # 2. Regenerate diagnose.txt if it exists
    if os.path.isfile(golden_txt):
        result_txt = subprocess.run(
            [sys.executable, DIAGNOSE, "--state", state_dir],
            capture_output=True, text=True, encoding="utf-8", check=True,
        )
        with open(golden_txt, "w", encoding="utf-8", newline="\n") as f:
            f.write(result_txt.stdout)
        print(f"  diagnose.txt updated")

    # 3. If mutator goldens exist, regenerate with the new diagnosis_id
    if not (os.path.isfile(dry_run_golden) or os.path.isfile(post_mutation_golden)):
        continue

    mutator_rel = None
    for problem in new_json.get("matched_problems", []):
        if problem.get("mutator"):
            mutator_rel = problem["mutator"]
            break

    if not mutator_rel:
        print(f"  SKIP mutator goldens (no mutator in updated diagnose.json)")
        continue

    mutator_path = os.path.join(REPO_ROOT, os.path.normpath(mutator_rel))
    diagnosis_id = new_json["diagnosis_id"]

    if os.path.isfile(dry_run_golden):
        tmp1 = tempfile.mkdtemp(prefix="regen_dry_")
        try:
            shutil.copytree(state_dir, os.path.join(tmp1, "state"))
            r_dry = subprocess.run(
                [sys.executable, mutator_path,
                 "--state", os.path.join(tmp1, "state"),
                 "--diagnosis-id", diagnosis_id],
                capture_output=True, text=True, encoding="utf-8",
            )
            with open(dry_run_golden, "w", encoding="utf-8", newline="\n") as f:
                f.write(r_dry.stdout)
            print(f"  dry-run.txt updated")
        finally:
            shutil.rmtree(tmp1, ignore_errors=True)

    if os.path.isfile(post_mutation_golden):
        tmp2 = tempfile.mkdtemp(prefix="regen_apply_")
        try:
            shutil.copytree(state_dir, os.path.join(tmp2, "state"))
            tmp_state = os.path.join(tmp2, "state")
            subprocess.run(
                [sys.executable, mutator_path,
                 "--state", tmp_state,
                 "--diagnosis-id", diagnosis_id, "--apply"],
                capture_output=True, text=True, encoding="utf-8", check=True,
            )
            r_post = subprocess.run(
                [sys.executable, DIAGNOSE, "--state", tmp_state, "--json"],
                capture_output=True, text=True, encoding="utf-8", check=True,
            )
            new_post = json.loads(r_post.stdout)
            with open(post_mutation_golden, "w", encoding="utf-8", newline="\n") as f:
                json.dump(new_post, f, indent=2)
                f.write("\n")
            print(f"  post-mutation.json updated (id: {new_post['diagnosis_id']})")
        finally:
            shutil.rmtree(tmp2, ignore_errors=True)

print("\nDone.")
