# Architecture

The two-layer session model, slug encoding, in-memory cache behaviour, junction-vs-realpath failure mode, migration and rename failure modes, and the debugging dead-ends that are worth ruling out.

---

## The two-layer session model

Claude Code sessions are stored in two independent locations that Desktop must reconcile:

```
Desktop metadata (one file per session):
  %APPDATA%\Claude\claude-code-sessions\<account-uuid>\<org-uuid>\local_<uuid>.json

CLI transcript (the actual conversation):
  %USERPROFILE%\.claude\projects\<slug>\<cli-session-id>.jsonl
```

The `cliSessionId` field in the metadata file is the link. When you click a session in Desktop's session list:

1. Desktop reads the metadata file — title, model, MCP config, etc.
2. Desktop reads `cliSessionId`, finds the matching JSONL under the slug directory, and renders the conversation.

If `cliSessionId` is missing or null, Desktop renders the session in the session list from the metadata but has no transcript handle to load — the chat opens with the "No messages yet" placeholder (verified visual). If `cliSessionId` points at a JSONL that no longer exists on disk, Desktop surfaces this with the explicit "Session not found on disk" error and offers Archive / Delete actions on the metadata.

---

## Slug encoding

Claude Code derives a project slug from the `cwd` path string at session start. Path separators, colons, and other punctuation become dashes:

```
C:\Users\You\Projects\MyApp  →  C--Users-You-Projects-MyApp
```

The encoding in `diagnose.py`:

```python
def _slug_encode(cwd):
    out = cwd
    for ch in (":", "\\", "/", ".", " "):
        out = out.replace(ch, "-")
    return out
```

The slug is derived once — from the literal string Desktop was given at session start — and never updated. Two common failure modes follow directly from this:

1. **Project rename.** Rename the project folder and existing sessions remain under the old slug. New sessions get a new slug. Desktop shows them as two separate, unrelated projects.
2. **Junction aliasing.** Sessions started via a Windows junction get the junction's slug; sessions started at the real path get the real path's slug. Same physical directory, two slug directories, two histories.

---

## In-memory cache behaviour

Desktop holds metadata files in memory and flushes back to disk periodically. **Modifying or deleting a metadata file while Desktop is running risks Desktop overwriting your changes on its next flush.**

This is the most common cause of "I ran the repair but it didn't stick."

Closing the Desktop window is not enough. Desktop continues running as a tray process and keeps flushing. You must quit fully:

1. Right-click the tray icon → Quit. The window and the tray icon must both disappear.
2. Verify the process is gone:
   ```
   tasklist /FI "IMAGENAME eq claude.exe"
   ```
   Expected: `INFO: No tasks are running which match the specified criteria.`
3. Only then run a mutator.

The only mutations safe to attempt while Desktop is running are to files it has never loaded into memory — typically very old sessions that have not been clicked recently. Even then, the risk is not zero. Treat "quit first" as mandatory.

---

## Junction-vs-realpath failure mode

Windows junctions let you alias a path: `C:\Projects\OldName` can point to `C:\Projects\NewName`. The slug failure occurs because Desktop derives the slug from the literal cwd string — it does not resolve junctions first.

If you have:

```
C:\Projects\OldName  (junction → C:\Projects\NewName)
C:\Projects\NewName  (the real folder)
```

