# Claude Code Desktop Session Recovery & Repair Tools

> Tested against Claude Code CLI v2.1.121 / Claude Desktop vX.Y.Z on Windows 11 — 2026-05-19

Your Claude Desktop history is blank, or sessions are opening to empty panes. This repo fixes that.

## Where to start

**Your Claude Desktop session history is broken** → [docs/session-recovery.md](docs/session-recovery.md)

**You use Claude Code worktrees and something broke** → [docs/worktree-lifecycle.md](docs/worktree-lifecycle.md)

## Symptom table

Run `python tools/diagnose.py` first — it identifies your specific problem and prints the exact command to run.

| Problem | Run this | Details |
|---|---|---|
| Claude Desktop history shows old sessions, but clicking them shows a blank pane | `python tools/diagnose.py` | [session-recovery.md#blank-pane](docs/session-recovery.md#blank-pane) |
| Claude Desktop shows a session in history but opening it shows no conversation content | `python tools/diagnose.py` | [session-recovery.md#missing-jsonl](docs/session-recovery.md#missing-jsonl) |
| Two entries in Claude Desktop history point to the same conversation | `python tools/diagnose.py` | [session-recovery.md#duplicate-synth-metadata](docs/session-recovery.md#duplicate-synth-metadata) |
| Sessions show history but are listed under a bare drive root rather than a project folder | `python tools/diagnose.py` | [session-recovery.md#old-root-cwd-reference](docs/session-recovery.md#old-root-cwd-reference) |
| Sessions started from a Windows junction path show separate history from sessions at the real path | `python tools/diagnose.py` | [session-recovery.md#junction-realpath](docs/session-recovery.md#junction-realpath) |
| Claude Desktop does not show some sessions in history at all, even though conversation transcripts exist on disk | `python tools/diagnose.py` | [session-recovery.md#orphan-jsonl](docs/session-recovery.md#orphan-jsonl) |

## Quickstart

```
python tools/diagnose.py
```

Read the output. It prints what it found, the matched problem, and the exact repair command if one exists. The diagnostic is read-only — safe to run at any time.

Requirements: Python 3.11+, Windows 11. No dependencies outside the standard library.

---

*No warranty. These tools fix specific broken states and may not apply to yours. See [LICENSE](LICENSE).*
