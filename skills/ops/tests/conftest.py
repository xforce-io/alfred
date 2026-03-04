"""Shared fixtures for ops skill tests."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

# Add scripts dir to path so tests can import observe/lifecycle/diagnose
_scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)


@pytest.fixture
def alfred_home(tmp_path: Path) -> Path:
    """Create a minimal ALFRED_HOME directory structure."""
    home = tmp_path / ".alfred"
    (home / "logs").mkdir(parents=True)
    (home / "agents").mkdir(parents=True)
    return home


@pytest.fixture
def running_status(alfred_home: Path) -> dict:
    """Write a status snapshot for a running daemon and return the snapshot data."""
    now = datetime.now()
    started = now - timedelta(hours=1)
    pid = os.getpid()  # Use current process PID so is_running check passes

    snapshot = {
        "status": "running",
        "project_root": "/opt/alfred",
        "pid": pid,
        "started_at": started.isoformat(),
        "timestamp": now.isoformat(),
        "agents": ["test_agent", "other_agent"],
        "heartbeats": {
            "test_agent": {
                "timestamp": (now - timedelta(minutes=5)).isoformat(),
                "result_preview": "HEARTBEAT_OK",
            },
            "other_agent": {
                "timestamp": (now - timedelta(hours=3)).isoformat(),
                "result_preview": "task completed",
            },
        },
        "task_states": {},
        "metrics": {
            "session_count": 5,
            "total_llm_calls": 42,
            "avg_latency_ms": 1200,
        },
    }

    (alfred_home / "everbot.status.json").write_text(
        json.dumps(snapshot, ensure_ascii=False), encoding="utf-8",
    )
    (alfred_home / "everbot.pid").write_text(str(pid), encoding="utf-8")

    return snapshot


@pytest.fixture
def stopped_status(alfred_home: Path) -> dict:
    """Write a status snapshot for a stopped daemon."""
    snapshot = {
        "status": "stopped",
        "project_root": "/opt/alfred",
        "pid": None,
        "started_at": None,
        "timestamp": datetime.now().isoformat(),
        "agents": [],
        "heartbeats": {},
        "task_states": {},
        "metrics": {},
    }
    (alfred_home / "everbot.status.json").write_text(
        json.dumps(snapshot, ensure_ascii=False), encoding="utf-8",
    )
    return snapshot


@pytest.fixture
def sample_heartbeat_md(alfred_home: Path) -> Path:
    """Create a sample HEARTBEAT.md with tasks."""
    agent_dir = alfred_home / "agents" / "test_agent"
    agent_dir.mkdir(parents=True, exist_ok=True)

    content = """# Heartbeat Tasks

```json
{
  "version": 2,
  "tasks": [
    {
      "id": "routine_abc123",
      "title": "Daily digest",
      "description": "Summarize key updates",
      "schedule": "1d",
      "timezone": "Asia/Shanghai",
      "execution_mode": "auto",
      "state": "pending",
      "last_run_at": "2026-03-03T10:00:00+08:00",
      "next_run_at": "2026-03-04T10:00:00+08:00",
      "timeout_seconds": 120,
      "retry": 0,
      "max_retry": 3,
      "error_message": null,
      "created_at": "2026-02-01T00:00:00+08:00"
    },
    {
      "id": "routine_def456",
      "title": "Health check",
      "description": "Check system health",
      "schedule": "0 */6 * * *",
      "timezone": "Asia/Shanghai",
      "execution_mode": "inline",
      "state": "failed",
      "last_run_at": "2026-03-04T06:00:00+08:00",
      "next_run_at": "2026-03-04T12:00:00+08:00",
      "timeout_seconds": 60,
      "retry": 3,
      "max_retry": 3,
      "error_message": "timeout exceeded",
      "created_at": "2026-02-15T00:00:00+08:00"
    }
  ]
}
```
"""
    hb_file = agent_dir / "HEARTBEAT.md"
    hb_file.write_text(content, encoding="utf-8")
    return hb_file


@pytest.fixture
def sample_logs(alfred_home: Path) -> None:
    """Create sample log files."""
    logs_dir = alfred_home / "logs"

    # heartbeat.log
    hb_lines = [
        "[2026-03-04T09:00:00] [test_agent] HEARTBEAT_OK\n",
        "[2026-03-04T09:30:00] [test_agent] HEARTBEAT_OK\n",
        "[2026-03-04T10:00:00] [other_agent] ERROR: timeout\n",
        "[2026-03-04T10:30:00] [test_agent] WARNING: slow response\n",
        "[2026-03-04T11:00:00] [test_agent] HEARTBEAT_OK\n",
    ]
    (logs_dir / "heartbeat.log").write_text("".join(hb_lines), encoding="utf-8")

    # daemon log
    daemon_lines = [
        "2026-03-04 09:00:00 [daemon] INFO: started\n",
        "2026-03-04 09:01:00 [daemon] ERROR: connection failed\n",
        "2026-03-04 09:02:00 [daemon] WARNING: retry attempt 1\n",
        "2026-03-04 09:03:00 [daemon] INFO: reconnected\n",
    ]
    (logs_dir / "everbot.out").write_text("".join(daemon_lines), encoding="utf-8")