Sessions launched via `OldName` produce slug `C--Projects-OldName`. Sessions launched via `NewName` produce slug `C--Projects-NewName`. Both exist under `~\.claude\projects\`. Desktop shows them as two separate projects despite sharing one physical directory.

### The runtime-binding trap

Desktop holds the literal cwd string from session launch in memory and uses it for every filesystem operation during that session. Removing the junction while a session is running with the junction path in its cwd breaks that session immediately — every read and write goes through the path, and once the junction is gone, they all fail.

This is true even if the JSONL is stored at the canonical slug. Storage location and runtime path binding are independent: the slug is derived at boot, but the live cwd reference persists for the session's lifetime.

### Before removing a junction

Quit any session whose metadata shows the junction path as `cwd`. Verify nothing is running from that path, then quit Desktop fully before removing the junction. Restoring the junction recovers a session that broke mid-removal — Desktop's in-memory state is not reloaded from disk, so path resolution works again as soon as the junction is back.

---

## Migration and rename failure modes

Two distinct breaks result from renaming a project folder:

1. **Metadata `cwd` is stale.** Metadata files from before the rename still have the old `cwd`. Sessions launched at the new path get the new slug; old sessions remain under the old slug. Both appear in Desktop as unrelated projects.
2. **JSONL is unreachable.** If `cliSessionId` points at a UUID whose JSONL no longer exists at any slug path — because the slug directory was never copied or synced to the new location — the transcript is effectively gone until recovered from a backup.

`rewrite_metadata_cwd.py` handles the first case: it updates the `cwd` field in affected metadata files to the new path. The second case requires `repoint_session_to_jsonl.py` if the JSONL exists under a different slug, or `find_missing_jsonls_in_backup.py` if a backup copy exists.

---

## Missing JSONLs (separate failure mode)

If a metadata file has a valid `cliSessionId` but the JSONL it points at is absent from disk, no repair to the metadata will help — there is nothing to link to.

This is a distinct failure mode from a missing `cliSessionId`. Desktop shows the session in the session list because the metadata file exists. Clicking it fails because the JSONL is gone — Desktop surfaces the "Session not found on disk" error rather than the "No messages yet" placeholder of the missing-link case.

Recovery depends entirely on whether a backup copy exists. `find_missing_jsonls_in_backup.py` searches a user-specified backup directory for the missing JSONL by session ID. If neither the live directory nor any backup contains the file, the conversation content is unrecoverable.

---

## Synthesising metadata for orphaned JSONLs

If JSONL transcripts exist on disk but no metadata file references them, Desktop will not show those sessions in the session list. `synth_session_metadata.py` creates metadata files for orphan JSONLs by reading the transcript directly.

Metadata field sources during synthesis:

- `title` — first user message in the JSONL, truncated to ~60 characters
- `createdAt` — timestamp of the first JSONL record
- `lastActivityAt` — timestamp of the last JSONL record
- `cliSessionId` — the JSONL filename (UUID stem, without `.jsonl`)
- `sessionId` — freshly generated UUID
- `model`, `enabledMcpTools`, `remoteMcpServersConfig` — copied from a recent working metadata file as a template

Two constraints observed in working metadata files:

- **`enabledMcpTools` cannot be `{}`** (an empty map). Working metadata always has 13–58 entries. Copy the map verbatim from a recent working file.
- **`remoteMcpServersConfig` must be present** — `null` and omission are not observed in working files; `[]` is safe.

Worktree sessions also include `branch`, `worktreePath`, `worktreeName`, and `sourceBranch`. Other fields (`completedTurns`, `chromePermissionMode`, `effort`, `alwaysAllowedReasons`, `titleSource`) are optional — synthesis works without them.

**Caution:** Desktop's "delete" UI removes the metadata file but leaves the JSONL on disk. Synthesising all orphaned JSONLs will restore sessions you may have deliberately deleted. Review the dry-run output before applying.

---

## Mutator gates

Every mutating script must pass five gates before it can be merged or run.

1. **Matching fixture.** A directory under `fixtures/<NN>-<name>/state/` reproduces the broken state the mutator targets, plus a `golden/` directory holding the expected `diagnose.py` output, dry-run output, and post-mutation snapshot. Enforced at contribution time — a mutator without a fixture will not be merged.
2. **Diagnosis-token enforcement.** The script accepts `--diagnosis-id <hex>` and refuses to run if the token does not match a fresh diagnosis of the current state. A stale token — state changed between diagnosis and apply — causes refusal. Enforced at runtime.
3. **Backup before mutation.** The script writes the original file to `.\repair-backup\<filename>` and verifies the write before touching the live file. A failed backup aborts the mutation. Enforced at runtime.
4. **Schema probe.** If the state layout is not in the recognised fixture set, the script enters audit-only mode and emits no mutation command. Enforced at runtime.
5. **Quit-Desktop precondition.** The script checks `claude.exe` in the process list and warns prominently if Desktop is running. Enforced at runtime.

Gates 2–5 implement the safety contract enumerated in [SECURITY.md](../SECURITY.md). Gate 1 is the contribution contract enforced by CI — see [CONTRIBUTING.md](../CONTRIBUTING.md).

---

## Why this happens

No definitive root cause is documented by Anthropic. Three hypotheses based on observed patterns:

1. **Old Desktop versions did not write `cliSessionId`.** Affected sessions tend to cluster in older date ranges. A version update may have changed when `cliSessionId` is set, leaving pre-upgrade sessions without it.
2. **Partial write during a Desktop crash.** If Desktop crashed between writing the metadata file and linking the JSONL, the metadata exists but `cliSessionId` was never filled in.
3. **Project rename or import path change.** The slug derivation happens at session start. A rename after the fact means Desktop's internal links no longer resolve.
4. **The link field is a single point of failure.** All the failure modes above share a structural root cause: \cliSessionId\ is the only record mapping a Desktop session entry to its JSONL transcript. No secondary index, no shadow store, no fallback. Any process that nulls or corrupts that field — a crash mid-write, a startup scanner heuristic, a Desktop update, a migration gap — severs the connection permanently until repaired. The toolkit reconstructs this field; a more resilient design would maintain the link redundantly: a separate index keyed on JSONL modification time and path, or a scanner constraint that only nulls entries where the JSONL is actually absent from disk.

### Dead end: MSIX / EXDEV

The Microsoft Store / MSIX-packaged Desktop install can fail to commit `local_<uuid>.json.tmp` to `local_<uuid>.json` due to EXDEV errors crossing the MSIX virtualisation boundary. This leaves `.tmp` files instead of finished metadata.

Quick test:

```
dir "%APPDATA%\Claude\claude-code-sessions\<acct>\<org>\" | findstr /i .tmp
```

If the result is empty, this is not your issue — the metadata files were fully written. If `.tmp` files exist, see [GitHub issue #48362](https://github.com/anthropics/claude-code/issues/48362) — switching from the Microsoft Store install to the standalone Win32 installer is the documented fix.

---

## `troubleshooting.json` schema

`troubleshooting.json` is the routing table `diagnose.py` consumes and the source of truth for `troubleshooting.md`.

### Row fields

| Field | Type | Description |
|---|---|---|
| `id` | string | Kebab-case unique identifier |
| `domain` | `"session"` \| `"worktree"` | Which subsystem this problem belongs to |
| `problem` | string | Symptom in plain user language |
| `what_to_do` | string | First action — always points at `diagnose.py` |
| `details` | string | Relative path to the deeper explanation, with anchor |
| `safety` | string | Preconditions before running any mutator |
| `fixture` | string | Name of the matching fixture directory under `fixtures/` |
| `mutator` | string \| null | Relative path to the mutator script, or `null` for read-only diagnoses |
| `match` | object | Predicate evaluated against the state snapshot |

### Match predicate language

```json
{ "any": [ { "snapshot.<field>": { ">=": 1 } } ] }
{ "all": [ { "snapshot.<field>": { "==": 0 } } ] }
```

Supported operators: `==`, `!=`, `>=`, `<=`, `>`, `<`, `in` (array membership), `regex` (string match).

The predicate is evaluated against the snapshot `diagnose.py` produces from the user's filesystem state. Nested fields use dot notation: `snapshot.cwd_prefix_types.bare_root`.

### Snapshot fields

| Field | Type | Description |
|---|---|---|
| `total_metadata_count` | int | Total `local_*.json` files |
| `metadata_with_cli_count` | int | Metadata files with `cliSessionId` present and non-null |
| `metadata_missing_cli_count` | int | Metadata files with `cliSessionId` absent or null |
| `metadata_dangling_cli_count` | int | Metadata where `cliSessionId` points at a missing JSONL |
| `metadata_duplicate_cli_count` | int | `cliSessionId` values that appear in more than one metadata file |
| `cwd_junction_mismatch_count` | int | Metadata where cwd is a junction path but JSONLs are at the canonical slug |
| `cwd_slug_mismatch_count` | int | Metadata where the cwd slug-encodes differently from the actual JSONL directory |
| `cwd_prefix_types` | object | Counts by type: `junction`, `canonical`, `bare_root`, `other` |
| `jsonl_orphan_count` | int | JSONLs with no metadata referencing them |
| `jsonl_count` | int | Total `.jsonl` files |
| `schema_version` | string | `"recognised"` or `"unrecognised"` |
| `desktop_version` | string \| null | Detected from `%LOCALAPPDATA%\AnthropicClaude\app-X.Y.Z\` |
| `cli_version` | string \| null | From `claude --version` if on PATH |
| `desktop_running` | bool | Whether `claude.exe` is in the tasklist |

---

## Fixture state directory layout

Fixtures use `--state <path>` to override the live AppData and projects directories.

```
<fixture>/state/
  appdata/
    Claude/
      claude-code-sessions/
        <account-uuid>/
          <org-uuid>/
            local_<session-uuid>.json
            ...
  projects/
    <slug>/
      <cli-session-id>.jsonl
      ...
