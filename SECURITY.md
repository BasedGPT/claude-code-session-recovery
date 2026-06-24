# Security

## Threat model

Scripts in this repo read from these locations on your system:

**Windows (standard install)**
- `%APPDATA%\Claude\claude-code-sessions\<account-uuid>\<org-uuid>\local_*.json` — Desktop session metadata files
- `%USERPROFILE%\.claude\projects\<slug>\*.jsonl` — Claude Code conversation transcripts
- `%LOCALAPPDATA%\AnthropicClaude\` — directory listing only, for version detection
- `%APPDATA%\Claude\Local Storage\leveldb\*.ldb` and `*.log` — Desktop grouping state (read-only; `list_groupings.py` only)

**Windows (MSIX / Microsoft Store install)** — `diagnose.py` falls back to:
- `%LOCALAPPDATA%\Packages\Claude_<hash>\LocalCache\Roaming\Claude\claude-code-sessions\…`

**macOS**
- `~/Library/Application Support/Claude/claude-code-sessions/<account-uuid>/<org-uuid>/local_*.json`
- `~/.claude/projects/<slug>/*.jsonl`

**Linux**
- `~/.config/Claude/claude-code-sessions/<account-uuid>/<org-uuid>/local_*.json`
- `~/.claude/projects/<slug>/*.jsonl`

Mutating scripts write to:

- The specific metadata files they repair, in-place, after creating a backup
- `~/.claude/projects/<slug>/<uuid>.jsonl` — `restore_from_vss.py` only (Windows); writes recovered transcripts as new files, never overwrites an existing JSONL
- `.\repair-backup\` — a directory created next to where the script runs; originals go here before any mutation, and `restore_from_vss.py` writes a restore log here

No other files are touched. Scripts never touch the registry and never access credentials or the clipboard.

You must trust this repo's code before running it. The source is short and readable — start at `tools/diagnose.py`.

## Provenance

Commits are GPG-signed. The signing key is published in the GitHub profile for [@BasedGPT](https://github.com/BasedGPT).

**Until a tagged release exists,** pin to a specific commit SHA and verify the working tree matches:

```
git -C claude-code-session-recovery rev-parse HEAD
git -C claude-code-session-recovery diff --stat   # expect: empty
```

Once releases are tagged (`v1.0.0`, `v1.0.1`, …), each release tag will carry SHA256 hashes of all tool files in the release notes. At that point, verify before running:

```
certutil -hashfile tools/diagnose.py SHA256
certutil -hashfile tools/sessions/repair_session_metadata.py SHA256
```

Compare against the hashes published in the release notes for the version you are using.

## Safety contract

Every mutating script enforces these conditions before touching anything:

1. **Diagnosis token required.** Mutators will not run without a valid `--diagnosis-id` token produced by `diagnose.py` against the current state. A stale token (state has changed since the last diagnosis) causes a refusal.
2. **Backup before mutation.** Every mutator writes the original file to `.\repair-backup\<filename>` before modifying it. The backup is written and verified before any mutation proceeds.
3. **Schema probe before action.** If the state layout is not in the recognised fixture set, scripts enter audit-only mode and will not print or execute any mutation command.
4. **Quit Desktop first.** Scripts warn prominently if `claude.exe` appears in the process list. Running a mutator while Desktop is open risks Desktop overwriting your repair on its next memory flush.

## What scripts never do

- No network calls of any kind
- No telemetry or usage reporting
- No auto-update
- No writes outside the two state directories listed above and the local `.\repair-backup\` folder
- No registry reads or writes
- No clipboard access
- No process spawning beyond `tasklist`, `claude --version` (version detection), and PowerShell + `vssadmin` (`restore_from_vss.py` only, for VSS enumeration)

## Reproducibility

Every diagnosis is deterministic: the same filesystem state produces the same diagnosis ID, every time. The diagnosis ID is a SHA-256 hash of the structural state snapshot — not a timestamp or a random value. Two runs against the same unchanged state will always produce the same ID.

Every mutation is reversible from the backup: copy `.\repair-backup\<filename>` back to its original path. The rollback command is printed by every mutating script in its dry-run output before anything is written.

## Reporting security issues

Report security issues through [GitHub Security Advisories](https://github.com/BasedGPT/claude-code-session-recovery/security/advisories) — not through a public issue. Include the script name, what you believe is unsafe, and any proof-of-concept. The maintainer will respond within a few days.
