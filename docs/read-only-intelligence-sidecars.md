# Read-only intelligence sidecars

These commands collect bounded structural evidence for manual investigation.
They do not diagnose a repair, choose a canonical session, or write to Claude
or VS Code state.

## Session lineage evidence

```text
python tools/sessions/audit_session_lineage.py
python tools/sessions/audit_session_lineage.py --json
python tools/sessions/audit_session_lineage.py --state <fixture-state-path> --json
```

`audit_session_lineage.py` reads Desktop `local_*.json` metadata and transcript
JSONL files. It considers only:

- explicit `forkedFrom` references;
- UUIDs that occur in more than one transcript;
- the first valid timestamp observed for a session; and
- equal titles, represented by an opaque deterministic title-group identifier.

Its classifications are deliberately narrow:

- `explicit_lineage`
- `shared_history_candidate`
- `title_only_ambiguous`
- `insufficient_evidence`

The classifications are evidence labels, not repair conclusions. In
particular, the tool never calls a session duplicate, stale, canonical,
removable, or safe to delete. Prompt and message content are not emitted.
Session identifiers, title groups, and error subjects are opaque by default.
Paths appear only with the explicit `--include-paths` option.

Each Desktop metadata record is a distinct session identity, using its
`sessionId` together with the metadata record boundary. A shared
`cliSessionId` maps transcript evidence onto those sessions; it never merges
the metadata records into one session.

Membership is indexed in a temporary SQLite database so it does not require an
unbounded in-memory UUID set. The temporary directory is removed on success or
failure. UUID memberships are read in index order and candidate pairs are
materialised one at a time into a bounded relationship table. The operation
stops before exceeding `--max-relationships`; it does not issue a transcript
membership self-join. File, directory-entry, JSONL-line, line-byte, per-file
and total-byte, node, relationship-operation, and finding caps are enforced. A
cap or read/parse failure produces `status: partial`, structured errors, and
exit code 2. If relationship generation reaches its cap, all findings,
classifications, and relationship-derived counts are suppressed. The partial
result retains only raw input counters for scanned files, transcripts, records,
and bytes.

The same suppression applies whenever the scan state is partial, starting with
metadata/transcript traversal and continuing through relationship
materialisation, commit, and finding construction. There is no exception for a
partial condition raised before relationship generation: inferred session,
relationship, reported-session, and classification counts become null and
findings are empty. Only raw counters for files, transcripts, records, and
bytes already scanned remain. Failure to create, connect to, initialise, query,
close, or clean the temporary index returns a privacy-safe structured partial
result and exit code 2; exception details and tracebacks are not emitted.

## Local-agent session inventory

```text
python tools/sessions/inventory_local_agent_sessions.py
python tools/sessions/inventory_local_agent_sessions.py --json
python tools/sessions/inventory_local_agent_sessions.py --state <fixture-state-path> --json
```

`inventory_local_agent_sessions.py` traverses the expected
`local-agent-mode-sessions/<account>/<organisation>/local_*` directory shape.
It reports aggregate root, owner, local-session, outputs-directory, and
output-entry counts grouped only under the `root`, `owner`, `local_*`, and
`outputs` structural buckets. It also counts nested local-agent transcript
roots at `local_*/.claude/projects/<encoded-cwd>/*.jsonl` under
`transcript_roots`. Those counts are kept separate from the standard
`claude-code-sessions` metadata root and standard `projects` transcript root.
When invoked with `--state` (or with the explicit standard-root overrides), the
`standard_roots` section reports only bounded aggregate counts and statuses for
those two standard roots; it never merges them with local-agent counts.
The inventory refuses to follow observed symlinks or Windows directory
junctions at the root or within these fixed scan shapes. A reparse point,
outside-resolved path, or boundary-check failure is reported as partial and
its linked entries are not counted.

It emits no owner or session rows and no root, owner, or session identifiers.
Only error subjects are opaque identifiers. It does not read local output or
transcript files, parse LevelDB, inspect conversation content, or classify any
session as Cowork (or any other product mode).

Directory, owner, session, output-entry, and nested-transcript-entry caps are
enforced. Incomplete or unreadable traversal is explicit as `status: partial`
and exit code 2; a reparse-point boundary failure follows the same contract.

## VS Code session surfaces

```text
python tools/sessions/audit_vscode_session_surfaces.py
python tools/sessions/audit_vscode_session_surfaces.py --json
python tools/sessions/audit_vscode_session_surfaces.py --state <fixture-state-path> --json
```

`audit_vscode_session_surfaces.py` reports:

- the count of project slugs containing transcript JSONL files;
- `sessions-index.json` presence count and total size only; and
- the count of `state.vscdb` databases containing the established
  `agentSessions.model.cache` key.

The live `state.vscdb` is never opened with SQLite. The audit first stats the
exact source set (`state.vscdb` plus optional `-wal` and `-shm`) and rejects a
combined footprint larger than `--max-database-bytes`. It chunk-copies that
exact set into a temporary directory while enforcing the same cumulative byte
cap. Before copying, and again after SQLite inspection, it computes internal
chunked SHA-256 fingerprints for every live source using
stat-before/hash/stat-after stability checks. It also compares device, inode,
size, and nanosecond mtime values for every source. Any appearance,
disappearance, replacement, content rewrite, growth, shrink, or mtime change
makes the result partial and suppresses cache detection. Fingerprints are never
emitted. Temporary snapshot files are removed on every exit path.

After the temporary SQLite query completes, the audit stats and compares the
entire live DB/WAL/SHM set once more against the original pre-copy identities.
This final gate catches source drift during snapshot inspection; a cache-key
result is discarded unless both the post-copy and post-query comparisons match.

Only the stable temporary snapshot is opened with SQLite `mode=ro` and
`PRAGMA query_only=ON`. The query targets the established key with an equality
lookup, and a SQLite progress handler interrupts work at
`--max-database-opcodes`. SQLite may create or update WAL coordination files
only inside the temporary directory; it cannot create a missing live `-shm`.
The audit does not parse either the index file or the cache value. A complete
scan uses only `index_only`, `db_only`, `both`, or `neither`; none is a recovery
recommendation. Any partial scan emits `surface_combination: null` and marks
the index and database conclusions `conclusive: false`. Directory, slug,
database-count, database-byte, and SQLite-operation caps are enforced, with
the same partial/error contract as the other sidecars.

## Privacy and exit contract

All three commands hide filesystem paths by default and never emit prompt,
message, output-file, index-file, or cache-value content. JSON output is sorted
for deterministic comparison. Missing roots and stores are distinguished from
access failures; access failures always make the result partial and expose only
a bounded opaque error subject.

- Exit 0: the bounded scan completed.
- Exit 2: a cap, unreadable source, invalid record, or database error made the
  result partial.

These tools have no `--apply` mode and no writer or recovery action.
