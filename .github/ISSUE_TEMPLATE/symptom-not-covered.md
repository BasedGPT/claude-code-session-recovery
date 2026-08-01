---
name: Symptom not in the routing table
about: You have a broken-state symptom that diagnose.py doesn't recognise
title: 'Symptom not covered: '
labels: 'symptom-not-covered'
---

**Use this template if `python tools/diagnose.py` says "State layout not in supported fixture set" or matches no known problem.**

## What you saw

[Describe the broken state. Be concrete: "X happens when I click Y" beats "history is weird".]

## Diagnose output

```
[paste the full diagnose.py --json output]
```

## State inventory (optional but very helpful)

If you're comfortable, list:
- How many `local_*.json` files in the platform's Claude `claude-code-sessions/<account>/<org>` directory
- How many `*.jsonl` files in `~/.claude/projects/` (any slug directory)
- Whether Claude Desktop is in the process list right now

Do not paste real session UUIDs or transcripts. State-shape only.

## Environment

- Claude Code CLI version:
- Claude Desktop version:
- Operating system and version:
