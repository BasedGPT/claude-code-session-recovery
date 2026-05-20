# Session recovery

Your Desktop is broken in one of these ways:

| Problem | Has automatic repair? |
|---|---|
| History shows old sessions, but clicking them shows a blank pane | Yes |
| Session shows in history but opens to no conversation content | No (may be unrecoverable) |
| Two entries in history point to the same conversation | Yes |
| Sessions listed under a bare drive root rather than a project folder | No (cosmetic only) |
| Sessions from a Windows junction path show separate history | Yes |
| Desktop does not show some sessions in history at all | Yes |

Run `python tools/diagnose.py` first. It identifies your exact problem and prints the next command.

---

## blank-pane {#blank-pane}

**Symptom:** Old sessions appear in the Desktop history picker. Clicking one shows a blank pane or an indefinitely spinning loader.

**What's broken:** The metadata file for these sessions is missing the `cliSessionId` field that links it to the conversation transcript. Desktop renders the picker entry from the metadata (title, model, date), but cannot render the conversation pane — there is nothing pointing it to the right JSONL file.

**What `diagnose.py` reports:**

```
PROBLEM FOUND: Claude Desktop history shows old sessions, but clicking them shows a blank pane
  Details: docs/session-recovery.md#blank-pane
  Safety: Quit Claude Desktop fully before any mutation. Diagnose is read-only and safe to run anytime.

  To repair -- dry-run first, review output, then add --apply:
    python tools/sessions/repair_session_metadata.py --diagnosis-id <id>
    python tools/sessions/repair_session_metadata.py --diagnosis-id <id> --apply
```

