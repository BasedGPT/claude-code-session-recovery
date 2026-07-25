# Worktree lifecycle

> This document describes the maintainer's chosen approach to Claude Code worktree lifecycle, developed over several months of production use. It is not authoritative Claude Code architecture and is not required by Claude Code itself. The tools described here implement a specific workflow — adopt only the parts that fit your setup.

The content below covers two distinct areas. The bug-fix tools (`recover_deleted_branches_worktrees.py`, `inventory_broken_worktree_sessions.py`, `audit_root_cwd_sessions.py`, `sweep_junction_canonical_cwds.py`, `backfill_recovery_stubs.py`) fix real Claude Code bugs that any worktree user might encounter — they are documented with the same authority as the session-recovery tools. The lifecycle-shaped tools (`worktree_shrink.py`, `worktree_resume_rule.py`, `worktree_inspector.ps1`) implement one specific workflow that may or may not match your setup.

---

## Bug-fix tools

These tools fix real Claude Code bugs. Adopt them if the bug applies to you.

### `recover_deleted_branches_worktrees.py`

**Problem it solves:** A worktree branch was deleted and the worktree folder is gone. `git worktree list` shows nothing, but session history for that worktree still exists in Desktop. If merge commits on `master` can reconstruct the branch tip, this tool recreates the branch and stubs the worktree.

Invoked via `diagnose.py`. Run `python tools/diagnose.py` first — it identifies whether your state matches this repair and provides the exact command.

### `inventory_broken_worktree_sessions.py`

Read-only audit. Lists session metadata files where the recorded `cwd` points at a worktree path that no longer exists on disk. Use this to triage which sessions are affected before deciding whether to repair or accept the loss.

### `audit_root_cwd_sessions.py`

Read-only audit. Finds sessions with a `claude/*` branch name (indicating a worktree session) but a `cwd` pointing at the bare project root rather than a worktree path. These sessions were typically started before the worktree setup was complete — the branch exists but the path binding is wrong.

### `sweep_junction_canonical_cwds.py`

Read-only audit. Walks all session metadata and classifies `cwd` prefixes as junction, canonical, bare-root, or other. This is the input to the junction-realpath diagnosis in `diagnose.py`. Run it directly if you want a raw count of how many sessions use junction vs. canonical paths before deciding whether to fix.

### `backfill_recovery_stubs.py`

**Problem it solves:** A worktree folder is missing from disk but still registered in `git worktree list`. Without intervention, `git status` inside that registration reports every tracked file as a staged deletion, producing the "2103 uncommitted changes" banner at session start inside any recovery stub.

This tool creates a quiet `--no-checkout` stub at the original path, silencing the banner without removing the git registration or touching the branch.

