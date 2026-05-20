# Troubleshooting

Source of truth: [`troubleshooting.json`](troubleshooting.json). This file is hand-mirrored from it; CI verifies they match.

Run `python tools/diagnose.py` first — it reads this table, probes your state, and prints the exact command to run.

---

## Session problems

### blank-pane-missing-cli

**Symptom:** Claude Desktop history shows old sessions, but clicking them shows a blank pane.

**Root cause:** `cliSessionId` field missing from session metadata — Desktop can list the session but cannot find the transcript to render.

**Fix:** `python tools/diagnose.py` will identify the affected files and print the repair command.

**Safety:** Quit Claude Desktop fully before any mutation. `diagnose.py` is read-only and safe to run anytime.

**Details:** [docs/session-recovery.md#blank-pane](docs/session-recovery.md#blank-pane)

---

### cli-points-missing-jsonl

**Symptom:** Claude Desktop shows a session in history but opening it shows no conversation content.

**Root cause:** Metadata file has a valid `cliSessionId` but the JSONL transcript it points at no longer exists on disk.

**Fix:** `python tools/diagnose.py` will check whether a backup copy of the transcript exists. If no backup is found, the session is likely unrecoverable.

**Safety:** This may be unrecoverable if no backup exists. `diagnose.py` is read-only.

**Details:** [docs/session-recovery.md#missing-jsonl](docs/session-recovery.md#missing-jsonl)

---

### duplicate-synth-metadata

**Symptom:** Two entries in Claude Desktop history point to the same conversation.

**Root cause:** An earlier recovery attempt created a synthetic metadata file for a session that already had a metadata file — leaving two Desktop history entries backed by the same JSONL transcript.

**Fix:** `python tools/diagnose.py` will identify the duplicate metadata files and print the cleanup command.

**Safety:** Quit Claude Desktop fully before any mutation. `diagnose.py` is read-only and safe to run anytime.

**Details:** [docs/session-recovery.md#duplicate-synth-metadata](docs/session-recovery.md#duplicate-synth-metadata)

---

### old-root-cwd-reference

**Symptom:** Sessions show history but are listed under a bare drive root rather than a project folder.

**Root cause:** The session was started from a project root path before a worktree was set up. Claude Desktop recorded the bare root (`C:\Users\name`) instead of a project-specific path. These sessions are accessible but will not benefit from worktree-aware routing.

**Fix:** `python tools/diagnose.py` will identify these sessions. No automatic repair is available; this state is informational.

**Safety:** Diagnose is read-only and safe to run anytime.

**Details:** [docs/session-recovery.md#old-root-cwd-reference](docs/session-recovery.md#old-root-cwd-reference)

---

### junction-realpath-slug-mismatch

**Symptom:** Sessions started from a Windows junction path show separate history from sessions at the real path, or some sessions appear missing.

**Root cause:** Claude Desktop derives the project slug from the literal path string at session start. Sessions via a junction (`C:\Old\Path`) get a different slug than sessions via the real path (`C:\New\Path`), even if they resolve to the same folder.

**Fix:** `python tools/diagnose.py` will identify which metadata files use the junction path and whether repointing them is safe.

**Safety:** Quit Claude Desktop fully before any mutation. Do not remove the junction while any session using it is active.

**Details:** [docs/session-recovery.md#junction-realpath](docs/session-recovery.md#junction-realpath)

---

### orphan-jsonl-no-metadata

**Symptom:** Conversation transcripts exist on disk but some sessions never appear in Claude Desktop history at all.

**Root cause:** The JSONL transcript file exists under `~/.claude/projects/<slug>/` but no metadata file in AppData references it via `cliSessionId`. Without a metadata file, Desktop has no entry to display in its history panel.

**Fix:** `python tools/diagnose.py` will count the orphaned transcripts and print the synthesis command. Review the synthesised metadata in `./synth-out/` before applying.

**Safety:** Quit Claude Desktop fully before any mutation. `diagnose.py` is read-only and safe to run anytime.

**Details:** [docs/session-recovery.md#orphan-jsonl](docs/session-recovery.md#orphan-jsonl)

---

*More rows are added as new failure modes are confirmed and fixture-tested. See [troubleshooting.json](troubleshooting.json) for the machine-readable version.*
