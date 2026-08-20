# Session recovery

Your Claude Desktop is broken in one of these ways. Run `python tools/diagnose.py` first — it identifies your exact problem and prints the next command.

The tools resolve the data directories automatically. On Windows, Desktop metadata
is under `%APPDATA%\Claude\claude-code-sessions\`; on macOS it is under
`~/Library/Application Support/Claude/claude-code-sessions/`. Transcripts are under
`~/.claude/projects/` on both platforms.

| Problem | Has automatic repair? |
|---|---|
| Session missing conversation history | Yes |
| Conversation history missing from disk | No (needs investigation) |
| Two sessions, same conversation history | Yes |
| Sessions started from outside any project folder | No (cosmetic only) |
| One project, two sets of sessions | Yes |
| Sessions missing from Desktop session list | Yes |

---

<a id="blank-pane-missing-cli"></a>
## Session missing conversation history

**What you see.** You open Claude Desktop. Your session list looks normal — titles, dates, models all there. You click one of the older sessions. The chat interface opens, but where the messages should be it just says **"No messages yet"**, like a fresh session you've never typed in. No error, no warning. The session opens. The conversation history doesn't.

**Why it happens (plain).** Every Claude Desktop session is stored as two pieces. One holds the title, model, and date — that's what shows in your session list. The other holds the conversation history. A small link connects them. For these sessions, that link is missing. Desktop can still show the session in the list because the first piece is intact, but has no way to find the conversation history to display. The chat opens with "No messages yet": the placeholder Desktop shows when there's nothing to render.

**Why it happens (technical).** Session state lives in two files: the platform-specific Desktop metadata file under `claude-code-sessions/<account>/<org>/local_<uuid>.json` (title, model, date, MCP config) and the transcript file under `~/.claude/projects/<project-slug>/<uuid>.jsonl` (the messages). The metadata's `cliSessionId` field carries the transcript filename's UUID stem. When it is missing or null, Desktop renders the entry in your session list from the metadata but has no handle to load the transcript with — so the chat opens with the "No messages yet" placeholder. See [architecture.md#the-two-layer-session-model](architecture.md#the-two-layer-session-model) for the full model.

**What `diagnose.py` reports:**

```
PROBLEM FOUND: Sessions appear in the session list but open with no conversation history
  Details: docs/session-recovery.md#blank-pane-missing-cli
  Safety: Quit Claude Desktop fully before any mutation. Diagnose is read-only and safe to run anytime.

  To repair -- dry-run first, review output, then add --apply:
    python tools/sessions/repair_session_metadata.py --diagnosis-id <id>
    python tools/sessions/repair_session_metadata.py --diagnosis-id <id> --apply
```

**Before running the mutator:** Quit Claude Desktop fully. On Windows verify with `tasklist /FI "IMAGENAME eq claude.exe"`; on macOS verify with `pgrep -x Claude`. Desktop holds metadata in memory and will overwrite repairs if it is still running. See [architecture.md#in-memory-cache-behaviour](architecture.md#in-memory-cache-behaviour).

**Recovery time:** 2–5 minutes.

---

<a id="cli-points-missing-jsonl"></a>
## Conversation history missing from disk ("Session not found on disk")

**What you see.** You open Claude Desktop. Your session list looks normal. You click an affected session. Instead of a conversation, the main pane shows three lines: **"Session not found on disk"** at the top, then **"Send a message to start fresh in this directory"** as an invitation. Two buttons sit underneath: **Archive** and **Delete**.

**Why it happens (plain).** Every Claude Desktop session is stored as two pieces: one with the title, date, and model, and one with the conversation itself. The link between them is intact for this session — but the file that holds the conversation is missing from disk. Desktop follows the link, finds nothing on the other end, and tells you so directly. There's no automatic repair that runs on your machine. Whether the conversation can be recovered depends on what backups exist — it's worth investigating before concluding it's gone.

**Why it happens (technical).** The metadata's `cliSessionId` is present and resolves to a valid-looking transcript path: `~\.claude\projects\<project-slug>\<uuid>.jsonl`. But no file exists at that path. Desktop surfaces this with the "Session not found on disk" error and the option to Archive or Delete the metadata entry. There's no automatic on-disk repair. Recovery depends on finding the `.jsonl` file in a backup, shadow copy, or version history — see [docs/recovering-deleted-jsonls.md](recovering-deleted-jsonls.md) for the search checklist. See [architecture.md#missing-jsonls-separate-failure-mode](architecture.md#missing-jsonls-separate-failure-mode) for the full discussion.

**What `diagnose.py` reports:**

```
PROBLEM FOUND: Session is in the session list but its conversation history is missing from disk
  Details: docs/session-recovery.md#cli-points-missing-jsonl
  Status : Needs investigation. The JSONL may exist in a backup, shadow copy, or cloud version history. Diagnose is read-only.

  Next:  python tools/sessions/find_missing_jsonls_in_backup.py [--backup PATH]

