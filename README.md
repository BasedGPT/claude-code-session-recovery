# Claude Code Desktop Session Recovery & Repair Tools

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

> Tested against Claude Code CLI v2.1.121 on Windows 11 — 2026-05-19. Claude Desktop version v1.8089.1

Something broke somewhere with your sessions in Claude Desktop. Because I'm a bit special I've broken my Desktop sessions in lots of different ways. I made this to help you diagnose and hopefully fix it if something has gone wrong for you too.

## Paste this into Claude Code to fix it

If you want Claude Code to diagnose and repair your sessions for you, paste this:

```
My Claude Code sessions are broken on Windows. Please help me fix them using https://github.com/BasedGPT/claude-code-session-recovery

1. Clone the repo to a temp folder
2. Read AGENTS.md — it's written for AI assistants and explains how to use this repo safely
3. Read docs/session-recovery.md to understand the symptoms and repair tools
4. Run `python tools/diagnose.py` — it auto-detects all file paths from the environment, no configuration needed
5. Read the output and run the repair command it recommends (dry-run first, then --apply)
6. Tell me what was found and what was fixed
```

## Quickstart

```
git clone https://github.com/BasedGPT/claude-code-session-recovery
cd claude-code-session-recovery
python tools/diagnose.py
```

Read the output. It prints what it found, the matched problem, and the exact repair command if one exists. The diagnostic is read-only — safe to run at any time.

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
| Session missing conversation history | `python tools/diagnose.py` | [session-recovery.md#blank-pane-missing-cli](docs/session-recovery.md#blank-pane-missing-cli) |
| Conversation history missing from disk | `python tools/diagnose.py` | [session-recovery.md#cli-points-missing-jsonl](docs/session-recovery.md#cli-points-missing-jsonl) |
| Two sessions, same conversation history | `python tools/diagnose.py` | [session-recovery.md#duplicate-synth-metadata](docs/session-recovery.md#duplicate-synth-metadata) |
| Sessions started from outside any project folder | `python tools/diagnose.py` | [session-recovery.md#old-root-cwd-reference](docs/session-recovery.md#old-root-cwd-reference) |
| One project, two sets of sessions | `python tools/diagnose.py` | [session-recovery.md#junction-realpath-slug-mismatch](docs/session-recovery.md#junction-realpath-slug-mismatch) |
| Sessions missing from Desktop session list | `python tools/diagnose.py` | [session-recovery.md#orphan-jsonl-no-metadata](docs/session-recovery.md#orphan-jsonl-no-metadata) |

---

Requirements: Python 3.11+, Windows 11. No dependencies outside the standard library.
