"""Tests for diagnose.py — comprehensive health diagnostics."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from diagnose import cmd_diagnose


class TestCmdDiagnose:
    def test_healthy_system(self, alfred_home, running_status, sample_heartbeat_md, sample_logs):
        # Update heartbeats to be fresh (test_agent is already fresh from fixture)
        status = json.loads((alfred_home / "everbot.status.json").read_text())
        now = datetime.now()
        status["heartbeats"]["other_agent"]["timestamp"] = (now - timedelta(minutes=10)).isoformat()
        (alfred_home / "everbot.status.json").write_text(json.dumps(status))

        # Fix the failed task so all tasks are healthy
        hb = (alfred_home / "agents" / "test_agent" / "HEARTBEAT.md").read_text()
        hb = hb.replace('"state": "failed"', '"state": "pending"')
        (alfred_home / "agents" / "test_agent" / "HEARTBEAT.md").write_text(hb)

        result = cmd_diagnose(alfred_home)
        assert result["ok"] is True
        assert result["data"]["health"] in ("healthy", "degraded")

    def test_unhealthy_daemon_stopped(self, alfred_home, stopped_status):
        result = cmd_diagnose(alfred_home)
        assert result["ok"] is True
        assert result["data"]["health"] == "unhealthy"
        # Should recommend starting daemon
        assert any("start" in r.lower() or "Start" in r for r in result["data"]["recommendations"])

    def test_degraded_stale_heartbeat(self, alfred_home, running_status):
        # other_agent heartbeat is 3 hours old (from fixture) → should be critical
        result = cmd_diagnose(alfred_home)
        assert result["ok"] is True
        checks = result["data"]["checks"]
        hb_checks = [c for c in checks if "heartbeat:other_agent" in c["name"]]
        assert len(hb_checks) == 1
        assert hb_checks[0]["status"] == "critical"

    def test_task_failures_detected(self, alfred_home, running_status, sample_heartbeat_md):
        result = cmd_diagnose(alfred_home)
        assert result["ok"] is True
        task_checks = [c for c in result["data"]["checks"] if "tasks:" in c["name"]]
        assert len(task_checks) >= 1
        assert task_checks[0]["failed"] == 1

    def test_agent_filter(self, alfred_home, running_status, sample_heartbeat_md):
        result = cmd_diagnose(alfred_home, agent="test_agent")
        assert result["ok"] is True
        # Should only have checks for test_agent (plus daemon and logs)
        hb_checks = [c for c in result["data"]["checks"] if "heartbeat:" in c["name"]]
        assert all("test_agent" in c["name"] for c in hb_checks)

    def test_no_status_file(self, alfred_home):
        result = cmd_diagnose(alfred_home)
        assert result["ok"] is True
        assert result["data"]["health"] == "unhealthy"

    def test_summary_format(self, alfred_home, running_status):
        result = cmd_diagnose(alfred_home)
        summary = result["data"]["summary"]
        assert "critical" in summary or "warning" in summary or "ok" in summary
