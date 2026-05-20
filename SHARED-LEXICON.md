# Claude Code Session Recovery — Lexicon

Terms used across this repo's docs. Match Claude Code / Desktop product copy where possible.

In user-facing docs (e.g. `docs/session-recovery.md`), describe technical concepts in plain English. In technical docs (`docs/architecture.md`, code comments), use the exact terms below.

## Language

**Session**:
One chat instance with a title, model, date, and conversation history. The unit.
_Avoid_: chat, conversation (as a unit), thread, window

**Session list**:
The left sidebar in Claude Desktop — the list of past sessions.
_Avoid_: session history, history (alone), chat history, picker

**Conversation history**:
The messages inside one session — what shows in the main pane when the session opens.
_Avoid_: conversation content, messages list, transcript (transcript is the file on disk, not the rendered messages)

**Metadata file**:
The JSON file that stores a session's title, model, date, and configuration. Reserve for technical docs; in user-facing docs say "the file that holds the title and date".
_Avoid_: session file, local_*.json, info file

**Transcript file**:
The JSONL file that stores the messages of one session. Reserve for technical docs; in user-facing docs say "the file that holds the conversation".
_Avoid_: conversation file, JSONL, log file

**cliSessionId**:
The field in the metadata file that points at the transcript file. Its value is the transcript filename's UUID stem.
_Avoid_: session ID, linker field, transcript ID

**Project slug**:
The directory name under `~/.claude/projects/` derived from the cwd path string at session start.
_Avoid_: project key, slug (alone), project ID

## Relationships

- A **Session** has one **Metadata file** and one **Transcript file**.
- The **Metadata file** holds the **cliSessionId** that points at the **Transcript file**.
- The **Session list** is rendered from all readable **Metadata files**.
- **Conversation history** is rendered from the **Transcript file** the metadata's `cliSessionId` points at.
- A **Project slug** groups transcripts on disk; one project produces one slug — unless the project folder is renamed, or accessed via a Windows junction.

## Example dialogue

> **Reader:** "My session is showing in my session list but the conversation history is empty when I click it."
> **Maintainer:** "That's a broken link in the session's metadata file — the `cliSessionId` field that points at the transcript file is missing or null. Desktop can render the session in the sidebar because the metadata is intact, but it has nothing to load into the main pane."

## Flagged ambiguities

- **"Session history"** is what users will naturally say — translate to **session list** when transcribing bug reports. Our docs reserve "history" for **conversation history** (the messages within one session) to avoid the overload.
- **"History"** alone is forbidden in docs. Always prefix with "conversation".
- **"Session" vs "conversation"** — not synonyms. A session is the unit (the row in the sidebar). A conversation is the content (the messages within one session).
