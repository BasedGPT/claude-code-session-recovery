"""Inventory local-agent session containers without reading their contents.

The report is aggregate-only: counts grouped into root, owner, ``local_*``, and
``outputs`` buckets. Identifiers appear only as opaque subjects on bounded
errors. LevelDB stores are never parsed, and no product/mode classification
(including Cowork) is attempted.
"""

import argparse
import os
import sys

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _TOOLS_DIR)
from platform_support import default_claude_appdata_dir  # noqa: E402
from sidecar_common import (  # noqa: E402
    ScanState,
    bounded_directory_entries,
    entry_kind,
    expected_path_kind,
    write_json,
)


def inventory(root, *, max_directory_entries=20000, max_owners=10000,
              max_sessions=100000, max_output_entries=1000000):
    state = ScanState()
    owner_total = 0
    session_total = 0
    outputs_directory_total = 0
    output_entry_total = 0

    root_kind = expected_path_kind(
        root, state, expected="directory", subject_namespace="agent-root"
    )
    root_status = "present" if root_kind == "directory" else root_kind
    if root_kind != "directory":
        return _result(
            state,
            root_status,
            owner_total,
            session_total,
            outputs_directory_total,
            output_entry_total,
            max_directory_entries,
            max_owners,
            max_sessions,
            max_output_entries,
        )

    accounts = bounded_directory_entries(
        root, state, cap=max_directory_entries, subject_namespace="agent-root"
    )
    stop = False
    for account in accounts:
        if entry_kind(account, state, subject_namespace="agent-account") != "directory":
            continue
        organisations = bounded_directory_entries(
            account.path, state, cap=max_directory_entries,
            subject_namespace="agent-account",
        )
        for organisation in organisations:
            if entry_kind(
                organisation, state, subject_namespace="agent-owner"
            ) != "directory":
                continue
            if owner_total >= max_owners:
                state.cap("owner_cap_reached")
                stop = True
                break
            owner_total += 1
            sessions = bounded_directory_entries(
                organisation.path,
                state,
                cap=max_directory_entries,
                subject_namespace="agent-owner",
            )
            for session in sessions:
                if not session.name.startswith("local_"):
                    continue
                if entry_kind(
                    session, state, subject_namespace="agent-local"
                ) != "directory":
                    continue
                if session_total >= max_sessions:
                    state.cap("session_cap_reached")
                    stop = True
                    break
                session_total += 1
                outputs_path = os.path.join(session.path, "outputs")
                outputs_kind = expected_path_kind(
                    outputs_path,
                    state,
                    expected="directory",
                    subject_namespace="agent-outputs",
                )
                if outputs_kind == "directory":
                    outputs_directory_total += 1
                    remaining = max_output_entries - output_entry_total
                    if remaining <= 0:
                        state.cap("output_entry_cap_reached")
                        stop = True
                    else:
                        outputs = bounded_directory_entries(
                            outputs_path,
                            state,
                            cap=max_directory_entries,
                            subject_namespace="outputs",
                        )
                        count = min(len(outputs), remaining)
                        output_entry_total += count
                        if len(outputs) > remaining:
                            state.cap("output_entry_cap_reached")
                            stop = True
                if stop:
                    break
            if stop:
                break
        if stop:
            break

    return _result(
        state,
        "present",
        owner_total,
        session_total,
        outputs_directory_total,
        output_entry_total,
        max_directory_entries,
        max_owners,
        max_sessions,
        max_output_entries,
    )


def _result(state, root_status, owner_total, session_total,
            outputs_directory_total, output_entry_total,
            max_directory_entries, max_owners, max_sessions,
            max_output_entries):
    return {
        "audit": "local_agent_session_inventory",
        **state.fields(),
        "buckets": {
            "root": {"status": root_status, "count": 1 if root_status == "present" else 0},
            "owner": {"count": owner_total},
            "local_*": {"count": session_total},
            "outputs": {
                "directory_count": outputs_directory_total,
                "entry_count": output_entry_total,
            },
        },
        "limits": {
            "max_directory_entries": max_directory_entries,
            "max_owners": max_owners,
            "max_sessions": max_sessions,
            "max_output_entries": max_output_entries,
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--state", help="Fixture state root")
    parser.add_argument(
        "--root",
        default=os.path.join(default_claude_appdata_dir(), "local-agent-mode-sessions"),
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--max-directory-entries", type=int, default=20000)
    parser.add_argument("--max-owners", type=int, default=10000)
    parser.add_argument("--max-sessions", type=int, default=100000)
    parser.add_argument("--max-output-entries", type=int, default=1000000)
    args = parser.parse_args(argv)
    root = (
        os.path.join(os.path.abspath(args.state), "appdata", "Claude",
                     "local-agent-mode-sessions")
        if args.state else args.root
    )
    result = inventory(
        root,
        max_directory_entries=max(1, args.max_directory_entries),
        max_owners=max(1, args.max_owners),
        max_sessions=max(1, args.max_sessions),
        max_output_entries=max(1, args.max_output_entries),
    )
    if args.json:
        write_json(result)
    else:
        print("Local-agent session inventory: {}".format(result["status"]))
        print("Root: {}".format(result["buckets"]["root"]["status"]))
        print("Owners: {}".format(result["buckets"]["owner"]["count"]))
        print("local_* sessions: {}".format(result["buckets"]["local_*"]["count"]))
        print("outputs directories: {}".format(
            result["buckets"]["outputs"]["directory_count"]
        ))
        print("outputs entries: {}".format(
            result["buckets"]["outputs"]["entry_count"]
        ))
        print("Errors: {}".format(result["error_count"]))
    return 2 if result["partial"] else 0


if __name__ == "__main__":
    sys.exit(main())
