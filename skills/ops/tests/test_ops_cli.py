"""Integration tests for ops_cli.py dispatcher."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

OPS_CLI = str(Path(__file__).resolve().parent.parent / "scripts" / "ops_cli.py")


def _run_ops(alfred_home: Path, *args: str) -> dict:
    """Run ops_cli.py as subprocess and return parsed JSON output."""
    cmd = [sys.executable, OPS_CLI, "--alfred-home", str(alfred_home)] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        pytest.fail(f"Non-JSON output: stdout={result.stdout!r}, stderr={result.stderr!r}")


class TestStatusIntegration:
    def test_status_running(self, alfred_home, running_status):
        out = _run_ops(alfred_home, "status")
        assert out["ok"] is True
        assert out["data"]["running"] is True

    def test_status_no_daemon(self, alfred_home):
        out = _run_ops(alfred_home, "status")
        assert out["ok"] is False


class TestHeartbeatIntegration:
    def test_heartbeat_all(self, alfred_home, running_status):
        out = _run_ops(alfred_home, "heartbeat")
        assert out["ok"] is True
        assert "heartbeats" in out["data"]

    def test_heartbeat_agent(self, alfred_home, running_status):
        out = _run_ops(alfred_home, "heartbeat", "--agent", "test_agent")
        assert out["ok"] is True
        assert out["data"]["agent"] == "test_agent"


class TestTasksIntegration:
    def test_tasks(self, alfred_home, sample_heartbeat_md):
        out = _run_ops(alfred_home, "tasks", "--agent", "test_agent")
        assert out["ok"] is True
        assert out["data"]["total"] == 2


class TestLogsIntegration:
    def test_logs(self, alfred_home, sample_logs):
        out = _run_ops(alfred_home, "logs", "--source", "heartbeat", "--tail", "3")
        assert out["ok"] is True
        assert out["data"]["count"] == 3

    def test_logs_level_filter(self, alfred_home, sample_logs):
        out = _run_ops(alfred_home, "logs", "--source", "heartbeat", "--level", "ERROR")
        assert out["ok"] is True
        assert all("ERROR" in line for line in out["data"]["lines"])


class TestMetricsIntegration:
    def test_metrics(self, alfred_home, running_status):
        out = _run_ops(alfred_home, "metrics")
        assert out["ok"] is True
        assert "metrics" in out["data"]


class TestDiagnoseIntegration:
    def test_diagnose(self, alfred_home, running_status, sample_heartbeat_md, sample_logs):
        out = _run_ops(alfred_home, "diagnose")
        assert out["ok"] is True
        assert out["data"]["health"] in ("healthy", "degraded", "unhealthy")
        assert "checks" in out["data"]
