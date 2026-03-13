"""E2E tests for ops skill against a real Alfred environment."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Tuple

import pytest


# ───────────────────────────────────────────────────────────────────
# Read-only query tests (safe — no state mutation)
# ───────────────────────────────────────────────────────────────────


class TestEverbotCLI:
    """Verify ``bin/everbot status`` works end-to-end."""

    def test_everbot_status(
        self, run_everbot: Callable[..., Tuple[int, str, str]],
    ) -> None:
        rc, stdout, stderr = run_everbot(["status"])
        assert rc == 0, f"everbot status failed (rc={rc}): {stderr}"
        assert stdout.strip(), "everbot status returned empty output"


class TestOpsStatus:
    def test_ops_status(self, run_ops: Callable[..., Dict[str, Any]]) -> None:
        result = run_ops(["status"])
        assert result["ok"] is True, f"ops status failed: {result}"
        assert "data" in result
        assert isinstance(result["data"]["running"], bool)


class TestOpsHeartbeat:
    def test_ops_heartbeat(self, run_ops: Callable[..., Dict[str, Any]]) -> None:
        result = run_ops(["heartbeat"])
        assert result["ok"] is True, f"ops heartbeat failed: {result}"
        assert "data" in result
        assert isinstance(result["data"]["heartbeats"], dict)


class TestOpsTasks:
    def test_ops_tasks(
        self,
        run_ops: Callable[..., Dict[str, Any]],
        alfred_home: Path,
    ) -> None:
        # Discover an agent name from the environment
        agents_dir = alfred_home / "agents"
        if not agents_dir.exists():
            pytest.skip("No agents directory found")
        agents = [d.name for d in agents_dir.iterdir() if d.is_dir()]
        if not agents:
            pytest.skip("No agents configured in environment")
        agent = agents[0]

        result = run_ops(["tasks", "--agent", agent])
        assert result["ok"] is True, f"ops tasks failed: {result}"
        assert "data" in result
        assert isinstance(result["data"]["tasks"], list)


class TestOpsLogs:
    def test_ops_logs_heartbeat(self, run_ops: Callable[..., Dict[str, Any]]) -> None:
        result = run_ops(["logs", "--source", "heartbeat", "--tail", "10"])
        assert result["ok"] is True, f"ops logs heartbeat failed: {result}"
        assert "data" in result
        assert isinstance(result["data"]["lines"], list)

    def test_ops_logs_daemon(self, run_ops: Callable[..., Dict[str, Any]]) -> None:
        result = run_ops(["logs", "--source", "daemon", "--tail", "10"])
        assert result["ok"] is True, f"ops logs daemon failed: {result}"


class TestOpsMetrics:
    def test_ops_metrics(self, run_ops: Callable[..., Dict[str, Any]]) -> None:
        result = run_ops(["metrics"])
        assert result["ok"] is True, f"ops metrics failed: {result}"
        assert "data" in result
        assert isinstance(result["data"]["metrics"], dict)


class TestOpsDiagnose:
    def test_ops_diagnose(self, run_ops: Callable[..., Dict[str, Any]]) -> None:
        result = run_ops(["diagnose"])
        assert result["ok"] is True, f"ops diagnose failed: {result}"
        assert "data" in result
        assert result["data"]["health"] in ("healthy", "degraded", "unhealthy")


# ───────────────────────────────────────────────────────────────────
# Lifecycle tests (destructive — require --run-destructive)
# ───────────────────────────────────────────────────────────────────


@pytest.mark.destructive
class TestOpsLifecycle:
    """Tests that change daemon state. Skipped unless ``--run-destructive``."""

    def test_ops_stop_start(self, run_ops: Callable[..., Dict[str, Any]]) -> None:
        # Stop
        stop_result = run_ops(["stop"])
        assert stop_result["ok"] is True, f"ops stop failed: {stop_result}"

        # Verify stopped
        status_after_stop = run_ops(["status"])
        assert status_after_stop["ok"] is True
        assert status_after_stop["data"]["running"] is False

        # Start
        start_result = run_ops(["start"])
        assert start_result["ok"] is True, f"ops start failed: {start_result}"

        # Verify running
        status_after_start = run_ops(["status"])
        assert status_after_start["ok"] is True
        assert status_after_start["data"]["running"] is True

    def test_ops_restart(self, run_ops: Callable[..., Dict[str, Any]]) -> None:
        restart_result = run_ops(["restart"])
        assert restart_result["ok"] is True, f"ops restart failed: {restart_result}"

        status = run_ops(["status"])
        assert status["ok"] is True
        assert status["data"]["running"] is True
