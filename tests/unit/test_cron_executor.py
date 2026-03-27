"""Unit tests for CronExecutor task execution engine."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.everbot.core.runtime.cron import CronExecutor, CronTickResult, TaskResult
from src.everbot.core.runtime.cron_delivery import CronDelivery
from src.everbot.core.tasks.execution_gate import GateVerdict
from src.everbot.core.tasks.routine_manager import RoutineManager
from src.everbot.core.tasks.task_manager import TaskState


def _make_executor(tmp_path: Path, **overrides) -> CronExecutor:
    sm = AsyncMock()
    sm.get_primary_session_id.return_value = "web_session_test"
    sm.get_heartbeat_session_id.return_value = "heartbeat_session_test"
    delivery = CronDelivery(
        session_manager=sm,
        primary_session_id="web_session_test",
        heartbeat_session_id="heartbeat_session_test",
        agent_name="test_agent",
        realtime_push=False,
    )
    defaults = dict(
        agent_name="test_agent",
        workspace_path=tmp_path,
        session_manager=sm,
        agent_factory=AsyncMock(),
        routine_manager=RoutineManager(tmp_path),
        delivery=delivery,
    )
    defaults.update(overrides)
    return CronExecutor(**defaults)


def _seed_task(tmp_path: Path, **task_overrides) -> RoutineManager:
    """Seed HEARTBEAT.md with one task and return the manager."""
    mgr = RoutineManager(tmp_path)
    defaults = dict(
        title="Test task",
        schedule="1h",
        next_run_at="2026-03-01T11:00:00+00:00",
        now=datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc),
    )
    defaults.update(task_overrides)
    mgr.add_routine(**defaults)
    return mgr


class TestCronTickResult:
    def test_user_visible_output_no_results(self):
        r = CronTickResult()
        assert r.user_visible_output == "HEARTBEAT_OK"

    def test_user_visible_output_with_done_results(self):
        r = CronTickResult(
            executed=2,
            results=[
                TaskResult(task_id="a", status="done", output="result A"),
                TaskResult(task_id="b", status="failed", output="err"),
                TaskResult(task_id="c", status="done", output="result C"),
            ],
        )
        assert "result A" in r.user_visible_output
        assert "result C" in r.user_visible_output
        assert "err" not in r.user_visible_output


class TestDeterministicExecution:
    @pytest.mark.asyncio
    async def test_time_reminder_executes_without_llm(self, tmp_path):
        mgr = _seed_task(tmp_path, title="time_reminder_test", description="报时")
        executor = _make_executor(tmp_path, routine_manager=mgr)

        task_list = mgr.load_task_list()
        run_agent = AsyncMock()
        inject_context = AsyncMock()

        result = await executor.tick(
            task_list,
            run_agent=run_agent,
            inject_context=inject_context,
            run_id="test_run",
        )

        assert result.executed == 1
        assert result.results[0].execution_path == "deterministic"
        assert "当前时间" in result.results[0].output
        run_agent.assert_not_called()


class TestSkillExecution:
    @pytest.mark.asyncio
    async def test_skill_skipped_when_gate_denies(self, tmp_path):
        mgr = _seed_task(
            tmp_path,
            title="Skill task",
            job="memory-review",
            scanner="session",
        )
        executor = _make_executor(tmp_path, routine_manager=mgr)

        task_list = mgr.load_task_list()

        with patch.object(
            type(executor), '_get_scanner',
            return_value=None,  # No scanner → gate will use default behavior
        ):
            from src.everbot.core.tasks.execution_gate import GateVerdict
            with patch(
                "src.everbot.core.runtime.cron.TaskExecutionGate.check",
                return_value=GateVerdict(allowed=False, skip_reason="no_changes"),
            ):
                result = await executor.tick(
                    task_list,
                    run_agent=AsyncMock(),
                    inject_context=AsyncMock(),
                    run_id="test_run",
                )

        assert result.skipped == 1
        assert result.results[0].status == "skipped"
        assert result.results[0].error == "no_changes"


class TestLLMInlineExecution:
    @pytest.mark.asyncio
    async def test_llm_task_calls_agent(self, tmp_path):
        mgr = _seed_task(tmp_path, title="LLM task", description="do something")
        executor = _make_executor(tmp_path, routine_manager=mgr)

        task_list = mgr.load_task_list()
        run_agent = AsyncMock(return_value="Task completed successfully")
        inject_context = AsyncMock(return_value="injected prompt")

        result = await executor.tick(
            task_list,
            run_agent=run_agent,
            inject_context=inject_context,
            agent=MagicMock(),
            heartbeat_content="content",
            run_id="test_run",
        )

        assert result.executed == 1
        assert result.results[0].execution_path == "llm_inline"
        assert result.results[0].output == "Task completed successfully"
        run_agent.assert_called_once()

    @pytest.mark.asyncio
    async def test_llm_task_timeout(self, tmp_path):
        mgr = RoutineManager(tmp_path)
        mgr.add_routine(
            title="Slow task",
            schedule="1h",
            next_run_at="2026-03-01T11:00:00+00:00",
            timeout_seconds=1,
            now=datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc),
        )
        executor = _make_executor(tmp_path, routine_manager=mgr)

        async def slow_agent(*args, **kwargs):
            await asyncio.sleep(10)
            return "never"

        task_list = mgr.load_task_list()
        result = await executor.tick(
            task_list,
            run_agent=slow_agent,
            inject_context=AsyncMock(return_value="prompt"),
            agent=MagicMock(),
            heartbeat_content="content",
            run_id="test_run",
        )

        assert result.failed == 1
        assert result.results[0].status == "timeout"


class TestTaskListing:
    def test_list_due_inline_tasks(self, tmp_path):
        mgr = _seed_task(tmp_path, title="Inline task", execution_mode="inline")
        executor = _make_executor(tmp_path, routine_manager=mgr)
        now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
        tasks = executor.list_due_inline_tasks(now=now)
        assert len(tasks) == 1
        assert tasks[0]["title"] == "Inline task"

    def test_list_due_isolated_tasks(self, tmp_path):
        mgr = _seed_task(
            tmp_path, title="Isolated task",
            execution_mode="isolated",
            description="x" * 300,
            timeout_seconds=300,
        )
        executor = _make_executor(tmp_path, routine_manager=mgr)
        now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
        tasks = executor.list_due_isolated_tasks(now=now)
        assert len(tasks) == 1
        assert tasks[0]["title"] == "Isolated task"

    def test_list_empty_when_no_heartbeat(self, tmp_path):
        executor = _make_executor(tmp_path)
        assert executor.list_due_inline_tasks() == []
        assert executor.list_due_isolated_tasks() == []


class TestStateFlush:
    @pytest.mark.asyncio
    async def test_task_state_persisted_after_execution(self, tmp_path):
        mgr = _seed_task(tmp_path, title="time_reminder_persist", description="报时")
        executor = _make_executor(tmp_path, routine_manager=mgr)

        task_list = mgr.load_task_list()
        await executor.tick(
            task_list,
            run_agent=AsyncMock(),
            inject_context=AsyncMock(),
            run_id="test_run",
        )

        # Re-read from disk — task should be re-armed (scheduled, so PENDING with new next_run_at)
        fresh = mgr.load_task_list()
        task = fresh.tasks[0]
        assert task.state == TaskState.PENDING.value
        assert task.last_run_at is not None


class TestJobImportError:
    @pytest.mark.asyncio
    async def test_import_failure_gives_actionable_error(self, tmp_path):
        mgr = _seed_task(
            tmp_path,
            title="Bad import job",
            job="health-check",
            scanner="session",
        )
        executor = _make_executor(tmp_path, routine_manager=mgr)
        task_list = mgr.load_task_list()

        with patch(
            "src.everbot.core.runtime.cron.TaskExecutionGate.check",
            return_value=GateVerdict(allowed=True),
        ), patch(
            "importlib.import_module",
            side_effect=ModuleNotFoundError("No module named 'everbot'"),
        ):
            result = await executor.tick(
                task_list,
                run_agent=AsyncMock(),
                inject_context=AsyncMock(),
                include_isolated=False,
            )

        assert result.failed == 1
        assert "Cannot import job module" in result.results[0].error
        assert "project root" in result.results[0].error
