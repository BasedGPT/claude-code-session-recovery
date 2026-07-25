"""Keep the machine routing table and human troubleshooting guide aligned."""

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_every_routing_row_has_human_section_and_details_anchor():
    rows = json.loads((REPO_ROOT / "troubleshooting.json").read_text(encoding="utf-8"))
    troubleshooting = (REPO_ROOT / "troubleshooting.md").read_text(encoding="utf-8")

    for row in rows:
        problem_id = row["id"]
        assert f"### {problem_id}" in troubleshooting

        details_path, anchor = row["details"].split("#", 1)
        details = (REPO_ROOT / details_path).read_text(encoding="utf-8")
        assert f'id="{anchor}"' in details