See [Stub status quietness](#stub-status-quietness) below for the exact mechanism.

---

## Lifecycle-shaped tools

The following tools implement a specific approach to worktree lifecycle. Each script's docstring opens with: "Implements the maintainer's chosen lifecycle. Not required by Claude Code. Adopt only if this workflow matches what you want."

### Lifecycle states

A worktree exists in one of three states:

| State | What's on disk | Git registration |
|---|---|---|
| **Materialised** | Full tree (~45 MB typical) — tracked files at branch tip, untracked files, build artifacts | Present in `git worktree list` |
| **Stub** | `.git` pointer file only (~2 KB) + per-worktree sparse-checkout config keeping `git status` quiet | Present in `git worktree list`, `--no-checkout` mode |
| **Quarantined** | Folder moved to `.shrink-quarantine/<name>-<ts>/` | Not in `git worktree list` |

The transition called **shrink** = materialised → quarantined, then a new stub created at the original path.

### Core rules

These are the settled rules behind the lifecycle:

1. Branches are never auto-deleted. Branches are bytes; loss is real.
2. Anything shrunk goes to `.shrink-quarantine/`. Never auto-purged.
3. Commit before wrap, wrap before shrink. The branch is the durable record.
4. Done sessions merge to master at wrap. WIP sessions skip merge and stay materialised.
5. The shrink marker is a hint; branch state is the source of truth. Always re-verify before acting.
6. Time is a sort signal, never a delete rule. Age does not imply abandonment.
7. Destructive action requires a recovery remedy. Quarantine, never delete outright.

### Wrap-up flow

Runs once per session at close. Cannot shrink the worktree the session is running inside (file locks prevent it).

1. Commit everything worth keeping to the branch.
2. Decide: done or WIP?
3. If done: merge the branch to master (fast-forward or `--no-ff` as fits the history). Drop a `.shrink-when-safe` marker at `.claude/worktrees/<name>/`.
4. If WIP: skip merge, skip marker, leave materialised.

**Resume rule:** Any session that opens a worktree containing `.shrink-when-safe` removes the marker first, unless it is explicitly running the shrink processor. Continuing work after marking done is a first-class path.

### `worktree_shrink.py`

Processes the shrink queue — materialised worktrees with a `.shrink-when-safe` marker. Designed to run from a different worktree (or the main working tree), never from inside the worktree being shrunk.

**Safety gates (checked before any move):**

1. **Atomic claim.** Rename `.shrink-when-safe` → `.shrink-in-progress.<pid>.<ts>`. If the rename fails, another processor owns it; skip.
2. **Branch verification.** Check the branch is merged (default `--merged`), squash-merged (`--squash-merged`), or explicitly abandoned (`--allow-unmerged`).
3. **Content check.** The folder contains nothing outside the dotfile allowlist (see below).
4. **Not the current cwd.** The script refuses to shrink the worktree it is running inside.
5. **Filesystem lock check.** A no-op rename of the folder. Catches every kind of lock-holder — Desktop window, terminal, IDE, cloud sync.

**Three branch modes:**

| Mode | When to use |
|---|---|
| `--merged` (default) | Branch merged via `git merge` — SHAs match on master |
| `--squash-merged` | Branch was squash-merged — content is on master but SHAs differ |
| `--allow-unmerged` | Genuinely abandoning a branch with unique commits (commits stay reachable via the branch ref, which is never deleted) |

**Manifest:** A JSON manifest is written to the quarantine target before any move, then updated after each phase. On failure at any phase, the tool stops and leaves the manifest in its current state. `--resume <manifest>` picks up at the last idempotent step.

**Sentinel file:** After a successful shrink, the stub path contains `.worktree-shrunk.txt` explaining what happened, where the quarantine folder is, and how to restore:

```
This worktree was shrunk to save disk space.

Branch: claude/<name>
Quarantine: .claude/worktrees/.shrink-quarantine/<name>-<ts>/
Shrunk: <ISO timestamp>

To rematerialise: from this directory, run
  git checkout <branch> -- .

To restore the original folder (untracked files included):
  Move-Item .claude/worktrees/.shrink-quarantine/<name>-<ts>/ .

The branch is preserved at <head_sha>.
```

**Dotfile allowlist:**

Disposable (purged at shrink, not preserved in quarantine): `node_modules/`, `__pycache__/`, `.pytest_cache/`, `.next/`, `dist/`, `build/`, `.cache/`, coverage outputs.

Preserved-in-quarantine (moved with the folder, listed in the manifest): `.playwright-mcp/`, `.tmp_audit/`, `.transcript-index/`, `.dxt-sources/`, `.obsidian/`, `.agents/`.

Anything else outside the allowlist causes the shrink to refuse. The user must commit it, add it to the allowlist, or remove it manually before proceeding.

### `worktree_resume_rule.py`

A Claude Code SessionStart hook. When a session opens a worktree that has a `.shrink-when-safe` marker, this hook removes the marker before the session starts — preventing a shrink from running while the worktree is in active use.

Install as a session-start hook in your Claude Code configuration.

### `worktree_inspector.ps1`

Read-only PowerShell classifier. Classifies each registered worktree into one of eight health buckets:

1. **Healthy stub** — registered, only `.git` on disk, branch ref valid
2. **Active materialised** — registered, files present, live session detected (lock present or Desktop window open to this path)
3. **Clean inactive materialised** — registered, files present, no live session, eligible for shrink
4. **Dirty materialised** — uncommitted tracked changes
5. **Unknown local files** — untracked files outside the dotfile allowlist
6. **Broken registration** — `git worktree list` says it exists but the folder doesn't, or vice versa
7. **Locked zombie** — filesystem lock held by an unknown process
8. **Shrink in progress** — folder contains a `.shrink-in-progress.*` marker (previous shrink crashed or is still running)

Healthy stubs are hidden by default. The inspector never mutates; it reports.

---

## Shared lifecycle implementation

`tools/worktrees/worktree_lifecycle.py` is the shared implementation used by
the Python lifecycle tools. It owns:

- Ready and in-progress marker names and atomic marker claims
- Sentinel fields and rendering
- Sparse-index configuration for quiet `--no-checkout` stubs
- Stub creation and quietness checks

`worktree_shrink.py` retains the shrink pipeline, quarantine policy, manifest,
resume flow, and user-facing output. `backfill_recovery_stubs.py` retains its
selection and reporting policy. Both call the shared module at the lifecycle
seam instead of carrying separate implementations.

`worktree_inspector.ps1` remains a standalone read-only adapter. It mirrors the
marker and sentinel literals because invoking Python during inspection would
add a runtime dependency and weaken the read-only boundary.

---

## Stub status quietness

`git worktree add --no-checkout` creates a worktree whose index is populated from HEAD but whose working tree is empty. Without further action, `git status` inside the stub reports every tracked file as a staged deletion.

Both the shrink toolkit and `backfill_recovery_stubs.py` silence this by configuring sparse-checkout:

```
git -C <stub> read-tree HEAD
git -C <stub> config core.sparseCheckout true
git -C <stub> config core.sparseCheckoutCone false
mkdir -p <gitdir>/info
printf '' > <gitdir>/info/sparse-checkout
git -C <stub> ls-files -z | git -C <stub> update-index --skip-worktree -z --stdin
```

Where `<gitdir>` is the output of `git -C <stub> rev-parse --git-dir` (resolves to `<repo>/.git/worktrees/<slug>`).

Three implementation notes:

- Newly-created `--no-checkout` stubs have no index file. `update-index --skip-worktree` against an empty index is a no-op — `read-tree HEAD` must run first.
- Use `-z` mode (NUL-separated) for the pipe. On Windows, `subprocess.run` with `text=True` converts `\n` to `\r\n` in the stdin pipe, corrupting filenames. Bash pipes preserve binary mode, but tooling should not depend on the shell layer.
- Do not call `git sparse-checkout init` — it runs `read-tree -u` internally and materialises root files before any cone-disable can take effect.

Effect: `git status` inside the stub reports clean; the git worktree registration is unchanged; the branch is unchanged.

**Rematerialisation** when a session resumes:

```
git -C <stub> ls-files -z | git -C <stub> update-index --no-skip-worktree -z --stdin
git -C <stub> config core.sparseCheckout false
git -C <stub> checkout HEAD -- .
```

---

## Recovery from a failed shrink

| Failure point | State on disk | Recovery |
|---|---|---|
| Lock or verification failed before move | Original folder intact; in-progress marker present | Rename `.shrink-in-progress.*` back to `.shrink-when-safe`, or delete it to abandon the shrink |
| Folder moved, registration not yet pruned | Folder in quarantine; `git worktree list` shows original path as stale | Run `git worktree prune`, verify quarantine intact, continue from step 7 |
| Folder moved, pruned, stub not yet created | Folder in quarantine; nothing at original path | Run `git worktree add --no-checkout <original> <branch>`, continue from step 8 |
| Stub created, post-validation failed | Stub at original path may be inconsistent | If quarantine intact: remove the bad stub, recreate. Do not delete quarantine until validated |

Re-run `worktree_shrink.py --resume <manifest>` to pick up at the last completed phase. The manifest path is printed on failure.

---

## FS-lock policy

The shrink tool does not identify lock holders. It refuses cleanly when a lock is detected, reporting that the worktree is locked and suggesting what might be holding it (Desktop window, terminal, IDE, cloud sync shell extension).

The Desktop tasklist check is a UX gate, not a safety primitive. Its purpose is to surface the common case (session window still open) with a clear message. The no-op rename test is the actual safety check — it catches every kind of lock-holder regardless of source.

---

## Multi-session tasks

A task can span multiple sessions. The branch accumulates commits across sessions; only the final session merges and drops the marker.

- Session 1: commit work, wrap as WIP (skip merge, skip marker), exit. Worktree stays materialised.
- Session 2: resume in the same worktree. More commits. Wrap as WIP again.
- Session N (final): work complete, wrap as done (merge to master, drop marker). A future shrink run empties the folder.