**Before running the mutator:** Quit Claude Desktop fully — window and tray. Verify with `tasklist /FI "IMAGENAME eq claude.exe"`. Desktop holds metadata in memory and will overwrite repairs if it is still running. See [architecture.md#in-memory-cache-behaviour](architecture.md#in-memory-cache-behaviour).

**Recovery time:** 2–5 minutes.

---

## missing-jsonl {#missing-jsonl}

**Symptom:** A session appears in the history picker and the metadata looks intact — title, model, date all show. Clicking it opens to no conversation content, or shows a "conversation not found" error.

**What's broken:** The `cliSessionId` field exists and points at a JSONL file ID, but that JSONL is absent from disk. The conversation transcript is gone.

**What `diagnose.py` reports:**

```
PROBLEM FOUND: Claude Desktop shows a session in history but opening it shows no conversation content
  Details: docs/session-recovery.md#missing-jsonl
  Safety: This may be unrecoverable if no backup exists. Diagnose is read-only.

  No automatic repair for this state. See:
    docs/session-recovery.md#missing-jsonl
```

`diagnose.py` will also print how many sessions are in this state.

**Recovery options:**

- `find_missing_jsonls_in_backup.py` searches a backup directory you specify for the missing JSONL by session ID. Point it at any external backup you maintain.
- If no backup exists, the session content is gone. The metadata (title, model, date) is intact, but there is nothing to render.

**Recovery time:** 5–15 minutes if a backup exists. Immediate if no backup.

---

## duplicate-synth-metadata {#duplicate-synth-metadata}

**Symptom:** Two entries in the Desktop history picker open the same conversation. The entries may have slightly different titles or dates.

**What's broken:** Two separate metadata files both carry the same `cliSessionId` value, pointing at the same JSONL. This typically results from a previous metadata-synthesis operation that created a new metadata file for a session that already had one.

**What `diagnose.py` reports:**

```
PROBLEM FOUND: Two entries in Claude Desktop history point to the same conversation
  Details: docs/session-recovery.md#duplicate-synth-metadata
  Safety: Quit Claude Desktop fully before any mutation. Diagnose is read-only and safe to run anytime.

  To repair -- dry-run first, review output, then add --apply:
    python tools/sessions/cleanup_synth_duplicates.py --diagnosis-id <id>
    python tools/sessions/cleanup_synth_duplicates.py --diagnosis-id <id> --apply
```

**Before running the mutator:** Quit Desktop fully. The dry-run output identifies which metadata file will be removed. Review it before applying — the tool removes the newer duplicate, not the original.

**Recovery time:** 2–5 minutes.

---

## old-root-cwd-reference {#old-root-cwd-reference}

**Symptom:** Sessions appear in history but are grouped under a bare drive root (for example, `C:\` or `C:\Users\You`) rather than under a specific project folder. These are typically older sessions.

**What's broken:** The session metadata has a `cwd` path pointing at a bare drive root or a very shallow path. This usually means the session was launched from outside any specific project context — from a terminal opened at the root before a project directory was set up.

**What `diagnose.py` reports:**

```
PROBLEM FOUND: Sessions show history but are listed under a bare drive root rather than a project folder
  Details: docs/session-recovery.md#old-root-cwd-reference
  Safety: These sessions are typically read-only recoverable. Diagnose is read-only and safe to run anytime.

  No automatic repair for this state. See:
    docs/session-recovery.md#old-root-cwd-reference
```

There is no mutator for this state. The sessions are accessible — clicking them opens the conversation correctly. The grouping under the root path is cosmetic, not a data-loss issue.

**Recovery time:** No repair needed.

---

## junction-realpath {#junction-realpath}

**Symptom:** Sessions started from a project folder show separate history from sessions at the same project opened a different way. You have two histories where you expect one.

**What's broken:** Claude Code derives the project slug from the literal `cwd` string at session start and does not resolve Windows junctions first. If some sessions were launched via a junction alias and others via the real path, they get different slugs and appear as separate projects in Desktop. See [architecture.md#junction-vs-realpath-failure-mode](architecture.md#junction-vs-realpath-failure-mode).

**What `diagnose.py` reports:**

```
PROBLEM FOUND: Sessions started from a Windows junction path show separate history from sessions at the real path
  Details: docs/session-recovery.md#junction-realpath
  Safety: Quit Claude Desktop fully before any mutation. Do not remove the junction while any session using it is active.

  To repair -- dry-run first, review output, then add --apply:
    python tools/sessions/repoint_session_to_jsonl.py --diagnosis-id <id>
    python tools/sessions/repoint_session_to_jsonl.py --diagnosis-id <id> --apply
```

**Before running the mutator:** Do not remove the junction before running the mutator. Removing a junction while Desktop has a session with the junction path in its cwd breaks that session's runtime path binding, even if the JSONL is stored at the canonical slug. Quit Desktop fully first.

**Recovery time:** 5–10 minutes.

---

## orphan-jsonl {#orphan-jsonl}

**Symptom:** You know a conversation happened — the JSONL file exists on disk — but Desktop does not show it in the history picker at all.

**What's broken:** The conversation transcript exists but has no corresponding metadata file. Desktop shows only sessions it has metadata for. Sessions become orphaned when the metadata file is deleted (Desktop's "delete" UI removes the metadata but leaves the JSONL), or when a session was started in a context where metadata was never written.

**What `diagnose.py` reports:**

```
PROBLEM FOUND: Claude Desktop does not show some sessions in history at all, even though conversation transcripts exist on disk
  Details: docs/session-recovery.md#orphan-jsonl
  Safety: Quit Claude Desktop fully before any mutation. Diagnose is read-only and safe to run anytime.

  To repair -- dry-run first, review output, then add --apply:
    python tools/sessions/synth_session_metadata.py --diagnosis-id <id>
    python tools/sessions/synth_session_metadata.py --diagnosis-id <id> --apply
```

**Caution:** Desktop's "delete" UI removes metadata but leaves the JSONL on disk. Synthesising metadata for all orphaned JSONLs will restore sessions you may have deliberately deleted. Review the dry-run output and confirm before applying.

**Recovery time:** 5–15 minutes.
