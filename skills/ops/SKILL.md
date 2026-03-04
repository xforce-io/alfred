---
name: ops
description: Operations and observability for Alfred daemon — status, heartbeat, tasks, logs, metrics, lifecycle management, and diagnostics.
version: "1.0.0"
tags: [ops, observability, monitoring, lifecycle, diagnostics]
---

# Ops

Perform common operations and observability queries on an Alfred (EverBot) environment.

## When To Use

- Check if the Alfred daemon is running and healthy
- View agent heartbeat status and recent results
- Inspect scheduled tasks and their execution state
- Read and filter system logs (daemon, heartbeat, web)
- Query runtime metrics (session count, LLM latency, tool calls)
- Start, stop, or restart the daemon
- Run a comprehensive diagnostic check

## Commands

All commands go through a single dispatcher:

```bash
python skills/ops/scripts/ops_cli.py <command> [options]
```

Global option: `--alfred-home <path>` (default: `~/.alfred`)

### status

```bash
python skills/ops/scripts/ops_cli.py status
```

Returns daemon running state, PID, uptime, registered agents, and project root.

### heartbeat

```bash
python skills/ops/scripts/ops_cli.py heartbeat
python skills/ops/scripts/ops_cli.py heartbeat --agent daily_insight
```

Returns heartbeat timestamps and result previews for all or a specific agent.

### tasks

```bash
python skills/ops/scripts/ops_cli.py tasks --agent daily_insight
```

Returns the HEARTBEAT.md task list for a specific agent (id, title, schedule, state, timing).

### logs

```bash
python skills/ops/scripts/ops_cli.py logs --source heartbeat --tail 50
python skills/ops/scripts/ops_cli.py logs --source daemon --level ERROR
python skills/ops/scripts/ops_cli.py logs --source heartbeat --agent daily_insight
```

Reads recent log lines from daemon, heartbeat, or web log files. Supports filtering by level and agent.

### metrics

```bash
python skills/ops/scripts/ops_cli.py metrics
```

Returns runtime metrics from the daemon status snapshot.

### diagnose

```bash
python skills/ops/scripts/ops_cli.py diagnose
python skills/ops/scripts/ops_cli.py diagnose --agent daily_insight
```

Runs a comprehensive health check: daemon state, heartbeat freshness, task failure rate, log error density. Returns an overall health score (healthy / degraded / unhealthy) with actionable findings.

### start / stop / restart

```bash
python skills/ops/scripts/ops_cli.py start
python skills/ops/scripts/ops_cli.py stop
python skills/ops/scripts/ops_cli.py restart
```

Lifecycle management. Locates `bin/everbot` via `project_root` from the status snapshot.

## Output Contract

All commands return JSON to stdout:

```json
{"ok": true, "command": "status", "data": {...}}
```

On error:

```json
{"ok": false, "command": "status", "error": "daemon not running", "hint": "Run 'bin/everbot start'"}
```

Exit code: `0` on success, `1` on error.
