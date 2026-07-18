See AGENTS.md for full context.

## Session staleness

No time-based threshold reliably distinguishes live sessions from dead ones. Users may keep Claude Code sessions open for weeks or months without writes. A transcript with mtime > 24h, a branch with no recent commits, or `commits_ahead == 0` may still belong to a live session.

Never use as "session is dead" evidence: transcript mtime, last-activity age, `commits_ahead == 0`, branch age, or any "looks idle" heuristic.

Reliable live-session signals on Windows only: filesystem lock probe (`Rename-Item` — errors if live cwd handle) or process cwd enumeration (`Get-CimInstance Win32_Process`). Default to skip unless the user has explicitly named a specific session to act on.
