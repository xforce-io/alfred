---
name: trajectory-reviewer
description: Review recent agent trajectory files and dolphin.log to detect failures, loops, latency spikes, and actionable fixes.
version: "0.1.0"
tags: [trajectory, review, debugging, log-analysis]
---

# Trajectory Reviewer

Use this skill when the user asks the agent to self-review recent execution behavior, find problems, or diagnose instability.

## What This Skill Checks

- Recent trajectory files (`~/.alfred/agents/*/tmp/trajectory_*.json`)
- Optional daemon/runtime log (`log/dolphin.log` by default)
- Error density, repeated failures, possible loop patterns, and long response gaps

## Command

```bash
python skills/trajectory-reviewer/scripts/review_recent.py --limit-files 2 --tail-lines 3000
```

Optional filters:

```bash
python skills/trajectory-reviewer/scripts/review_recent.py \
  --agent daily_insight \
  --session heartbeat_session_daily_insight \
  --limit-files 5 \
  --tail-lines 5000 \
  --output /tmp/trajectory_review.md
```

## Recommended Workflow

1. Run the script with `--limit-files 2` first.
2. Read `High` findings first, then `Medium`.
3. Convert findings into concrete fixes (prompt update, tool guardrail, retry policy, timeout split, etc.).
4. Re-run after fixes and compare issue counts.

## Output Contract

The script returns Markdown with:

- Scope and files analyzed
- Metric summary
- Findings ordered by severity (`High`, `Medium`, `Low`)
- Suggested next actions

If `--output` is provided, the same report is written to that file.
