---
name: routine-manager
description: Manage structured routines in HEARTBEAT.md with add/list/update/remove operations.
version: "1.0.0"
tags: [routine, heartbeat, scheduler]
---

# Routine Manager

Manage structured routines stored in `HEARTBEAT.md` JSON task block.

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
python skills/routine-manager/scripts/routine_cli.py --workspace "$WORKSPACE_ROOT" list --include-disabled
```

### add_routine (periodic)

```bash
python skills/routine-manager/scripts/routine_cli.py --workspace "$WORKSPACE_ROOT" add \
  --title "Daily digest" \
  --description "Summarize key updates" \
  --schedule "1d" \
  --execution-mode "auto" \
  --source "heartbeat_reflect"
```

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
