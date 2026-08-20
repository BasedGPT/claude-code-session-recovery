# Read-only transcript audits

These commands inspect transcript files without changing them. They are useful
when a normal diagnosis shows a link or path problem but the physical JSONL
layout itself is uncertain.

```text
python tools/sessions/audit_transcript_integrity.py --state <fixture-state>
python tools/sessions/audit_transcript_integrity.py --projects-dir <projects> --json
python tools/sessions/audit_transcript_identity.py --state <fixture-state> --json
```

`audit_transcript_integrity.py` counts file presence, empty/unreadable files,
line and byte facts, UTF-8/JSON/NUL anomalies, and transcript graph topology.
It reports roots, missing parents, forks, leaves, weak components, reachability
from explicit roots, unrooted nodes, and cycles as facts. It does not infer an
active leaf, resume reachability, crash damage, or a safe cleanup action.

`audit_transcript_identity.py` inventories every physical direct child JSONL,
retains duplicate session IDs across slugs, compares metadata links against
all candidates, and reports observed slug collisions and resolved-path splits.
Its `worktree_key_mismatch_candidate` label is evidence for review only; it is
not a confirmed rebind.

Both commands default to opaque deterministic references. Use `--details` for
per-record structural counts and `--include-paths` only when explicitly needed;
paths are user-home redacted. `--transcript` may name a missing file so the
missing/empty detector boundary remains explicit. Limits produce a bounded
result rather than unbounded record retention. The node limit also caps stored
parent-reference occurrences; the output reports total, retained, and
truncated parent-reference counts. Inaccessible entries below a valid projects
root, and inaccessible Desktop account or organisation metadata directories in
the identity audit, produce opaque scan errors and a partial result while
retaining exit 0.
An unavailable scan root or invalid limit exits 2. Findings do not otherwise
change the successful audit exit status.

## Duplicate transcript IDs

`tools/transcript_files.py` now exposes the lossless
`build_transcript_path_inventory()` map. The legacy
`build_transcript_index()` compatibility helper raises
`DuplicateTranscriptIdError` instead of silently selecting the last path. The
inventory also reports `complete` or `partial` plus opaque discovery errors;
the compatibility index refuses a partial inventory because an inaccessible
slug could conceal another physical path with the same session ID. The legacy
path iterator also raises before yielding any visible path from a partial scan.
Read-only
diagnosis suppresses absence and uniqueness conclusions when discovery is
partial. Truncated-content checks skip ambiguous IDs. Repair, repoint,
synthesis, and VS Code cache recovery refuse partial inventories and skip or
refuse duplicate IDs; they never choose one path implicitly. No merge,
relocation, junction, metadata rebind, cache write, or transcript mutation is
performed by these audits.

Metadata-dependent mutators use the same fail-closed rule through a shared
metadata inventory. Inaccessible account or organisation directories,
unreadable `local_*.json` files, malformed JSON, and non-object JSON make that
inventory partial. Repair, synthesis, repoint, duplicate cleanup, cwd rewrite,
deleted-worktree recovery, and fixture-mode VSS selection refuse the entire
inference before staging or mutating anything. A genuinely absent optional
metadata root remains a complete empty inventory.

This is intentionally a read-only evidence layer, not a new troubleshooting
route. Mutation remains subject to the existing diagnosis-token, backup,
Desktop-closed, dry-run, and apply gates documented in
[`docs/architecture.md`](architecture.md).