```

`diagnose.py --state <path>` sets:
- `appdata_claude_dir` = `<path>/appdata/Claude`
- `projects_dir` = `<path>/projects`

All UUIDs in fixtures are deterministic fakes of the form `fixture-NN-XXXX-0000-0000-000000000000`.

---

## Diagnosis ID hash construction

The `diagnosis_id` is an 8-hex SHA-256 of these snapshot fields only (the `structural_keys` tuple in `make_diagnosis_id`):

```
total_metadata_count, metadata_with_cli_count, metadata_missing_cli_count,
metadata_dangling_cli_count, metadata_duplicate_cli_count,
metadata_null_timestamp_count, cwd_junction_mismatch_count,
cwd_slug_mismatch_count, truncated_jsonl_count, jsonl_orphan_count,
cwd_prefix_types, jsonl_count, schema_version, mapped_drive_unc_mismatch_count
```

Every structural detection signal is included, so two states get the same ID only when they are structurally identical. Version fields (`desktop_version`, `cli_version`) and process state (`desktop_running`) are excluded. A mutator gets a mismatch if the broken state changes between diagnosis and repair, but not if the CLI version is updated in the meantime. Fixture-mode runs skip live system detection entirely, so golden outputs are identical across environments.

---

## Session grouping layer

Claude Desktop lets users assign sessions to named groups ("OCD", "Work", "Recipes", etc.) and reorder them within each group. This state is stored separately from the two-layer session model above — it lives in Chromium Local Storage, not in the metadata JSON files.

### Storage location

```
%APPDATA%\Claude\Local Storage\leveldb\*.ldb  (SSTables)
%APPDATA%\Claude\Local Storage\leveldb\*.log  (WAL, uncompacted writes)
```

The filename (`004022.ldb`, etc.) changes after LevelDB compaction. `list_groupings.py` scans all `.ldb` and `.log` files, picking the entry with the highest LevelDB sequence number so recent writes not yet compacted into an SSTable are included.

**Origin key:** `_https://claude.ai` (ASCII, not UTF-16LE)
**Local Storage key:** `dframe-store`