PROBLEM FOUND: Session is in the session list but its conversation history is missing from disk
  Details: docs/recovering-deleted-jsonls.md#windows-vss-shadow-copies
  Safety : Quit Claude Desktop fully before restoring. Diagnose is read-only.

  To repair -- dry-run first, review output, then add --apply:
    python tools/sessions/restore_from_vss.py --diagnosis-id <id>
    python tools/sessions/restore_from_vss.py --diagnosis-id <id> --apply
```

`diagnose.py` will also print how many sessions are in this state. On Windows, it prints both problems — the backup-search path and the automated VSS restore path.

**Before restoring from backup — mtime interaction with cleanup.**

Claude Code's cleanup deletes JSONLs based on filesystem mtime, not the timestamp of the last message. Backup zips preserve original mtimes. If you extract a backup while `cleanupPeriodDays` is at its default (30 days), any JSONL with an mtime older than 30 days will be re-deleted on the next Desktop launch — the restore appears to work, then the files vanish in the next session.

Before extracting any backup:
1. Open `~/.claude/settings.json` and set `"cleanupPeriodDays": 36500`
2. Extract the backup zip
3. Run `synth_session_metadata.py` (see [Sessions missing from Desktop session list](#orphan-jsonl-no-metadata)) to make the restored transcripts visible
4. Revert `cleanupPeriodDays` to your preferred value once sessions are recovered

This applies to any restore method that preserves original file timestamps — the backup zip from `backup_claude_state.py`, manual extraction, or platform snapshot tools (Windows VSS, macOS Time Machine).

**Recovery options:**

- **Windows VSS (automated):** `restore_from_vss.py` searches Windows shadow copies for the missing transcript and restores it in one command. `diagnose.py` routes you here automatically. See [recovering-deleted-jsonls.md#windows-vss-shadow-copies](recovering-deleted-jsonls.md#windows-vss-shadow-copies).
- **User backup:** `find_missing_jsonls_in_backup.py` searches a backup directory you specify for the missing transcript by session ID. Point it at any external backup you maintain.
- If neither option finds the file, work through the full checklist at [recovering-deleted-jsonls.md](recovering-deleted-jsonls.md). The conversation content may be unrecoverable, but the metadata (title, model, date) remains intact.

**Recovery time:** 2–5 minutes if VSS or backup has the file. Immediate if neither does.

---

<a id="duplicate-synth-metadata"></a>
## Two sessions, same conversation history

**What you see.** You open Claude Desktop. Your session list has two entries that look like distinct sessions — different titles, side by side. You click one. The conversation history opens. You click the other. The same conversation history opens, identical message for message. Two listings, one conversation.

**Why it happens (plain).** Each session is stored as two pieces: one with the title, date, and model, and one with the conversation itself. Normally each session has its own pair. But for these two entries, both metadata pieces point at the same conversation file. Desktop shows two rows in your session list (because there are two metadata files) but loads the same messages when you click either (because both metadata files point at the same conversation underneath). The titles differ because they were generated at different times — usually one was the original, the other was created later by a recovery tool that didn't notice the original already existed.

**Why it happens (technical).** Two metadata files (each `local_<uuid>.json`) carry the same `cliSessionId` value. Desktop renders one row per metadata file, but both resolve to the same `~\.claude\projects\<project-slug>\<uuid>.jsonl` transcript when clicked. This usually means an earlier metadata-synthesis run created a new metadata file for a transcript that already had one — most often as a side effect of multi-step recovery where some transcripts were genuinely orphaned and others weren't. No data is lost; the session list just shows the same conversation twice.

**What `diagnose.py` reports:**

```
PROBLEM FOUND: Two sessions in the session list open to the same conversation history
  Details: docs/session-recovery.md#duplicate-synth-metadata
  Safety: Quit Claude Desktop fully before any mutation. Diagnose is read-only and safe to run anytime.

  To repair -- dry-run first, review output, then add --apply:
    python tools/sessions/cleanup_synth_duplicates.py --diagnosis-id <id>
    python tools/sessions/cleanup_synth_duplicates.py --diagnosis-id <id> --apply
