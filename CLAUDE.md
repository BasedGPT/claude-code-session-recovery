See AGENTS.md for full context.

## Session staleness

No time-based threshold reliably distinguishes live sessions from dead ones. Robbie keeps Claude Code sessions open for weeks or months without writes. A transcript with mtime > 24h, a branch with no recent commits, or `commits_ahead == 0` are all equally likely to be a live session he's returning to.

Never use as "session is dead" evidence: transcript mtime, last-activity age, `commits_ahead == 0`, branch age, or any "looks idle" heuristic.

Reliable live-session signals on Windows only: filesystem lock probe (`Rename-Item` — errors if live cwd handle) or process cwd enumeration (`Get-CimInstance Win32_Process`). Default to skip unless Robbie has explicitly named a specific session to act on.
