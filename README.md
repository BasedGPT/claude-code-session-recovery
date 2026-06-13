# Claude Code Desktop Session Recovery & Repair Tools

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

> Tested against Claude Code CLI v2.1.121 on Windows 11 — 2026-05-19. Claude Desktop version v1.8089.1. macOS confirmed by community (path adjustments only). Windows MSIX (Microsoft Store) install: `diagnose.py` works; write-bearing repair scripts do not — see [MSIX note](#msix-microsoft-store-installs) below.

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
| Conversation history missing from disk | `python tools/diagnose.py` | [session-recovery.md#cli-points-missing-jsonl](docs/session-recovery.md#cli-points-missing-jsonl) — on Windows, VSS shadow copies are searched automatically via [restore\_from\_vss.py](tools/sessions/restore_from_vss.py) |
| Two sessions, same conversation history | `python tools/diagnose.py` | [session-recovery.md#duplicate-synth-metadata](docs/session-recovery.md#duplicate-synth-metadata) |
| Sessions started from outside any project folder | `python tools/diagnose.py` | [session-recovery.md#old-root-cwd-reference](docs/session-recovery.md#old-root-cwd-reference) |
| One project, two sets of sessions | `python tools/diagnose.py` | [session-recovery.md#junction-realpath-slug-mismatch](docs/session-recovery.md#junction-realpath-slug-mismatch) |
| Sessions missing from Desktop session list | `python tools/diagnose.py` | [session-recovery.md#orphan-jsonl-no-metadata](docs/session-recovery.md#orphan-jsonl-no-metadata) |
| Group assignments wiped or missing after Desktop update | `python tools/groupings/list_groupings.py` | [architecture.md#session-grouping-layer](docs/architecture.md#session-grouping-layer) — read-only diagnostic; no automated fix |

---

## Known patterns

### `transcriptUnavailable: true` — startup scanner regression (cross-platform)

The Desktop startup scanner can remove `cliSessionId` from metadata files and set `transcriptUnavailable: true`, even when the transcript file is intact on disk. Sessions affected by this show in the sidebar with title and folder intact but open with no conversation history.

This was originally documented for macOS Desktop 2.1.144+ but is confirmed on Windows (Claude Code desktop v1.11187.1, 2026-06-04). Treat it as cross-platform.

`diagnose.py` surfaces affected sessions. `repair_session_metadata.py --apply` backfills the `cliSessionId` link and removes the flag.

**Note on bridged/cloud sessions:** one reporter observed that sessions "bridged to remote/cloud" showed this pattern at a higher rate than local sessions. Working hypothesis: bridged sessions may not write a local `.jsonl`, so the scanner finds no file to match and sets the flag. If confirmed, those sessions would not be recoverable via disk repair — the transcript was never local. `diagnose.py` will tell you whether a `.jsonl` exists for each affected session before you run any repair.

### VSS recovery and NTFS junctions

`restore_from_vss.py` searches Windows VSS (Volume Shadow Copy) snapshots automatically when the `cli-points-missing-jsonl` symptom is detected. There is one configuration where VSS finds nothing: if `~/.claude` is redirected via an NTFS junction to a cloud drive — for example `C:\Users\<username>\.claude` → `D:\Dropbox\.claude` — VSS snapshots follow the junction target. The shadows reflect the cloud-drive folder, not the state of `~/.claude` before the junction existed.

In this setup, check the cloud provider's own version history instead. The junction target folder is versioned by Dropbox, OneDrive, or whatever service it points at.

---

## MSIX (Microsoft Store) installs

`diagnose.py` is read-only and works correctly on MSIX installs — it reads files directly from the real package path and gives an accurate picture of session state.

The write-bearing scripts (`repair_session_metadata.py`, `synth_session_metadata.py`) **will not surface sessions** on an MSIX install. Claude Desktop on MSIX maintains an internal session index that takes precedence over metadata files written externally. Write access to the real package path (`%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\`) is available to Python scripts, but write access is not sufficient — a full 24-entry synthesis pass confirmed zero errors and no Desktop change (community-confirmed 2026-06-10). Desktop does not load transcripts from external metadata entries and overwrites externally set values (title, timestamps, `completedTurns`) on restart.

`diagnose.py` detects the install type automatically and prints this note if MSIX is found. The EXE (winget) installer does not have this limitation.

---

## Mapped network drives (Windows, VS Code extension)

If your workspace is on a mapped Windows drive (`N:\`, `Z:\`, etc.), the VS Code extension's session history sidebar may show no past sessions even though the sessions exist on disk and `claude --resume` works fine in the integrated terminal.

The CLI writes project slugs from the literal drive-letter path (`N:\path\to\project` → `n--path-to-project`). The VS Code extension resolves drive letters to UNC paths via Node's `fs.realpath()` (`N:\` → `\\server\share\`) and derives a different slug from that UNC form. The two encoding paths disagree, so the extension searches under a project directory that doesn't exist.

`diagnose.py` detects this and prints a NOTE when affected project directories are found. The Desktop app and `claude --resume` / `--continue` are not affected.

**To fix the VS Code extension sidebar, choose one option:**

**Option A — open via UNC path.** Open VS Code using the UNC path (`\\server\share\path\to\project`) instead of the drive letter. This aligns the extension's encoding with the slug the CLI wrote. New sessions started this way will use the UNC-encoded slug automatically.

**Option B — rename the project directory.** Keep opening via the drive letter, but rename the existing project directory to match what the extension expects.

1. Find the UNC path your drive letter resolves to:
   ```powershell
   [System.IO.Path]::GetFullPath("N:\\")   # replace N with your drive letter
   ```
2. Encode the result as a project slug: replace `\\` with `--` and `\` with `-`. For example, `\\Qnap01\Qnap1\my\project` → `--Qnap01-Qnap1-my-project`.
3. Rename `~\.claude\projects\<drive-letter-slug>\` to `~\.claude\projects\<unc-slug>\`.
4. If any metadata files in `%APPDATA%\Claude\claude-code-sessions\` have a `cwd` field set to the drive-letter path, update them to the UNC path — otherwise the Desktop app may show sessions but the stored cwd will be stale.

This is a known VS Code extension limitation (`anthropics/claude-code#31219`, closed as not planned). Community-confirmed on Windows 10 and Windows 11 across multiple drive types (NAS via SMB, UNC-mapped drives).

---

## Prevention — stop it happening again

The tools above repair problems after they occur. These run proactively, so you have a clean recovery path if something goes wrong next time.

**Two directories matter.** A complete backup needs both `%APPDATA%\Claude\claude-code-sessions\` (Desktop metadata — the session index) and `~\.claude\projects\` (transcript files — conversation history). Backing up transcripts alone leaves you with orphaned `.jsonl` files that won't appear in the Desktop sidebar; you'd need to run `synth_session_metadata.py` on top of the restore to link them. The `backup_claude_state.py` script below covers both layers automatically.

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

**Before restoring from a backup zip:** set `"cleanupPeriodDays": 36500` in `~/.claude/settings.json` first. The backup preserves original file timestamps, and Claude Code's cleanup deletes JSONLs by filesystem mtime — not by message date. Any JSONL older than 30 days by mtime will be re-deleted on next launch if you restore without this step. See [docs/session-recovery.md](docs/session-recovery.md#cli-points-missing-jsonl) for the full restore sequence.

---

### Set `cleanupPeriodDays` high

In `~/.claude/settings.json`:

```json
{
  "cleanupPeriodDays": 36500
}
```

This tells Claude Code to keep transcripts for approximately 100 years. The default is 30 days, which is aggressive if you want to preserve long-running project history.

**Caveat:** three documented paths bypass this setting regardless of its value — SDK subagent sessions (`settingSources: []`), CLI invocations with `--setting-sources local`, and sessions where `cleanupPeriodDays` resolves to `0`. The backup covers you when the setting is bypassed.

---

### Session start watch: `tools/sessions/session_watch.py`

**`session_watch.py`** — a Claude Code `SessionStart` hook that detects transcript loss as it happens. On each session start it scans `~/.claude/projects/**/*.jsonl`, compares sha256, size, and mtime against the previous run's manifest, and emits a timestamped ALERT (to stderr and `watch.log`) if any transcript disappeared or shrank — including the `cleanupPeriodDays` value and the `prev → current` version transition, so you know exactly when loss occurred and whether the configured retention should have prevented it. Silent on the happy path; exits 0 always.

```json
// .claude/settings.json — merge with existing hooks if present
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python C:/absolute/path/to/tools/sessions/session_watch.py"
          }
        ]
      }
    ]
  }
}
```

Credit: mirrors `claude-transcript-watch.sh` by @AiTrillium ([anthropics/claude-code#62272](https://github.com/anthropics/claude-code/issues/62272)), reimplemented in Python for native Windows support.

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

```json
// .claude/settings.json — merge with existing hooks if present
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python C:/absolute/path/to/tools/worktrees/worktree_resume_rule.py"
          }
        ]
      }
    ]
  }
}
```

The full lifecycle policy — what "safe to shrink" means, how the queue works, what the quarantine folder is for — is in [docs/worktree-lifecycle.md](docs/worktree-lifecycle.md).

---

## Complementary tools

These are not part of this repo but pair well with the suite.

**`claude-transcript-watch.sh`** — a `SessionStart` hook by @AiTrillium ([shared on anthropics/claude-code#62272](https://github.com/anthropics/claude-code/issues/62272#issuecomment-4584631435)) that manifests all `.jsonl` transcript files on each Desktop launch and alerts if any disappear or shrink. Catches the deletion as it happens, with the `version → version` transition pinned in the log — useful for diagnosing which update triggered a cleanup. Bash + coreutils + python3 stdlib, no external packages. Works natively on macOS and Linux; Windows requires Git Bash or WSL.

A Python-native Windows equivalent is now available at [`tools/sessions/session_watch.py`](tools/sessions/session_watch.py) — see the [Session start watch](#session-start-watch-toolssessionssession_watchpy) entry in the Prevention section above.

**`clean-my-agent`** by [@blain3white](https://github.com/blain3white/clean-my-agent) — on-demand backup of `~/.claude/projects/` JSONLs to a timestamped local copy. Covers the transcript layer only — if you lose both transcripts and metadata (as in the `cleanupPeriodDays` bypass bug), restore the transcripts first, then run `synth_session_metadata.py` to rebuild the metadata layer before restarting Desktop.

---

## macOS path reference

The toolkit works on macOS with path adjustments. Two directories matter for diagnosis and repair:

| Layer | macOS path |
|---|---|
| **Desktop metadata** (session index the toolkit reads and repairs) | `~/Library/Application Support/Claude/claude-code-sessions/` |
| **Transcript files** (conversation history) | `~/.claude/projects/` |

> **`IndexedDB/` is not the metadata layer.** `~/Library/Application Support/Claude/IndexedDB/` is Electron's cloud bridge store — it holds session titles and sync state for Remote Control sessions. It is separate from what the toolkit reads and cannot be repaired by these scripts. If `diagnose.py` reports nothing found, check that you're pointing it at `claude-code-sessions/` above, not `IndexedDB/`.

---

Requirements: Python 3.11+. Windows 11 EXE (winget) install: full support. Windows MSIX (Microsoft Store) install: `diagnose.py` only — write-bearing scripts do not work (see [MSIX note](#msix-microsoft-store-installs)). macOS: community-confirmed with path adjustments; native macOS paths tracked in [#4](https://github.com/BasedGPT/claude-code-session-recovery/issues/4). No dependencies outside the standard library.