### Schema

All grouping state lives in `dframe-store` → JSON → `state`:

```
state.customGroups            list[{id: "cg-<uuid>", name: str}]
state.customGroupAssignments  {session_key: group_id}
state.customGroupOrder        {group_id: [session_key, ...]}
```

Session keys match the `cliSessionId` format used by session metadata:

| Key format | Meaning |
|---|---|
| `code:local_<uuid>` | Worktree / local Claude Code session |
| `code:session_<id>` | Cloud session (claude.ai) |

Group IDs use the prefix `cg-` (not `g-`).

A second key, `LSS-persisted.dframe-local-slice`, holds a copy of `customGroupAssignments` only — no group names. `dframe-store` is authoritative.

### LevelDB format notes

Two Chromium-specific differences from upstream LevelDB:

1. **Magic bytes.** Footer bytes 40–47 are `57fb808b247547db`, not the upstream `57fb808b24e37478`. A parser using the upstream magic silently returns no results on Chromium files.
2. **Snappy-compressed index block.** The 5-byte block trailer's type byte is `0x01` (Snappy) for the index block. A parser that reads the index block as raw bytes gets garbage — misinterpreted restart count, buffer overruns.

`list_groupings.py` implements both fixes in pure Python with no pip dependencies.

### Read-only constraint

The grouping store is owned by Claude Desktop. Writing to it while Desktop holds the LevelDB open risks corruption. `list_groupings.py` is read-only by design. If grouping assignments are lost (e.g. after the known wipe bug on Desktop update), there is no automated recovery path — `list_groupings.py` can confirm what is currently assigned but cannot restore a prior state.

### Fixture path convention

`list_groupings.py --state <root>` expects the LevelDB directory at:

```
<root>/appdata/Claude/Local Storage/leveldb/
```

This matches the fixture layout used by all other tools in this repo.