```

**Before running the mutator:** Quit Desktop fully. The dry-run output identifies which metadata file will be removed. Review it before applying — the tool removes the newer duplicate, not the original.

**Recovery time:** 2–5 minutes.

---

<a id="old-root-cwd-reference"></a>
## Sessions started from outside any project folder

**What you see.** Probably nothing obvious. These sessions show up in your session list normally and open fine when clicked. `diagnose.py` reports this state about the underlying metadata; you wouldn't notice it on your own.

**Why it happens (plain).** A few sessions have their starting location recorded as a generic path (`C:\` or your home folder) instead of a specific project folder. This usually means the session was launched from a terminal that hadn't yet `cd`'d into a project. The sessions still work; their location is just less informative than it could be.

**Why it happens (technical).** The metadata's `cwd` field is a bare-root path (`C:\`, `C:\Users\<name>`). `diagnose.py` flags this via the `cwd_prefix_types.bare_root` snapshot field. No repair is offered: the `cwd` was correctly recorded, just at an unhelpful level of granularity. Most often seen on sessions from earlier Claude Code versions.

**What `diagnose.py` reports:**

```
PROBLEM FOUND: Sessions started from a bare drive root rather than a project folder
  Details: docs/session-recovery.md#old-root-cwd-reference
  Safety: These sessions are typically read-only recoverable. Diagnose is read-only and safe to run anytime.

  No automatic repair for this state. See:
    docs/session-recovery.md#old-root-cwd-reference
```

**Recovery time:** No repair needed.

---

<a id="junction-realpath-slug-mismatch"></a>
## One project, two sets of sessions

**What you see.** Sessions you remember running against one project appear split or duplicated in your session list. Some show one starting path; others show a different starting path — but both paths point at the same physical folder on disk. Click into any of them and they work fine; they just don't sit alongside each other the way you'd expect.

**Why it happens (plain).** Windows lets one folder path point at another, using a feature called a junction. So `C:\Projects\OldName` can be set up to resolve to `C:\Projects\NewName`: same physical files, two valid paths. Claude Desktop remembers the literal path you used to launch each session, not the resolved real path. Sessions started via the junction look like a different project from sessions started via the real path, even though they share the same files. The session list ends up with two entries for what's really one project.

**Why it happens (technical).** Desktop derives the project slug from the literal `cwd` string at session start and does not resolve Windows junctions to the canonical path first. A junction at `C:\Projects\OldName` pointing to `C:\Projects\NewName` produces slug `C--Projects-OldName` for sessions launched via the junction and `C--Projects-NewName` for sessions launched via the real path. Both slug directories appear under `~\.claude\projects\`, each holding its own subset of transcripts. The repair (`repoint_session_to_jsonl.py`) updates the affected metadata to use the canonical path.

**Runtime trap.** Removing the junction while a session is open with the junction path in its `cwd` breaks that session's runtime path binding immediately — every read and write goes through that path, and once the junction is gone, they all fail. Quit Desktop fully before removing any junction that current sessions reference. See [architecture.md#junction-vs-realpath-failure-mode](architecture.md#junction-vs-realpath-failure-mode) for the full mechanism.

**How this hit us.** Mode 5 can be triggered any time a junction lets sessions start from both the link and the target. In our case, the `ctx/` junction was created as a workaround for a Claude Code bug (`@import` silently drops paths with spaces, so a no-space alias was needed). The other common trigger is a folder rename with a junction left at the old path for backward compatibility.

**What `diagnose.py` reports:**

```
PROBLEM FOUND: One project shows two sets of sessions in the session list
  Details: docs/session-recovery.md#junction-realpath-slug-mismatch
  Safety: Quit Claude Desktop fully before any mutation. Do not remove the junction while any session using it is active.

  To repair -- dry-run first, review output, then add --apply:
    python tools/sessions/repoint_session_to_jsonl.py --diagnosis-id <id>
    python tools/sessions/repoint_session_to_jsonl.py --diagnosis-id <id> --apply
