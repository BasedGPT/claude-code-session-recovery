# Claude Code Desktop Session Recovery & Repair Tools

> Tested against Claude Code CLI v2.1.121 on Windows 11 — 2026-05-19. Claude Desktop version detected at runtime by `diagnose.py`.

Your Claude Desktop session list is blank, or sessions are opening with no conversation history. This repo fixes that.

## Where to start

**Sessions in your Claude Desktop session list are broken or missing** → [docs/session-recovery.md](docs/session-recovery.md)

**You use Claude Code worktrees and something broke** → [docs/worktree-lifecycle.md](docs/worktree-lifecycle.md)

**You want to understand the bug before running anything** → [docs/architecture.md](docs/architecture.md)

**You want to know what the scripts touch before running them** → [SECURITY.md](SECURITY.md)

**You are an AI assistant helping a user with this repo** → [AGENTS.md](AGENTS.md)

## Symptom table

Run `python tools/diagnose.py` first — it identifies your specific problem and prints the exact command to run.

| Problem | Run this | Details |
|---|---|---|
| Sessions appear in the session list but open with no conversation history | `python tools/diagnose.py` | [session-recovery.md#blank-pane-missing-cli](docs/session-recovery.md#blank-pane-missing-cli) |
| Session is in the session list but its conversation history is missing from disk | `python tools/diagnose.py` | [session-recovery.md#cli-points-missing-jsonl](docs/session-recovery.md#cli-points-missing-jsonl) |
| Two sessions in the session list open to the same conversation history | `python tools/diagnose.py` | [session-recovery.md#duplicate-synth-metadata](docs/session-recovery.md#duplicate-synth-metadata) |
| Sessions started from a bare drive root rather than a project folder | `python tools/diagnose.py` | [session-recovery.md#old-root-cwd-reference](docs/session-recovery.md#old-root-cwd-reference) |
| One project shows two sets of sessions in the session list | `python tools/diagnose.py` | [session-recovery.md#junction-realpath-slug-mismatch](docs/session-recovery.md#junction-realpath-slug-mismatch) |
| Sessions are absent from the session list even though their transcript files exist on disk | `python tools/diagnose.py` | [session-recovery.md#orphan-jsonl-no-metadata](docs/session-recovery.md#orphan-jsonl-no-metadata) |

## Quickstart

```
python tools/diagnose.py
```

Read the output. It prints what it found, the matched problem, and the exact repair command if one exists. The diagnostic is read-only — safe to run at any time.

Requirements: Python 3.11+, Windows 11. No dependencies outside the standard library.

---

*No warranty. These tools fix specific broken states and may not apply to yours. See [LICENSE](LICENSE).*
