# AGENTS.md — Claude Code Desktop Session Recovery & Repair Tools

Primer for any AI assistant helping a user with this repo. Six sections, in priority order.

---

## 1. Commands

**Diagnose (read-only — always safe to run first):**
```
python tools/diagnose.py
python tools/diagnose.py --json                                              # machine-readable
python tools/diagnose.py --state fixtures/02-blank-pane-missing-cli/state   # against a fixture
```

**Repair (run the command `diagnose.py` prints, not the script directly):**
```
python tools/sessions/<mutator>.py --diagnosis-id <hex>          # dry-run
python tools/sessions/<mutator>.py --diagnosis-id <hex> --apply  # apply
```

**Inspect worktrees (read-only PowerShell):**
```
pwsh tools/worktrees/worktree_inspector.ps1
```

**Inspect session groupings (read-only):**
```
python tools/groupings/list_groupings.py
python tools/groupings/list_groupings.py --quiet
```

---

## 2. Architecture

Claude sessions have two parts: Desktop metadata (`%APPDATA%\Claude\claude-code-sessions\...\local_*.json`) and CLI transcripts (`~\.claude\projects\<slug>\*.jsonl`). The `cliSessionId` field in the metadata file links them. Most session-history bugs are a broken or missing link. A diagnosis token is a deterministic 8-hex SHA-256 hash of the structural state snapshot — mutators require it and refuse to run if the state has changed since it was issued.

Shared implementation modules keep that behaviour consistent:

| Module | Responsibility |
|---|---|
| `tools/session_state.py` | Read-only state discovery, snapshot construction, matching, and diagnosis tokens |
| `tools/transcript_files.py` | Transcript discovery and JSONL interpretation |
| `tools/mutator_safety.py` | Policy-free mutation mechanics such as token checks and verified backups |
| `tools/worktrees/worktree_lifecycle.py` | Worktree marker, sentinel, and quiet-stub mechanics |
| `tests/fixture_scenarios.py` | Isolated fixture execution shared by verification and golden regeneration |

Executable scripts remain the command-line and reporting adapters. Keep
user-facing wording, exit codes, and safety policy local to those scripts.
Shared modules may own reusable platform facts and default path resolution so
Windows and macOS adapters cannot silently drift; the executable still reports
the resolved paths and retains the repair policy.

---

## 3. Safety boundaries

**Tier 1 — always:** Run `diagnose.py` first. Read its output before suggesting anything. It is read-only and safe at any time.

**Tier 2 — ask before running:** Before suggesting any mutator command, confirm Claude Desktop is fully quit using the platform-specific process-check command printed by `diagnose.py`, and the user has reviewed the dry-run output.

**Tier 3 — never:** Do not run any mutator while Claude Desktop is open. Desktop holds metadata files in memory and will overwrite repairs on its next flush.

---

## 4. Tech stack

- Python 3.11+, standard library runtime (stdlib `zoneinfo` used by some tools)
- Test dependency: `requirements-dev.txt`
- PowerShell 5.1+ for `worktree_inspector.ps1`
- Windows 11 and macOS; individual tools document platform-specific limits
- No external APIs, no network calls, no telemetry

---

## 5. File and directory locations

| What | Path |
|---|---|
| Desktop session metadata | Windows `%APPDATA%\Claude\claude-code-sessions\...`; macOS `~/Library/Application Support/Claude/claude-code-sessions/...` |
| CLI transcripts | Windows/macOS `~/.claude/projects/<slug>/*.jsonl` |
| Desktop install | `%LOCALAPPDATA%\AnthropicClaude\app-<version>\` |
| Backup and staging dirs | Script-specific directories documented by each mutator's dry-run output |
| Fixtures | `fixtures/<NN>-<name>/state/` |
| Golden outputs and exit contracts | `fixtures/<NN>-<name>/golden/` |
| Routing table | `troubleshooting.json` (source of truth), `troubleshooting.md` (human copy) |

---

## 6. Git workflow conventions

- Every change to the public tree is a versioned release. `VERSION` is the
  authority; each change advances numeric SemVer.
- Run `python tools/release.py check --base <base-sha> --head <head-sha>` before
  merging. The check requires the prior version's annotated tag.
- Releases are annotated tags (`v1.0.0`, `v1.0.1`, ...). The tag workflow
  re-runs tests and verifies the tag/version/commit binding.
- `main` is the stable branch. Pull requests and required CI checks are
  mandatory; force-pushes to `main` and updates/deletions of `v*` tags are not
  permitted.
- New mutators require a matching fixture and must pass the five mutator gates (see `docs/architecture.md`), including the dry-run exit and unchanged-state contracts.

---

*Maintainer: [@BasedGPT](https://github.com/BasedGPT)*
