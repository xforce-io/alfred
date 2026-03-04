---
name: routine-manager
description: Manage structured routines in HEARTBEAT.md with add/list/update/remove operations.
version: "1.0.0"
tags: [routine, heartbeat, scheduler]
---

# Routine Manager

Manage structured routines stored in `HEARTBEAT.md` JSON task block.

## Agent Guidelines

- When the user asks "有哪些任务" / "列出任务" / "show routines", always use `--format table` for human-readable output.
- When programmatically parsing routine data, use the default JSON format.

## When To Use

- The user asks to create, edit, disable, or remove recurring routines.
- Heartbeat reflection detects a recurring intent that is not scheduled yet.
- You need to inspect current routines before creating a new one.

## Commands

Use the CLI helper:

```bash
python skills/routine-manager/scripts/routine_cli.py --workspace "$WORKSPACE_ROOT" <command> [options]
```

## Functions

### list_routines

```bash
# JSON output (default, for programmatic use)
python skills/routine-manager/scripts/routine_cli.py --workspace "$WORKSPACE_ROOT" list --include-disabled

# Table output (for human display)
python skills/routine-manager/scripts/routine_cli.py --workspace "$WORKSPACE_ROOT" list --format table
```

Table output example:

```
ID                    Title                     Schedule        State       Enabled   Skill             Next Run
────────────────────  ────────────────────────  ──────────────  ──────────  ────────  ────────────────  ────────────────────────
routine_86e85e59      每日新闻简报               1d              pending     True      -                 2026-03-05T09:10:42
```

### add_routine (periodic — fixed time of day, use cron)

For tasks that must run at a **specific time each day**, use a cron expression with `--timezone`:

```bash
python skills/routine-manager/scripts/routine_cli.py --workspace "$WORKSPACE_ROOT" add \
  --title "Daily attractor push" \
  --description "Push attractor case at 15:00 daily" \
  --schedule "0 15 * * *" \
  --timezone "Asia/Shanghai" \
  --execution-mode "isolated" \
  --source "manual"
```

### add_routine (periodic — fixed interval, use interval string)

For tasks that repeat every N hours/days **regardless of wall-clock time**, use an interval string:

```bash
python skills/routine-manager/scripts/routine_cli.py --workspace "$WORKSPACE_ROOT" add \
  --title "Daily digest" \
  --description "Summarize key updates" \
  --schedule "1d" \
  --timezone "Asia/Shanghai" \
  --execution-mode "auto" \
  --source "heartbeat_reflect"
```

> **Important**: Always pass `--timezone` for recurring tasks. If omitted, the system defaults to the local timezone and logs a warning. For fixed-time schedules (e.g., "每日15:00"), always use cron expressions (`0 15 * * *`), NOT interval strings (`1d`).

### add_routine (one-shot with relative delay — preferred)

Use `--delay` for tasks that should run after a relative time offset. Accepts `s` (seconds), `m` (minutes), `h` (hours), `d` (days):

```bash
python skills/routine-manager/scripts/routine_cli.py --workspace "$WORKSPACE_ROOT" add \
  --title "Remind me in 2 minutes" \
  --description "Tell a programmer joke" \
  --delay "2m"
```

### add_routine (one-shot with absolute time)

Use `--next-run-at` (ISO-8601) for tasks that should run at a specific time:

```bash
python skills/routine-manager/scripts/routine_cli.py --workspace "$WORKSPACE_ROOT" add \
  --title "Morning briefing" \
  --description "Summarize overnight updates" \
  --next-run-at "2026-02-14T08:00:00+08:00"
```

### add_routine (skill-bound with scanner gate)

For routines that trigger a skill and optionally gate execution on a scanner:

```bash
python skills/routine-manager/scripts/routine_cli.py --workspace "$WORKSPACE_ROOT" add \
  --title "会话变化分析" \
  --description "当会话发生变化时，分析潜在问题" \
  --schedule "30m" \
  --skill "memory-review" \
  --scanner "session" \
  --min-execution-interval "2h" \
  --execution-mode "isolated" \
  --source "manual"
```

- `--skill`: The skill to invoke when the routine fires.
- `--scanner`: Scanner gate type; the routine only executes if the scanner detects changes.
- `--min-execution-interval`: Minimum interval between actual skill executions (e.g. `2h`), even if the scanner triggers more frequently.

### update_routine

```bash
python skills/routine-manager/scripts/routine_cli.py --workspace "$WORKSPACE_ROOT" update \
  --id "routine_abcd1234" \
  --description "Updated description" \
  --execution-mode "inline"
```

### remove_routine

Soft delete (disable):

```bash
python skills/routine-manager/scripts/routine_cli.py --workspace "$WORKSPACE_ROOT" remove --id "routine_abcd1234"
```

Hard delete:

```bash
python skills/routine-manager/scripts/routine_cli.py --workspace "$WORKSPACE_ROOT" remove --id "routine_abcd1234" --hard
```

## Safety Notes

- Always `list` before `add` to avoid duplicate routines.
- For destructive operations (`--hard`), confirm user intent first.
- Keep `execution_mode` explicit: `inline` for short tasks, `isolated` for long/complex tasks.
- `execution_mode=auto` is supported and will be inferred by framework heuristics.
