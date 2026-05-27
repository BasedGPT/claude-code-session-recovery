# Claude Code Desktop Session Recovery & Repair Tools

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

> Tested against Claude Code CLI v2.1.121 on Windows 11 — 2026-05-19. Claude Desktop version v1.8089.1

Something broke somewhere with your sessions in Claude Desktop. Because I'm a bit special I've broken my Desktop sessions in lots of different ways. I made this to help you diagnose and hopefully fix it if something has gone wrong for you too.

## Fix it with Claude Code (or any AI)

This runs best from a **Claude Code CLI session**, not from Claude Desktop because the Desktop app needs to be closed to apply the fixes. If you run it from Desktop then, the tool will detect that and warn you and give you the instructions on what to do if you're still more comfortable running from Claude Desktop.

**Step 1 — open a CLI session**

Open Windows Terminal, PowerShell, or cmd, then run:

```
claude
```

That starts Claude Code in your terminal.

**Step 2 — paste this prompt**

```
My Claude Code sessions are broken on Windows. Please help me fix them using https://github.com/BasedGPT/claude-code-session-recovery

1. Clone the repo and cd into it: `git clone https://github.com/BasedGPT/claude-code-session-recovery` then `cd claude-code-session-recovery`
2. Read AGENTS.md — it's written for AI assistants and explains how to use this repo safely
3. Read docs/session-recovery.md to understand the symptoms and repair tools
4. Run `python tools/diagnose.py` — it auto-detects all file paths from the environment, no configuration needed
5. For each repair command: run the dry-run first, show me the full output, and wait for me to say "yes, apply" before adding --apply
6. Tell me what was found and what was fixed
```

## Fix it yourself

```
git clone https://github.com/BasedGPT/claude-code-session-recovery
cd claude-code-session-recovery
python tools/diagnose.py
```

Read the output. It prints what it found, the matched problem, and the exact repair command. The diagnostic is read-only — safe to run at any time.

## Where to start

**Sessions in your Claude Desktop session list are broken or missing** → [docs/session-recovery.md](docs/session-recovery.md)

**You use Claude Code worktrees and something broke** → [docs/worktree-lifecycle.md](docs/worktree-lifecycle.md)

**You want to understand the bug before running anything** → [docs/architecture.md](docs/architecture.md)

**You want to know what the scripts touch before running them** → [SECURITY.md](SECURITY.md)

**You want to see which sessions are in which group, or groups disappeared after a Desktop update** → `python tools/groupings/list_groupings.py`

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
| Group assignments wiped or missing after Desktop update | `python tools/groupings/list_groupings.py` | [architecture.md#session-grouping-layer](docs/architecture.md#session-grouping-layer) — read-only diagnostic; no automated fix |

---

## Prevention — stop it happening again

The tools above repair problems after they occur. These two run proactively, so you have a clean recovery path if something goes wrong next time.

### Weekly backup: `tools/sessions/backup_claude_state.py`

Takes a compressed snapshot of all three data layers that Claude Code depends on:

- **Desktop metadata** — `%APPDATA%\Claude\claude-code-sessions\` (the session index Desktop reads on startup)
- **JSONL transcripts** — `~\.claude\projects\` (the actual conversation history)
- **FTS5 transcript index** — if you have one configured

Each layer is written to a dated zip under a `BACKUPS_ROOT` you configure at the top of the script. Old snapshots are automatically sent to the Recycle Bin — the default keeps the last 5 backups.

**Run it manually:**

```
python tools/sessions/backup_claude_state.py
python tools/sessions/backup_claude_state.py --dry-run   # see what would be zipped
```

**Schedule it daily (Task Scheduler):**

| Field | Value |
|---|---|
| Program | `py` |
| Arguments | `-3 "C:\path\to\tools\sessions\backup_claude_state.py"` |
| Start In | your repo root |
| Trigger | Daily, 6:00 AM |
| Run As | your user account |

Running while Desktop is open is fine — all source operations are read-only.

---

### Worktree lifecycle: `tools/worktrees/`

If you use Claude Code's worktree feature, removing a worktree the normal way (`git worktree remove`) permanently destroys any session data that was never committed — including `.env` files, scratch notes, and anything not tracked by git. It also orphans the Desktop session entries, which then appear in your session list with no content.

This suite manages the lifecycle safely:

**`worktree_shrink.py`** — instead of deleting a merged worktree, shrinks it from a full working tree (~45 MB) down to a bare stub (~2 KB). The branch history and session data remain reachable; the disk footprint disappears. Runs a 9-step pipeline with a manifest and quarantine folder so a partial failure is always recoverable.

```
python tools/worktrees/worktree_shrink.py <name>           # dry-run
python tools/worktrees/worktree_shrink.py <name> --apply   # shrink (branch must be merged)
python tools/worktrees/worktree_shrink.py --queue --apply  # process all marked-for-shrink worktrees
```

**`backfill_recovery_stubs.py`** — if you already have bare stubs from earlier recovery work or manual removal, this quiets them. Without it, every bare stub reports a "large number of uncommitted changes" banner in Claude Code at session start.

```
python tools/worktrees/backfill_recovery_stubs.py           # dry-run
python tools/worktrees/backfill_recovery_stubs.py --apply
```

**`worktree_resume_rule.py`** — a Claude Code `SessionStart` hook. When you open a worktree that was queued for shrinking (has a `.shrink-when-safe` marker), it removes the marker so the shrink queue skips it. Keeps "continue working on this branch" as a first-class action — the human session always wins.

```yaml
# .claude/settings.json
hooks:
  SessionStart:
    - python /absolute/path/to/tools/worktrees/worktree_resume_rule.py
```

The full lifecycle policy — what "safe to shrink" means, how the queue works, what the quarantine folder is for — is in [docs/worktree-lifecycle.md](docs/worktree-lifecycle.md).

---

Requirements: Python 3.11+, Windows 11 (macOS supported via `--state`; native macOS paths tracked in [#4](https://github.com/BasedGPT/claude-code-session-recovery/issues/4)). No dependencies outside the standard library.
