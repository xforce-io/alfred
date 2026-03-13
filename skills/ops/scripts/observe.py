"""Observability queries: status, heartbeat, tasks, logs, metrics."""

from __future__ import annotations

import json
import os
import re
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


def _read_status_snapshot(alfred_home: Path) -> Optional[Dict[str, Any]]:
    """Read and parse everbot.status.json."""
    status_file = alfred_home / "everbot.status.json"
    if not status_file.exists():
        return None
    try:
        return json.loads(status_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _is_pid_running(pid: int) -> bool:
    """Check if a process with given PID is running."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _read_pid(alfred_home: Path) -> Optional[int]:
    """Read PID from everbot.pid file."""
    pid_file = alfred_home / "everbot.pid"
    if not pid_file.exists():
        return None
    try:
        return int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return None


def _compute_uptime(started_at: str) -> Optional[int]:
    """Compute uptime in seconds from ISO-8601 started_at."""
    try:
        start = datetime.fromisoformat(started_at)
        now = datetime.now(start.tzinfo) if start.tzinfo else datetime.now()
        return max(0, int((now - start).total_seconds()))
    except (ValueError, TypeError):
        return None


def cmd_status(alfred_home: Path) -> Dict[str, Any]:
    """Daemon status: running state, PID, uptime, agents, project_root."""
    snapshot = _read_status_snapshot(alfred_home)
    pid = _read_pid(alfred_home)
    running = pid is not None and _is_pid_running(pid)

    if snapshot is None and not running:
        return {"ok": False, "command": "status", "error": "daemon not running",
                "hint": "Run 'bin/everbot start' to start the daemon"}

    if snapshot is None:
        snapshot = {}

    uptime = _compute_uptime(snapshot.get("started_at", "")) if running else None

    return {
        "ok": True,
        "command": "status",
        "data": {
            "running": running,
            "pid": pid,
            "uptime_seconds": uptime,
            "started_at": snapshot.get("started_at"),
            "project_root": snapshot.get("project_root", ""),
            "agents": snapshot.get("agents", []),
            "timestamp": snapshot.get("timestamp"),
        },
    }


def cmd_heartbeat(alfred_home: Path, agent: Optional[str] = None) -> Dict[str, Any]:
    """Heartbeat status for all or a specific agent."""
    snapshot = _read_status_snapshot(alfred_home)
    if snapshot is None:
        return {"ok": False, "command": "heartbeat", "error": "status file not found",
                "hint": "Is the daemon running?"}

    heartbeats = snapshot.get("heartbeats", {}) or {}

    if agent:
        if agent not in heartbeats:
            known = snapshot.get("agents", [])
            if agent not in known:
                return {"ok": False, "command": "heartbeat",
                        "error": f"agent not found: {agent}",
                        "hint": f"Known agents: {', '.join(known) if known else 'none'}"}
            # Agent exists but no heartbeat yet
            return {"ok": True, "command": "heartbeat",
                    "data": {"agent": agent, "heartbeat": None}}
        return {"ok": True, "command": "heartbeat",
                "data": {"agent": agent, "heartbeat": heartbeats[agent]}}

    return {"ok": True, "command": "heartbeat", "data": {"heartbeats": heartbeats}}


def _parse_heartbeat_tasks(content: str) -> List[Dict[str, Any]]:
    """Extract tasks from HEARTBEAT.md JSON block."""
    pattern = r"```json\s*\n(.*?)\n\s*```"
    m = re.search(pattern, content, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
        return data.get("tasks", [])
    except (json.JSONDecodeError, AttributeError):
        return []


def cmd_tasks(alfred_home: Path, agent: str) -> Dict[str, Any]:
    """List tasks from an agent's HEARTBEAT.md."""
    agent_dir = alfred_home / "agents" / agent
    if not agent_dir.exists():
        return {"ok": False, "command": "tasks",
                "error": f"agent workspace not found: {agent}"}

    heartbeat_file = agent_dir / "HEARTBEAT.md"
    if not heartbeat_file.exists():
        return {"ok": True, "command": "tasks",
                "data": {"agent": agent, "tasks": []}}

    try:
        content = heartbeat_file.read_text(encoding="utf-8")
    except OSError as e:
        return {"ok": False, "command": "tasks", "error": f"failed to read HEARTBEAT.md: {e}"}

    tasks = _parse_heartbeat_tasks(content)

    # Extract summary fields
    task_summaries = []
    for t in tasks:
        task_summaries.append({
            "id": t.get("id", ""),
            "title": t.get("title", ""),
            "schedule": t.get("schedule"),
            "state": t.get("state", "unknown"),
            "last_run_at": t.get("last_run_at"),
            "next_run_at": t.get("next_run_at"),
            "retry": t.get("retry", 0),
            "max_retry": t.get("max_retry", 3),
            "error_message": t.get("error_message"),
            "execution_mode": t.get("execution_mode", "auto"),
        })

    return {
        "ok": True, "command": "tasks",
        "data": {
            "agent": agent,
            "tasks": task_summaries,
            "total": len(task_summaries),
            "failed": sum(1 for t in task_summaries if t["state"] == "failed"),
        },
    }


def _tail_file(path: Path, n: int = 50) -> List[str]:
    """Read the last n lines of a file."""
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return list(deque(f, maxlen=n))
    except OSError:
        return []


LOG_SOURCES = {
    "daemon": "everbot.out",
    "heartbeat": "heartbeat.log",
    "web": "everbot-web.out",
}


def cmd_logs(
    alfred_home: Path,
    source: str = "heartbeat",
    tail: int = 50,
    level: Optional[str] = None,
    agent: Optional[str] = None,
) -> Dict[str, Any]:
    """Read recent log lines."""
    if source not in LOG_SOURCES:
        return {"ok": False, "command": "logs",
                "error": f"unknown log source: {source}",
                "hint": f"Valid sources: {', '.join(LOG_SOURCES.keys())}"}

    log_file = alfred_home / "logs" / LOG_SOURCES[source]
    if not log_file.exists():
        return {"ok": False, "command": "logs",
                "error": f"log file not found: {log_file}"}

    lines = _tail_file(log_file, tail * 3 if (level or agent) else tail)

    # Filter by level
    if level:
        level_upper = level.upper()
        lines = [line for line in lines if level_upper in line]

    # Filter by agent
    if agent:
        lines = [line for line in lines if agent in line]

    # Trim to requested count after filtering
    lines = lines[-tail:]

    # Strip trailing newlines
    lines = [line.rstrip("\n") for line in lines]

    # Count error/warning
    error_count = sum(1 for line in lines if "ERROR" in line)
    warning_count = sum(1 for line in lines if "WARNING" in line)

    return {
        "ok": True, "command": "logs",
        "data": {
            "source": source,
            "file": str(log_file),
            "lines": lines,
            "count": len(lines),
            "error_count": error_count,
            "warning_count": warning_count,
        },
    }


def cmd_metrics(alfred_home: Path) -> Dict[str, Any]:
    """Runtime metrics from status snapshot."""
    snapshot = _read_status_snapshot(alfred_home)
    if snapshot is None:
        return {"ok": False, "command": "metrics", "error": "status file not found",
                "hint": "Is the daemon running?"}

    metrics = snapshot.get("metrics", {}) or {}
    return {
        "ok": True, "command": "metrics",
        "data": {
            "metrics": metrics,
            "daemon_status": snapshot.get("status", "unknown"),
            "timestamp": snapshot.get("timestamp"),
        },
    }
