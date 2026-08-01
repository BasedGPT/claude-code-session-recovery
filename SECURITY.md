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

Session mutators write to:

- The specific metadata files they repair, in-place, after creating a backup
- `~/.claude/projects/<slug>/<uuid>.jsonl` — `restore_from_vss.py` only (Windows); writes recovered transcripts as new files, never overwrites an existing JSONL
- Script-specific backup or staging directories. Each mutator prints the exact path and rollback command in its dry-run output.

The worktree lifecycle tools have a separate write boundary documented in
[`docs/worktree-lifecycle.md`](docs/worktree-lifecycle.md). Scripts never touch
the registry and never access credentials or the clipboard.

You must trust this repo's code before running it. The source is short and readable — start at `tools/diagnose.py`.

## Provenance

Commits are not required to carry a GPG signature. Pin and verify the exact
commit you intend to run.

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

The toolkit applies these conditions before mutation:

1. **Diagnosis token required.** Mutators will not run without a valid `--diagnosis-id` token produced by `diagnose.py` against the current state. A stale token (state has changed since the last diagnosis) causes a refusal.
2. **Backup before mutation.** A mutator that rewrites an existing file creates and verifies its documented backup before changing the live file. Mutators that create a new destination refuse unsafe overwrites.
3. **Schema probe before action.** If the state layout is not in the recognised fixture set, scripts enter audit-only mode and will not print or execute any mutation command.
4. **Quit Desktop first.** This is an operator precondition for every live-state mutation. Scripts that can make a reliable platform-specific process check enforce it directly; other scripts print the precondition and rely on the operator to verify it. Running a mutator while Desktop is open risks Desktop overwriting the repair on its next memory flush.

## What scripts never do

- No network calls of any kind
- No telemetry or usage reporting
- No auto-update
- No undocumented write destinations; every mutator prints its destination and rollback path before applying
- No registry reads or writes
- No clipboard access
- Session tools spawn only their documented local helpers: Windows `tasklist`, macOS `pgrep`/`ps`, `claude --version` for version detection, and PowerShell plus `vssadmin` for `restore_from_vss.py`. Worktree tools invoke Git as documented in the lifecycle guide.

## Reproducibility

Every diagnosis is deterministic: the same filesystem state produces the same diagnosis ID, every time. The diagnosis ID is a SHA-256 hash of the structural state snapshot — not a timestamp or a random value. Two runs against the same unchanged state will always produce the same ID.

Every rewrite of an existing file is reversible from the verified backup. The rollback command is printed by the mutating script in its dry-run output before anything is written.

## Reporting security issues

Report security issues through [GitHub Security Advisories](https://github.com/BasedGPT/claude-code-session-recovery/security/advisories) — not through a public issue. Include the script name, what you believe is unsafe, and any proof-of-concept. The maintainer will respond within a few days.