```

**Before running the mutator:** Quit Desktop fully first. See the Runtime trap above before removing any junction that current sessions reference.

**Recovery time:** 5–10 minutes.

---

<a id="truncated-jsonl"></a>

## Session opens but its conversation history is shorter than expected

**What you see.** A session appears in the session list and opens normally, but the conversation stops earlier than you remember. The transcript file exists on disk, yet the visible conversation has fewer assistant turns than the metadata says were completed.

**Why it happens (plain).** Desktop's metadata records how many turns the session completed. The JSONL transcript is the actual conversation. If the transcript has fewer assistant records than `completedTurns`, the file may have been truncated or rolled back by a sync, backup, or filesystem failure. The metadata link can still be perfectly valid, so repairing metadata will not recreate the missing records.

**What `diagnose.py` reports:**

```
Truncated    : 1 session(s) have fewer messages than completedTurns records (history appears cut off)
```

`diagnose.py` compares the assistant-record count in each local JSONL with the metadata's `completedTurns` value. It is read-only and does not modify either file.

**Recovery.** No automatic repair is available. Check cloud-sync version history, scheduled backups, VSS snapshots on Windows, or another copy of the project transcripts for a longer version of the same JSONL. Compare copies by session UUID and size before restoring anything. If a longer transcript is recovered, run `diagnose.py` again and follow the exact command it prints for the remaining state.

**Safety.** Do not overwrite the surviving transcript until a backup copy exists. Quit Claude Desktop fully before restoring a JSONL, because the app may hold session state in memory.

**Recovery time:** Depends on whether a longer backup or version-history copy exists.

---

<a id="orphan-jsonl-no-metadata"></a>
## Sessions missing from Desktop session list

**What you see.** You remember a specific conversation. You know it happened. The file for it is on disk (you can find it under `~\.claude\projects\<project>\` with a `.jsonl` extension if you look). But Desktop's session list doesn't show that session at all — not in any group, not under any header. It's absent.

**Why it happens (plain).** Each Claude Desktop session is stored as two pieces — the part with the title, date, and model, and the part with the conversation itself. For these orphan sessions, the first piece is gone but the conversation piece is still on disk. Desktop only displays sessions it has both pieces for, so anything missing the title/date piece is invisible.

Two ways sessions end up orphan: you used Desktop's "Delete" UI on them (Delete removes the title/date piece but leaves the conversation on disk — useful if you ever want to recover it), or some earlier recovery attempt orphaned the original conversation by sending a message that created a new transcript and overwrote the link.

**Why it happens (technical).** The transcript exists at `~\.claude\projects\<project-slug>\<uuid>.jsonl` but no metadata file references it via `cliSessionId`. Desktop only renders sessions for which it has metadata, so the transcript is invisible.

Orphan transcripts arise from two documented mechanisms: Desktop's "Delete" UI removes the metadata file but does not touch the transcript file; and recoveries can orphan a transcript if the user sent a message in a mode-1 state — Desktop generates a new transcript and overwrites the metadata's `cliSessionId` to point at the new one, stranding the original. The repair (`synth_session_metadata.py`) reads the orphan transcript directly and synthesises a fresh metadata file: title from the first user message, timestamps from the first and last records, `cliSessionId` set to the transcript's UUID stem.

**How this hit us.** A session of ours went into mode 1 (link broken). We sent a message before noticing — Desktop created a new transcript for the message and overwrote the link to point at it. The original conversation ended up orphan: still on disk, just not in the session list. Recovering required two steps: repointing the metadata back to the original transcript, then synthesising new metadata for the new transcript so the few new messages also showed up as their own session in the list.

**What `diagnose.py` reports:**

```
PROBLEM FOUND: Sessions are absent from the session list even though their transcript files exist on disk
  Details: docs/session-recovery.md#orphan-jsonl-no-metadata
  Safety: Quit Claude Desktop fully before any mutation. Diagnose is read-only and safe to run anytime.

  To repair -- dry-run first, review output, then add --apply:
    python tools/sessions/synth_session_metadata.py --diagnosis-id <id>
    python tools/sessions/synth_session_metadata.py --diagnosis-id <id> --apply
