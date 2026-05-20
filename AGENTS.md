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

---

## 3. Safety boundaries

**Tier 1 — always:** Run `diagnose.py` first. Read its output before suggesting anything. It is read-only and safe at any time.

**Tier 2 — ask before running:** Before suggesting any mutator command, confirm Claude Desktop is fully quit (`tasklist /FI "IMAGENAME eq claude.exe"` returns no results) and the user has reviewed the dry-run output.

**Tier 3 — never:** Do not run any mutator while Claude Desktop is open. Desktop holds metadata files in memory and will overwrite repairs on its next flush.

---

## 4. Tech stack

- Python 3.11+, standard library only (stdlib `zoneinfo` used by some tools; no pip installs required)
- PowerShell 5.1+ for `worktree_inspector.ps1`
- Windows 11 (no macOS or Linux support in v1)
- No external APIs, no network calls, no telemetry

---

## 5. File and directory locations

| What | Path |
|---|---|
| Desktop session metadata | `%APPDATA%\Claude\claude-code-sessions\<account-uuid>\<org-uuid>\local_*.json` |
| CLI transcripts | `%USERPROFILE%\.claude\projects\<slug>\*.jsonl` |
| Desktop install | `%LOCALAPPDATA%\AnthropicClaude\app-<version>\` |
| Backup dir (created by mutators) | `.\repair-backup\` (relative to where the script runs) |
| Fixtures | `fixtures/<NN>-<name>/state/` |
| Golden outputs | `fixtures/<NN>-<name>/golden/` |
| Routing table | `troubleshooting.json` (source of truth), `troubleshooting.md` (human copy) |

---

## 6. Git workflow conventions

- Commits are signed. Releases are tagged (`v1.0.0`, `v1.0.1`, ...).
- Each release tag carries SHA256 hashes of all tool files in the release notes.
- `main` is the stable branch. No force-push to `main`.
- New mutators require a matching fixture and must pass the five mutator gates (see `docs/architecture.md`).

---

*Maintainer: [@BasedGPT](https://github.com/BasedGPT)*
