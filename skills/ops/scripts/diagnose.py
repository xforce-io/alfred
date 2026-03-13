"""Comprehensive diagnostics: aggregate status, heartbeat, tasks, logs into a health report."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from observe import (
    _read_status_snapshot,
    _read_pid,
    _is_pid_running,
    _parse_heartbeat_tasks,
    _tail_file,
    LOG_SOURCES,
)


def _check_daemon(alfred_home: Path, snapshot: Optional[Dict]) -> Dict[str, Any]:
    """Check daemon running state."""
    pid = _read_pid(alfred_home)
    running = pid is not None and _is_pid_running(pid)
    return {
        "name": "daemon",
        "status": "ok" if running else "critical",
        "detail": f"running (pid={pid})" if running else "not running",
    }


def _check_heartbeats(
    snapshot: Optional[Dict], agent: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Check heartbeat freshness."""
    if not snapshot:
        return [{"name": "heartbeat", "status": "unknown", "detail": "no status snapshot"}]

    heartbeats = snapshot.get("heartbeats", {}) or {}
    agents = snapshot.get("agents", [])
    results = []

    targets = [agent] if agent else agents
    for name in targets:
        hb = heartbeats.get(name)
        if not hb:
            results.append({
                "name": f"heartbeat:{name}",
                "status": "warning",
                "detail": "no heartbeat recorded",
            })
            continue

        ts_str = hb.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str)
            now = datetime.now(ts.tzinfo) if ts.tzinfo else datetime.now()
            age_seconds = (now - ts).total_seconds()
            if age_seconds > 7200:  # > 2 hours
                status = "critical"
            elif age_seconds > 3600:  # > 1 hour
                status = "warning"
            else:
                status = "ok"
            results.append({
                "name": f"heartbeat:{name}",
                "status": status,
                "detail": f"last heartbeat {int(age_seconds)}s ago",
                "last_timestamp": ts_str,
            })
        except (ValueError, TypeError):
            results.append({
                "name": f"heartbeat:{name}",
                "status": "warning",
                "detail": f"unparseable timestamp: {ts_str}",
            })

    return results


def _check_tasks(alfred_home: Path, agent: Optional[str] = None) -> List[Dict[str, Any]]:
    """Check task health across agents."""
    agents_dir = alfred_home / "agents"
    if not agents_dir.exists():
        return []

    results = []
    targets = [agent] if agent else [
        d.name for d in agents_dir.iterdir()
        if d.is_dir() and (d / "HEARTBEAT.md").exists()
    ]

    for name in targets:
        hb_file = agents_dir / name / "HEARTBEAT.md"
        if not hb_file.exists():
            continue
        try:
            content = hb_file.read_text(encoding="utf-8")
        except OSError:
            continue

        tasks = _parse_heartbeat_tasks(content)
        total = len(tasks)
        failed = sum(1 for t in tasks if t.get("state") == "failed")
        running = sum(1 for t in tasks if t.get("state") == "running")

        if total == 0:
            continue

        failure_rate = failed / total if total > 0 else 0
        if failure_rate > 0.5:
            status = "critical"
        elif failed > 0:
            status = "warning"
        else:
            status = "ok"

        results.append({
            "name": f"tasks:{name}",
            "status": status,
            "detail": f"{total} tasks, {failed} failed, {running} running",
            "total": total,
            "failed": failed,
        })

    return results


def _check_logs(alfred_home: Path) -> List[Dict[str, Any]]:
    """Check recent log error density."""
    results = []
    for source, filename in LOG_SOURCES.items():
        log_file = alfred_home / "logs" / filename
        if not log_file.exists():
            continue

        lines = _tail_file(log_file, 200)
        error_count = sum(1 for line in lines if "ERROR" in line)
        warning_count = sum(1 for line in lines if "WARNING" in line)

        if error_count > 20:
            status = "critical"
        elif error_count > 5:
            status = "warning"
        else:
            status = "ok"

        results.append({
            "name": f"logs:{source}",
            "status": status,
            "detail": f"{error_count} errors, {warning_count} warnings in last {len(lines)} lines",
            "error_count": error_count,
            "warning_count": warning_count,
        })

    return results


def cmd_diagnose(alfred_home: Path, agent: Optional[str] = None) -> Dict[str, Any]:
    """Run comprehensive diagnostics and return health report."""
    snapshot = _read_status_snapshot(alfred_home)

    checks: List[Dict[str, Any]] = []

    # 1. Daemon
    checks.append(_check_daemon(alfred_home, snapshot))

    # 2. Heartbeats
    checks.extend(_check_heartbeats(snapshot, agent))

    # 3. Tasks
    checks.extend(_check_tasks(alfred_home, agent))

    # 4. Logs
    checks.extend(_check_logs(alfred_home))

    # Compute overall health
    statuses = [c["status"] for c in checks]
    if "critical" in statuses:
        health = "unhealthy"
    elif "warning" in statuses:
        health = "degraded"
    else:
        health = "healthy"

    # Build recommendations
    recommendations = []
    for c in checks:
        if c["status"] == "critical":
            if "daemon" in c["name"]:
                recommendations.append("Start the daemon: bin/everbot start")
            elif "heartbeat" in c["name"]:
                recommendations.append(f"Investigate stale heartbeat for {c['name'].split(':')[-1]}")
            elif "tasks" in c["name"]:
                recommendations.append(f"Review failed tasks for {c['name'].split(':')[-1]}")
            elif "logs" in c["name"]:
                recommendations.append(f"Check {c['name'].split(':')[-1]} logs for errors")

    return {
        "ok": True,
        "command": "diagnose",
        "data": {
            "health": health,
            "checks": checks,
            "recommendations": recommendations,
            "summary": f"{health}: {len(checks)} checks, "
                       f"{statuses.count('critical')} critical, "
                       f"{statuses.count('warning')} warning, "
                       f"{statuses.count('ok')} ok",
        },
    }