```

**Caution:** Synthesising metadata for all orphan transcripts will restore sessions you may have deliberately deleted (Desktop's "Delete" UI leaves transcripts on disk). Review the dry-run output and confirm before applying.

**Recovery time:** 5–15 minutes.

<a id="null-timestamp-metadata"></a>

## Entire session list empty after restart

**What you see.** Every time you restart Claude Desktop, the session list is completely empty — not one session, from any project. The transcripts are all on disk, the app's own log (`main.log`) says it loaded them (`Loaded N persisted sessions from ...claude-code-sessions...`), and clearing Desktop's caches or web storage changes nothing. It looks like total data loss. It isn't.

**Why it happens (plain).** One of the small per-session files that Desktop uses to build the list is missing its dates. When Desktop tries to sort the list by date, that one dateless entry breaks the whole operation, and the list silently comes up empty. Nothing is lost — the list just refuses to render while that one bad file is present.

**Why it happens (technical).** A metadata file in `claude-code-sessions/<account>/<org>/` has `"createdAt": null` (and typically `"updatedAt"`/`"lastActivityAt"` null too). The main process loads all metadata without complaint, but the renderer's sort/format step throws on the null and the entire list component renders empty. No error is logged in `main.log` or the renderer log, which is what makes this so hard to find — every subsystem reports healthy. These entries don't come from Desktop itself; they come from hand-rolled recovery/import attempts that synthesise metadata files without filling in the timestamp fields (`synth_session_metadata.py` in this repo fills them from the transcript, so metadata it produces is not affected).

**How this was confirmed.** macOS case, Desktop 1.20186.0, 2026-07-10 (reported in [anthropics/claude-code#59736](https://github.com/anthropics/claude-code/issues/59736)): 34 metadata files loaded on every restart, sidebar always empty, full web-storage reset had no effect. Exactly one metadata file had null timestamps (plus one duplicate-cli file). Moving those two files out of the directory and restarting immediately restored all remaining sessions to the list.

**What `diagnose.py` reports:**

```
Null stamps  : 1 metadata file(s) have null createdAt/updatedAt (can blank the entire session list)

PROBLEM FOUND: The entire session list is empty after every Desktop restart, even though transcripts are intact on disk and the app log reports sessions being loaded
  Details: docs/session-recovery.md#null-timestamp-metadata
```

**Repair (manual).** Quit Claude Desktop fully and verify no processes remain. Move the flagged `local_*.json` files out of `claude-code-sessions/<account>/<org>/` into a backup folder — quarantine, don't delete. Restart Desktop; the list should populate. If you want the quarantined session back in the list afterwards, fill its `createdAt`/`updatedAt` from the first and last `timestamp` records of its transcript JSONL and move the file back.

**Recovery time:** 5 minutes.

---

<a id="account-uuid-rotation"></a>

## Existing sessions remain under an older account/organisation pair

**What you see.** After logging out and back in, Claude Desktop opens with an empty session history even though the older `local_*.json` files are still present under the Claude Desktop data directory.

**Why it happens (plain).** Desktop stores session metadata under an account UUID and an organisation UUID. Logout/login can rotate that pair. Desktop then reads the new pair, while the populated metadata remains under the older pair.

**What `diagnose.py` reports.** When more than one pair exists, the diagnostic lists every pair using a deterministic opaque label and its `local_*.json` count. Labels are ordered by hidden pair identity but reveal neither account nor organisation UUID. A populated pair alongside an empty pair, or multiple populated pairs, produces a read-only `account-uuid-rotation` finding. The toolkit never infers which pair is authoritative.

```
Desktop pairs : 2
  pair-01 local_*.json=12
  pair-02 local_*.json=0
```

**Recovery.** The toolkit does not infer an authoritative live pair or move
metadata between pairs. Confirm which pair Desktop currently reads and preserve
every populated pair before deciding on any manual recovery. If the missing
metadata is in a `backup_claude_state.py` Desktop archive,
`restore_claude_metadata_backup.py` can verify the archive and restore each
file to the exact account/organisation path recorded by its manifest. It does
not merge or reinterpret pairs. Legacy flat archives require explicit account
and organisation UUID arguments and are inspectable only; `--apply` requires a
v2 manifest-backed archive. Always review the default dry-run first and fully
quit Desktop before `--apply`. A usable apply command is withheld unless live
metadata and transcript inventories are complete and the schema is recognised.
Apply repeats that fail-closed gate, then pins every destination directory,
holds and hashes the exact archive source, and repeats its target, Desktop,
archive, and normalized live-state guards around every atomic create. Only its
own verified staging files, created links, and empty setup directories are
discounted; unrelated transcript, metadata, or account-state drift aborts and
rolls back safely created links. Windows rollback retains no-share-delete file
and directory handles and uses object-bound deletion, so a replacement inserted
at the same name is never pathname-deleted. POSIX and other platforms retain
created targets/directories and report `rollback incomplete` when publication
fails because no portable inode-bound conditional unlink/rmdir is available;
this is an intentional safety-over-cleanup tradeoff. Strict JSON validation
rejects duplicate keys, non-finite values, oversized integers, and excessive
nesting. The
metadata-synthesis mutator refuses to
choose a destination while more than one pair exists. `diagnose.py` still
reports any orphan-transcript finding in that state, but suppresses its
synthesis command until the destination ambiguity is resolved.

**Safety.** `diagnose.py` only enumerates directories and reads metadata. It does not move, copy, rename, delete, or rewrite user data.
