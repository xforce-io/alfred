"""End-to-end tests for HeartbeatRunner → CronExecutor → RoutineManager pipeline.

These tests exercise the full chain without mocking internal CronExecutor methods,
verifying that task execution results flow correctly through the delegation layer
and that task state is properly persisted to disk via RoutineManager.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from contextlib import contextmanager

import pytest

from src.everbot.core.runtime.heartbeat import HeartbeatRunner
from src.everbot.core.tasks.task_manager import TaskState


# ── Helpers ──────────────────────────────────────────────────


def _build_structured_md(tasks: list[dict]) -> str:
    task_list = {"version": 2, "tasks": tasks}
    return f"# HEARTBEAT\n\n## Tasks\n\n```json\n{json.dumps(task_list, indent=2)}\n```\n"


def _make_session_manager():
    sm = SimpleNamespace(
        get_primary_session_id=lambda agent_name: f"web_session_{agent_name}",
        get_heartbeat_session_id=lambda agent_name: f"heartbeat_session_{agent_name}",
        acquire_session=AsyncMock(return_value=True),
        release_session=MagicMock(),
        inject_history_message=AsyncMock(return_value=True),
        deposit_mailbox_event=AsyncMock(return_value=True),
    )

    @contextmanager
    def _file_lock(session_id, blocking=False):
        yield True
    sm.file_lock = _file_lock
    return sm


def _make_runner(tmp_path: Path, **overrides) -> HeartbeatRunner:
    sm = overrides.pop("session_manager", _make_session_manager())
    defaults = {
        "agent_name": "test_agent",
        "workspace_path": tmp_path,
        "session_manager": sm,
        "agent_factory": AsyncMock(),
        "interval_minutes": 1,
        "active_hours": (0, 24),
        "max_retries": 3,
        "on_result": None,
    }
    defaults.update(overrides)
    return HeartbeatRunner(**defaults)


def _seed_heartbeat(tmp_path: Path, tasks: list[dict]) -> str:
    content = _build_structured_md(tasks)
    (tmp_path / "HEARTBEAT.md").write_text(content, encoding="utf-8")
    return content


def _reload_tasks(tmp_path: Path) -> list[dict]:
    """Re-read HEARTBEAT.md from disk and parse task list."""
    content = (tmp_path / "HEARTBEAT.md").read_text(encoding="utf-8")
    match = content.split("```json")
    if len(match) < 2:
        return []
    json_block = match[1].split("```")[0]
    data = json.loads(json_block)
    return data.get("tasks", [])


# ── E2E: deterministic task (time reminder) ──────────────────


class TestE2EDeterministicTask:
    """Full pipeline: HeartbeatRunner → CronExecutor → deterministic execution → disk persistence."""

    @pytest.mark.asyncio
    async def test_time_reminder_executes_and_persists(self, tmp_path: Path, monkeypatch):
        """A time_reminder task runs without LLM, returns result, and persists state to disk."""
        now = datetime.now(timezone.utc)
        past = (now - timedelta(hours=1)).isoformat()
        tasks = [{
            "id": "tr_1",
            "title": "time_reminder_e2e",
            "description": "报时",
            "schedule": "1h",
            "state": "pending",
            "enabled": True,
            "next_run_at": past,
            "execution_mode": "inline",
            "timeout_seconds": 30,
        }]
        hb_content = _seed_heartbeat(tmp_path, tasks)

        runner = _make_runner(tmp_path)
        runner._read_heartbeat_md()

        fake_context = SimpleNamespace(
            set_variable=MagicMock(), set_session_id=MagicMock(),
            get_var_value=MagicMock(return_value=""),
        )
        fake_agent = SimpleNamespace(executor=SimpleNamespace(context=fake_context))

        # Mock only the outermost runner methods (event recording etc)
        monkeypatch.setattr(runner, "_record_timeline_event", MagicMock())
        monkeypatch.setattr(runner, "_record_runtime_metric", MagicMock())
        monkeypatch.setattr(runner, "_write_heartbeat_event", MagicMock())
        monkeypatch.setattr(runner, "_write_heartbeat_file", MagicMock())
        monkeypatch.setattr(runner._cron, "_write_event", MagicMock())

        run_agent_mock = AsyncMock()
        monkeypatch.setattr(runner, "_run_agent", run_agent_mock)

        result = await runner._execute_structured_tasks(fake_agent, hb_content, "e2e_run_1")

        # Should contain time info (deterministic result)
        assert "当前时间" in result or result == "HEARTBEAT_OK"
        # LLM should NOT have been called
        run_agent_mock.assert_not_called()

        # Verify task state persisted to disk
        disk_tasks = _reload_tasks(tmp_path)
        assert len(disk_tasks) == 1
        task = disk_tasks[0]
        assert task["state"] == "pending"  # re-armed (has schedule)
        assert task["last_run_at"] is not None


class TestE2ELLMInlineTask:
    """Full pipeline: HeartbeatRunner → CronExecutor → LLM inline execution → disk persistence."""

    @pytest.mark.asyncio
    async def test_llm_task_executes_and_persists(self, tmp_path: Path, monkeypatch):
        """An LLM inline task runs agent, returns result, and persists state to disk."""
        now = datetime.now(timezone.utc)
        past = (now - timedelta(hours=1)).isoformat()
        tasks = [{
            "id": "llm_1",
            "title": "LLM task e2e",
            "description": "analyze something",
            "schedule": "1h",
            "state": "pending",
            "enabled": True,
            "next_run_at": past,
            "execution_mode": "inline",
            "timeout_seconds": 60,
        }]
        hb_content = _seed_heartbeat(tmp_path, tasks)

        runner = _make_runner(tmp_path)
        runner._read_heartbeat_md()

        fake_context = SimpleNamespace(
            set_variable=MagicMock(), set_session_id=MagicMock(),
            get_var_value=MagicMock(return_value=""),
        )
        fake_agent = SimpleNamespace(executor=SimpleNamespace(context=fake_context))

        monkeypatch.setattr(runner, "_record_timeline_event", MagicMock())
        monkeypatch.setattr(runner, "_record_runtime_metric", MagicMock())
        monkeypatch.setattr(runner, "_write_heartbeat_event", MagicMock())
        monkeypatch.setattr(runner, "_write_heartbeat_file", MagicMock())
        monkeypatch.setattr(runner._cron, "_write_event", MagicMock())

        # Mock LLM agent to return a result
        run_agent_mock = AsyncMock(return_value="Analysis complete: all metrics normal")
        monkeypatch.setattr(runner, "_run_agent", run_agent_mock)
        monkeypatch.setattr(
            runner, "_inject_heartbeat_context",
            AsyncMock(return_value="Please analyze something"),
        )

        result = await runner._execute_structured_tasks(fake_agent, hb_content, "e2e_run_2")

        # Result should contain LLM output
        assert "Analysis complete" in result
        # LLM should have been called
        run_agent_mock.assert_called_once()

        # Verify task state persisted to disk
        disk_tasks = _reload_tasks(tmp_path)
        assert len(disk_tasks) == 1
        task = disk_tasks[0]
        assert task["state"] == "pending"  # re-armed (has schedule)
        assert task["last_run_at"] is not None


class TestE2ETimeoutTask:
    """Full pipeline: HeartbeatRunner → CronExecutor → timeout → error persisted."""

    @pytest.mark.asyncio
    async def test_timeout_persists_error_to_disk(self, tmp_path: Path, monkeypatch):
        """A timed-out inline task persists error state to HEARTBEAT.md."""
        now = datetime.now(timezone.utc)
        past = (now - timedelta(hours=1)).isoformat()
        tasks = [{
            "id": "timeout_e2e",
            "title": "Slow task",
            "description": "will timeout",
            "schedule": "1h",
            "state": "pending",
            "enabled": True,
            "next_run_at": past,
            "execution_mode": "inline",
            "timeout_seconds": 1,
        }]
        hb_content = _seed_heartbeat(tmp_path, tasks)

        runner = _make_runner(tmp_path)
        runner._read_heartbeat_md()

        fake_context = SimpleNamespace(
            set_variable=MagicMock(), set_session_id=MagicMock(),
            get_var_value=MagicMock(return_value=""),
        )
        fake_agent = SimpleNamespace(executor=SimpleNamespace(context=fake_context))

        async def _slow_run(*args, **kwargs):
            await asyncio.sleep(10)
            return "never"

        monkeypatch.setattr(runner, "_record_timeline_event", MagicMock())
        monkeypatch.setattr(runner, "_record_runtime_metric", MagicMock())
        monkeypatch.setattr(runner, "_write_heartbeat_event", MagicMock())
        monkeypatch.setattr(runner, "_write_heartbeat_file", MagicMock())
        monkeypatch.setattr(runner._cron, "_write_event", MagicMock())
        monkeypatch.setattr(runner, "_run_agent", _slow_run)
        monkeypatch.setattr(
            runner, "_inject_heartbeat_context",
            AsyncMock(return_value="execute something"),
        )

        result = await runner._execute_structured_tasks(fake_agent, hb_content, "e2e_run_3")

        assert result == "HEARTBEAT_OK"

        # Verify error persisted to disk
        disk_tasks = _reload_tasks(tmp_path)
        assert len(disk_tasks) == 1
        task = disk_tasks[0]
        assert task["error_message"] == "timeout"
        assert task["last_run_at"] is not None


class TestE2EMultipleTaskOrdering:
    """Full pipeline: multiple tasks with different types execute in order."""

    @pytest.mark.asyncio
    async def test_deterministic_and_llm_tasks_both_execute(self, tmp_path: Path, monkeypatch):
        """Two due tasks — one deterministic, one LLM — both execute and persist."""
        now = datetime.now(timezone.utc)
        past = (now - timedelta(hours=1)).isoformat()
        tasks = [
            {
                "id": "det_1",
                "title": "time_reminder_multi",
                "description": "报时",
                "schedule": "1h",
                "state": "pending",
                "enabled": True,
                "next_run_at": past,
                "execution_mode": "inline",
                "timeout_seconds": 30,
            },
            {
                "id": "llm_2",
                "title": "LLM review",
                "description": "review something",
                "schedule": "1h",
                "state": "pending",
                "enabled": True,
                "next_run_at": past,
                "execution_mode": "inline",
                "timeout_seconds": 60,
            },
        ]
        hb_content = _seed_heartbeat(tmp_path, tasks)

        runner = _make_runner(tmp_path)
        runner._read_heartbeat_md()

        fake_context = SimpleNamespace(
            set_variable=MagicMock(), set_session_id=MagicMock(),
            get_var_value=MagicMock(return_value=""),
        )
        fake_agent = SimpleNamespace(executor=SimpleNamespace(context=fake_context))

        monkeypatch.setattr(runner, "_record_timeline_event", MagicMock())
        monkeypatch.setattr(runner, "_record_runtime_metric", MagicMock())
        monkeypatch.setattr(runner, "_write_heartbeat_event", MagicMock())
        monkeypatch.setattr(runner, "_write_heartbeat_file", MagicMock())
        monkeypatch.setattr(runner._cron, "_write_event", MagicMock())
        monkeypatch.setattr(
            runner, "_run_agent",
            AsyncMock(return_value="Review done: looks good"),
        )
        monkeypatch.setattr(
            runner, "_inject_heartbeat_context",
            AsyncMock(return_value="please review"),
        )

        result = await runner._execute_structured_tasks(fake_agent, hb_content, "e2e_run_4")

        # Result should include both outputs
        assert "当前时间" in result
        assert "Review done" in result

        # Both tasks should be re-armed
        disk_tasks = _reload_tasks(tmp_path)
        assert len(disk_tasks) == 2
        for task in disk_tasks:
            assert task["state"] == "pending"
            assert task["last_run_at"] is not None


class TestE2EIsolatedTaskClaim:
    """Full pipeline: claim + execute isolated task through HeartbeatRunner forwarding."""

    @pytest.mark.asyncio
    async def test_claim_and_execute_isolated_task(self, tmp_path: Path, monkeypatch):
        """Claim an isolated task through HeartbeatRunner, execute via CronExecutor."""
        now = datetime.now(timezone.utc)
        past = (now - timedelta(hours=1)).isoformat()
        tasks = [{
            "id": "iso_e2e",
            "title": "Isolated e2e",
            "description": "isolated work",
            "schedule": "1d",
            "state": "pending",
            "enabled": True,
            "next_run_at": past,
            "execution_mode": "isolated",
            "timeout_seconds": 120,
        }]
        _seed_heartbeat(tmp_path, tasks)

        sm = _make_session_manager()
        runner = _make_runner(tmp_path, session_manager=sm)

        # Step 1: List due isolated tasks
        due = runner.list_due_isolated_tasks(now=now)
        assert len(due) == 1
        assert due[0]["id"] == "iso_e2e"

        # Step 2: Claim
        claimed = await runner.claim_isolated_task("iso_e2e", now=now)
        assert claimed is True

        # Step 3: Verify task is now running on disk
        disk_tasks = _reload_tasks(tmp_path)
        assert disk_tasks[0]["state"] == "running"

        # Step 4: Execute (mock the actual LLM run)
        monkeypatch.setattr(
            runner._cron, "_run_isolated_task",
            AsyncMock(return_value="isolated work done"),
        )

        snapshot = due[0]
        await runner.execute_isolated_claimed_task(snapshot)

        # Step 5: Verify final state on disk
        disk_tasks = _reload_tasks(tmp_path)
        assert len(disk_tasks) == 1
        task = disk_tasks[0]
        assert task["state"] == "pending"  # re-armed (has schedule)
        assert task["last_run_at"] is not None


class TestE2ERecoverStuckTask:
    """Full pipeline: stuck running task gets recovered through list_due call."""

    def test_stuck_task_recovered_via_list_due(self, tmp_path: Path):
        """Calling list_due_inline_tasks auto-recovers stuck tasks."""
        now = datetime.now(timezone.utc)
        long_ago = (now - timedelta(minutes=30)).isoformat()
        tasks = [{
            "id": "stuck_e2e",
            "title": "Stuck Task",
            "state": "running",
            "schedule": "1d",
            "enabled": True,
            "execution_mode": "inline",
            "timeout_seconds": 600,
            "last_run_at": long_ago,
        }]
        _seed_heartbeat(tmp_path, tasks)

        runner = _make_runner(tmp_path)

        # list_due triggers recovery
        runner.list_due_inline_tasks(now=now)

        # Verify task was recovered to pending on disk
        disk_tasks = _reload_tasks(tmp_path)
        assert len(disk_tasks) == 1
        assert disk_tasks[0]["state"] == "pending"
        assert disk_tasks[0]["error_message"] == "recovered: stuck in running state"
