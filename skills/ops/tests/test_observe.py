"""Tests for observe.py — status, heartbeat, tasks, logs, metrics queries."""

from __future__ import annotations

import json

from observe import cmd_status, cmd_heartbeat, cmd_tasks, cmd_logs, cmd_metrics


class TestCmdStatus:
    def test_running_daemon(self, alfred_home, running_status):
        result = cmd_status(alfred_home)
        assert result["ok"] is True
        assert result["data"]["running"] is True
        assert result["data"]["pid"] is not None
        assert result["data"]["uptime_seconds"] is not None
        assert result["data"]["uptime_seconds"] >= 0
        assert result["data"]["project_root"] == "/opt/alfred"
        assert "test_agent" in result["data"]["agents"]

    def test_daemon_not_running_no_files(self, alfred_home):
        result = cmd_status(alfred_home)
        assert result["ok"] is False
        assert "not running" in result["error"]

    def test_stopped_daemon_with_status_file(self, alfred_home, stopped_status):
        result = cmd_status(alfred_home)
        assert result["ok"] is True
        assert result["data"]["running"] is False
        assert result["data"]["uptime_seconds"] is None

    def test_corrupted_status_file(self, alfred_home):
        (alfred_home / "everbot.status.json").write_text("not json", encoding="utf-8")
        result = cmd_status(alfred_home)
        assert result["ok"] is False


class TestCmdHeartbeat:
    def test_all_heartbeats(self, alfred_home, running_status):
        result = cmd_heartbeat(alfred_home)
        assert result["ok"] is True
        hb = result["data"]["heartbeats"]
        assert "test_agent" in hb
        assert "other_agent" in hb

    def test_specific_agent(self, alfred_home, running_status):
        result = cmd_heartbeat(alfred_home, agent="test_agent")
        assert result["ok"] is True
        assert result["data"]["agent"] == "test_agent"
        assert result["data"]["heartbeat"]["result_preview"] == "HEARTBEAT_OK"

    def test_unknown_agent(self, alfred_home, running_status):
        result = cmd_heartbeat(alfred_home, agent="nonexistent")
        assert result["ok"] is False
        assert "not found" in result["error"]

    def test_no_status_file(self, alfred_home):
        result = cmd_heartbeat(alfred_home)
        assert result["ok"] is False

    def test_agent_exists_no_heartbeat(self, alfred_home, running_status):
        # Remove heartbeat entry but keep agent in list
        status = json.loads((alfred_home / "everbot.status.json").read_text())
        del status["heartbeats"]["test_agent"]
        (alfred_home / "everbot.status.json").write_text(json.dumps(status))
        result = cmd_heartbeat(alfred_home, agent="test_agent")
        assert result["ok"] is True
        assert result["data"]["heartbeat"] is None


class TestCmdTasks:
    def test_tasks_with_heartbeat_md(self, alfred_home, sample_heartbeat_md):
        result = cmd_tasks(alfred_home, agent="test_agent")
        assert result["ok"] is True
        assert result["data"]["total"] == 2
        assert result["data"]["failed"] == 1
        tasks = result["data"]["tasks"]
        assert tasks[0]["id"] == "routine_abc123"
        assert tasks[0]["state"] == "pending"
        assert tasks[1]["state"] == "failed"
        assert tasks[1]["error_message"] == "timeout exceeded"

    def test_tasks_agent_not_found(self, alfred_home):
        result = cmd_tasks(alfred_home, agent="nonexistent")
        assert result["ok"] is False
        assert "not found" in result["error"]

    def test_tasks_no_heartbeat_md(self, alfred_home):
        (alfred_home / "agents" / "empty_agent").mkdir(parents=True)
        result = cmd_tasks(alfred_home, agent="empty_agent")
        assert result["ok"] is True
        assert result["data"]["tasks"] == []

    def test_tasks_empty_json_block(self, alfred_home):
        agent_dir = alfred_home / "agents" / "empty_tasks"
        agent_dir.mkdir(parents=True)
        (agent_dir / "HEARTBEAT.md").write_text(
            '# Heartbeat\n\n```json\n{"version": 2, "tasks": []}\n```\n'
        )
        result = cmd_tasks(alfred_home, agent="empty_tasks")
        assert result["ok"] is True
        assert result["data"]["tasks"] == []
        assert result["data"]["total"] == 0


class TestCmdLogs:
    def test_read_heartbeat_log(self, alfred_home, sample_logs):
        result = cmd_logs(alfred_home, source="heartbeat")
        assert result["ok"] is True
        assert result["data"]["count"] == 5
        assert result["data"]["error_count"] == 1
        assert result["data"]["warning_count"] == 1

    def test_read_daemon_log(self, alfred_home, sample_logs):
        result = cmd_logs(alfred_home, source="daemon")
        assert result["ok"] is True
        assert result["data"]["count"] == 4

    def test_filter_by_level(self, alfred_home, sample_logs):
        result = cmd_logs(alfred_home, source="heartbeat", level="ERROR")
        assert result["ok"] is True
        assert all("ERROR" in line for line in result["data"]["lines"])

    def test_filter_by_agent(self, alfred_home, sample_logs):
        result = cmd_logs(alfred_home, source="heartbeat", agent="test_agent")
        assert result["ok"] is True
        assert all("test_agent" in line for line in result["data"]["lines"])

    def test_tail_limit(self, alfred_home, sample_logs):
        result = cmd_logs(alfred_home, source="heartbeat", tail=2)
        assert result["ok"] is True
        assert result["data"]["count"] == 2

    def test_unknown_source(self, alfred_home):
        result = cmd_logs(alfred_home, source="unknown")
        assert result["ok"] is False
        assert "unknown log source" in result["error"]

    def test_log_file_not_found(self, alfred_home):
        result = cmd_logs(alfred_home, source="web")
        assert result["ok"] is False
        assert "not found" in result["error"]


class TestCmdMetrics:
    def test_metrics_available(self, alfred_home, running_status):
        result = cmd_metrics(alfred_home)
        assert result["ok"] is True
        assert result["data"]["metrics"]["session_count"] == 5
        assert result["data"]["metrics"]["total_llm_calls"] == 42

    def test_no_status_file(self, alfred_home):
        result = cmd_metrics(alfred_home)
        assert result["ok"] is False
