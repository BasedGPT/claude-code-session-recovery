# Recovering deleted JSONLs

Claude Desktop prunes old transcript files, and they can also disappear through accidental deletion or a failed backup restore. A missing JSONL is a starting point for investigation, not a verdict. Work through the checklist below before concluding the conversation is gone.

---

## Where to look

1. **User-configured daily backup of `~\.claude\projects\`**

   Ask yourself whether you have a scheduled backup (Task Scheduler, robocopy, rsync, or similar) that mirrors `%USERPROFILE%\.claude\projects\` to another location. Common patterns:

   - `%USERPROFILE%\OneDrive\<some folder>\.claude-userdata\projects\`
   - A network drive
   - An external drive

   If you have one, compare file stems in that location against the missing UUID. The transcript filename is `<uuid>.jsonl`; the UUID comes from the `cliSessionId` field in the Desktop metadata file at `%APPDATA%\Claude\claude-code-sessions\<acct>\<org>\local_<uuid>.json`.

2. **Windows VSS shadow copies**

   Available if System Protection is enabled on your C: drive. Two ways to check:

   - Right-click the `projects\` folder in Explorer → Properties → Previous Versions tab.
   - From an elevated PowerShell prompt: `vssadmin list shadows`

   Look for any snapshot dated before the JSONL disappeared. If one exists, mount it and copy the file out.

3. **Cloud-provider version history on the backup folder itself**

   If your backup destination sits inside an OneDrive, Dropbox, or iCloud folder, those services keep version history on the folder contents — not just individual files. Check:

   - OneDrive web → navigate to the backup folder → Version History
   - Dropbox web → navigate to the folder → click the clock icon
   - iCloud Drive → check for a Recents or version history option in the web interface *(macOS only)*

4. **`%APPDATA%\Claude\local-agent-mode-sessions\<acct>\<org>\local_<sid>\`**

   Each session has a directory here containing `audit.jsonl` — a per-session agent action log recording what tools fired and in what order. This does **not** contain conversation transcript content, but it confirms the session existed and what it did. One `local_<sid>/` directory per session, keyed by the Desktop session ID — the UUID in the metadata filename (`local_<uuid>.json`), separate from the `cliSessionId` field inside it. Useful for confirming timing and activity even when the transcript content is not available elsewhere.

5. **FTS5 transcript index at `~\.claude\transcript-index\index.db`**

   Partial recovery path. If the session's subagent compactions were indexed, `agent-acompact-*` records may contain conversation summaries. In one documented incident, this recovered 548 messages across 2 of 9 lost sessions. Querying the index requires SQL against an FTS5 database — more technical than the other options. See the "If nothing is found" section below if you want to pursue this path.

6. **Recycle Bin**

   Long shot. If the file was deleted interactively rather than pruned by Desktop, it may still be in the Recycle Bin. Open the Recycle Bin in Explorer and look for a file matching the UUID stem, or check programmatically:

   ```powershell
   # List Recycle Bin contents for .jsonl files
   (New-Object -ComObject Shell.Application).Namespace(0xA).Items() |
     Where-Object { $_.Name -match '\.jsonl$' }
   ```

---

## Other sources

The list above covers the most common places. Other sources worth checking include Time Machine (macOS only), Restic, Borg, or Duplicity snapshots, organisation backup tools such as CrashPlan or Veeam Endpoint Backup, and network drives that mirror your home directory. Look for any tool that snapshots `%USERPROFILE%\.claude\projects\<slug>\<uuid>.jsonl`.

---

## After finding the file

Copy the recovered JSONL to:

```
%USERPROFILE%\.claude\projects\<expected-slug>\<session-id>.jsonl
```

The Desktop metadata already has `cliSessionId` pointing at this UUID. Once the file exists at the expected path, Desktop will render it on next launch — no metadata edits needed.

The `<expected-slug>` is derived from the session's `cwd` path: replace each `:`, `\`, `/`, `.`, and space with `-`. You can find the exact slug by checking the `cwd` field in the Desktop metadata file:

```
%APPDATA%\Claude\claude-code-sessions\<acct>\<org>\local_<uuid>.json
```

Open that file, read `cwd`, and apply the substitution. The resulting string is the slug; the directory `%USERPROFILE%\.claude\projects\<slug>\` is where the JSONL must go.

---

## If nothing is found

Partial recovery via the FTS5 transcript index (item 5 above) is the next path to attempt — it involves SQL queries against `~\.claude\transcript-index\index.db` and manual reconstruction from compaction summaries. This is more involved than the checklist above and not documented here. If you want help with it, or if you have worked through the full checklist and found nothing, open an issue on this repository with the output of `python tools/diagnose.py` and describe what backups you checked.
