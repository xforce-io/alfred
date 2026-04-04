"""Unit tests for CronExecutor task execution engine."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.everbot.core.runtime.cron import CronExecutor, CronTickResult, TaskResult
from src.everbot.core.runtime.cron_delivery import CronDelivery
from src.everbot.core.tasks.execution_gate import GateVerdict
from src.everbot.core.tasks.routine_manager import RoutineManager
from src.everbot.core.tasks.task_manager import TaskState
from src.everbot.core.jobs.llm_errors import LLMTransientError, LLMConfigError


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

    @pytest.mark.asyncio
    async def test_gate_skip_rearms_next_run_at(self, tmp_path):
        """When gate skips an inline task, next_run_at must advance so the
        scheduler doesn't re-trigger it every tick (spin prevention)."""
        old_next_run = "2026-03-01T11:00:00+00:00"
        mgr = _seed_task(
            tmp_path,
            title="Gated task",
            job="memory-review",
            scanner="session",
            schedule="2h",
            min_execution_interval="2h",
            next_run_at=old_next_run,
        )
        executor = _make_executor(tmp_path, routine_manager=mgr)
        task_list = mgr.load_task_list()

        with patch(
            "src.everbot.core.runtime.cron.TaskExecutionGate.check",
            return_value=GateVerdict(allowed=False, skip_reason="no_changes"),
        ):
            await executor.tick(
                task_list,
                run_agent=AsyncMock(),
                inject_context=AsyncMock(),
                run_id="test_run",
            )

        # Reload and verify next_run_at was advanced
        refreshed = mgr.load_task_list()
        task = refreshed.tasks[0]
        assert task.next_run_at != old_next_run, \
            "next_run_at should advance after gate skip to prevent scheduler spin"


class TestJobGateAfterClaim:
    """Regression: claim_task sets last_run_at=now BEFORE gate check runs.

    When a job task has min_execution_interval, the gate check reads
    task.last_run_at. If claim_task already set it to now, the interval
    check sees now >= now + interval → always False → perpetual skip.
    """

    @pytest.mark.asyncio
    async def test_inline_job_with_interval_executes_when_due(self, tmp_path):
        """A due inline job task with min_execution_interval should execute,
        not be skipped by interval_not_met after claim sets last_run_at."""
        mgr = _seed_task(
            tmp_path,
            title="Skill Evaluate",
            job="skill-evaluate",
            scanner=None,
            min_execution_interval="2h",
            next_run_at="2026-03-01T10:00:00+00:00",
            now=datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc),
        )
        # Set last_run_at to 4 hours ago (well past the 2h interval)
        task_list = mgr.load_task_list()
        task_list.tasks[0].last_run_at = datetime(
            2026, 3, 1, 8, 0, tzinfo=timezone.utc,
        ).isoformat()
        mgr.flush(task_list)

        executor = _make_executor(tmp_path, routine_manager=mgr)
        task_list = mgr.load_task_list()

        with patch.object(
            executor, '_invoke_job',
            new_callable=AsyncMock,
            return_value="Evaluated 3/3 skills",
        ):
            result = await executor.tick(
                task_list,
                run_agent=AsyncMock(),
                inject_context=AsyncMock(),
                run_id="test_run",
            )

        # The job should have executed, NOT been skipped
        assert result.executed == 1
        assert result.results[0].status == "done"
        assert result.results[0].execution_path == "skill"

    @pytest.mark.asyncio
    async def test_inline_job_with_interval_skips_when_recent(self, tmp_path):
        """A job whose last_run_at is recent should be skipped by the gate."""
        now = datetime.now(timezone.utc)
        recent_run = (now - timedelta(minutes=30)).isoformat()
        mgr = _seed_task(
            tmp_path,
            title="Skill Evaluate",
            job="skill-evaluate",
            scanner=None,
            min_execution_interval="2h",
            # next_run_at in the past so it's due
            next_run_at=(now - timedelta(hours=1)).isoformat(),
            now=now,
        )
        # Set last_run_at to 30 min ago (within the 2h interval)
        task_list = mgr.load_task_list()
        task_list.tasks[0].last_run_at = recent_run
        mgr.flush(task_list)

        executor = _make_executor(tmp_path, routine_manager=mgr)
        task_list = mgr.load_task_list()

        result = await executor.tick(
            task_list,
            run_agent=AsyncMock(),
            inject_context=AsyncMock(),
            run_id="test_run",
        )

        assert result.skipped == 1
        assert result.results[0].status == "skipped"
        assert result.results[0].error == "interval_not_met"


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

    def test_list_due_inline_excludes_interval_not_met(self, tmp_path):
        """Tasks whose min_execution_interval hasn't elapsed since last_run_at
        should NOT appear in list_due_inline_tasks, preventing scheduler spin."""
        now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
        mgr = _seed_task(
            tmp_path,
            title="Gated job",
            execution_mode="inline",
            job="skill-evaluate",
            min_execution_interval="2h",
            next_run_at="2026-03-01T11:00:00+00:00",
            now=now,
        )
        # Simulate: task ran at 11:30 UTC, only 30min ago — interval not met
        task_list = mgr.load_task_list()
        task = task_list.tasks[0]
        task.last_run_at = "2026-03-01T11:30:00+00:00"
        task.state = "pending"
        task.next_run_at = "2026-03-01T11:00:00+00:00"  # past → get_due_tasks says "due"
        mgr.flush(task_list)

        executor = _make_executor(tmp_path, routine_manager=mgr)
        tasks = executor.list_due_inline_tasks(now=now)
        assert len(tasks) == 0, "Task with unmet min_execution_interval should be excluded"

    def test_list_due_inline_includes_interval_met(self, tmp_path):
        """Tasks whose min_execution_interval HAS elapsed should still appear."""
        now = datetime(2026, 3, 1, 14, 0, tzinfo=timezone.utc)
        mgr = _seed_task(
            tmp_path,
            title="Gated job",
            execution_mode="inline",
            job="skill-evaluate",
            min_execution_interval="2h",
            next_run_at="2026-03-01T11:00:00+00:00",
            now=now,
        )
        # Simulate: task ran at 11:30 UTC, 2.5h ago — interval met
        task_list = mgr.load_task_list()
        task = task_list.tasks[0]
        task.last_run_at = "2026-03-01T11:30:00+00:00"
        task.state = "pending"
        task.next_run_at = "2026-03-01T11:00:00+00:00"
        mgr.flush(task_list)

        executor = _make_executor(tmp_path, routine_manager=mgr)
        tasks = executor.list_due_inline_tasks(now=now)
        assert len(tasks) == 1

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


class TestIsolatedJobExecution:
    """Isolated + job tasks should call _invoke_job, not create an agent turn."""

    @pytest.mark.asyncio
    async def test_isolated_job_calls_invoke_job_not_agent(self, tmp_path):
        """When an isolated task has task.job set, it should run the Python
        module via _invoke_job instead of creating an LLM agent turn."""
        mgr = _seed_task(
            tmp_path,
            title="Skill Evaluate",
            execution_mode="isolated",
            job="skill-evaluate",
            timeout_seconds=180,
        )
        executor = _make_executor(tmp_path, routine_manager=mgr)
        task_list = mgr.load_task_list()
        run_agent = AsyncMock(return_value="agent result")

        with patch.object(
            executor, '_invoke_job',
            new_callable=AsyncMock,
            return_value="Evaluated 3/3 skills",
        ) as mock_invoke_job, patch.object(
            executor, '_create_job_agent',
            new_callable=AsyncMock,
        ) as mock_create_agent:
            result = await executor.tick(
                task_list,
                run_agent=run_agent,
                inject_context=AsyncMock(),
                run_id="test_run",
                include_inline=False,
            )

        # Job module should be called
        mock_invoke_job.assert_called_once()
        # Agent should NOT be created
        mock_create_agent.assert_not_called()
        # run_agent should NOT be called for this task
        run_agent.assert_not_called()
        assert result.executed == 1
        assert result.results[0].status == "done"

    @pytest.mark.asyncio
    async def test_isolated_job_uses_delivery_pipeline(self, tmp_path):
        """Isolated job results should go through the delivery pipeline
        (deposit_job_event, inject_to_history, emit_realtime)."""
        mgr = _seed_task(
            tmp_path,
            title="Skill Evaluate",
            execution_mode="isolated",
            job="skill-evaluate",
            timeout_seconds=180,
        )
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
        executor = _make_executor(
            tmp_path, routine_manager=mgr,
            session_manager=sm, delivery=delivery,
        )
        task_list = mgr.load_task_list()

        with patch.object(
            executor, '_invoke_job',
            new_callable=AsyncMock,
            return_value="Evaluated 3/3 skills",
        ), patch.object(
            delivery, 'deposit_job_event',
            new_callable=AsyncMock,
        ) as mock_deposit, patch.object(
            delivery, 'inject_to_history',
            new_callable=AsyncMock,
        ) as mock_inject, patch.object(
            delivery, '_emit_realtime',
            new_callable=AsyncMock,
        ) as mock_realtime:
            result = await executor.tick(
                task_list,
                run_agent=AsyncMock(),
                inject_context=AsyncMock(),
                run_id="test_run",
                include_inline=False,
            )

        assert result.executed == 1
        mock_deposit.assert_called_once()
        mock_inject.assert_called_once()
        mock_realtime.assert_called_once()

    @pytest.mark.asyncio
    async def test_isolated_agent_task_still_creates_agent(self, tmp_path):
        """Isolated tasks WITHOUT job should still create an agent session."""
        mgr = _seed_task(
            tmp_path,
            title="Daily News",
            execution_mode="isolated",
            description="Generate news",
            timeout_seconds=300,
        )
        executor = _make_executor(tmp_path, routine_manager=mgr)
        task_list = mgr.load_task_list()
        run_agent = AsyncMock(return_value="News report")

        with patch.object(
            executor, '_create_job_agent',
            new_callable=AsyncMock,
        ) as mock_create_agent, patch.object(
            executor, '_invoke_job',
            new_callable=AsyncMock,
        ) as mock_invoke_job:
            mock_agent = MagicMock()
            mock_create_agent.return_value = mock_agent
            result = await executor.tick(
                task_list,
                run_agent=run_agent,
                inject_context=AsyncMock(),
                run_id="test_run",
                include_inline=False,
            )

        # Agent SHOULD be created for non-job isolated tasks
        mock_create_agent.assert_called_once()
        # _invoke_job should NOT be called
        mock_invoke_job.assert_not_called()
        run_agent.assert_called_once()

    @pytest.mark.asyncio
    async def test_isolated_job_failure_reports_via_delivery(self, tmp_path):
        """When an isolated job fails, error should go through delivery pipeline."""
        mgr = _seed_task(
            tmp_path,
            title="Skill Evaluate",
            execution_mode="isolated",
            job="skill-evaluate",
            timeout_seconds=180,
        )
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
        executor = _make_executor(
            tmp_path, routine_manager=mgr,
            session_manager=sm, delivery=delivery,
        )
        task_list = mgr.load_task_list()

        with patch.object(
            executor, '_invoke_job',
            new_callable=AsyncMock,
            side_effect=RuntimeError("LLM judge failed"),
        ), patch.object(
            delivery, 'deposit_job_event',
            new_callable=AsyncMock,
        ) as mock_deposit, patch.object(
            delivery, '_emit_realtime',
            new_callable=AsyncMock,
        ) as mock_realtime:
            result = await executor.tick(
                task_list,
                run_agent=AsyncMock(),
                inject_context=AsyncMock(),
                run_id="test_run",
                include_inline=False,
            )

        assert result.failed == 1
        assert "LLM judge failed" in result.results[0].error
        # Failure should be reported via delivery
        mock_deposit.assert_called_once()
        assert mock_deposit.call_args[1]["event_type"] == "job_failed"
        mock_realtime.assert_called_once()


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


class TestInvokeJobLLMErrorHandling:
    """_invoke_job should catch LLM errors and return degraded result, not raise."""

    @pytest.mark.asyncio
    async def test_transient_error_returns_degraded_not_raises(self, tmp_path):
        mgr = _seed_task(tmp_path, title="Memory Review", job="memory-review")
        executor = _make_executor(tmp_path, routine_manager=mgr)
        task_list = mgr.load_task_list()
        task = task_list.tasks[0]

        with patch("importlib.import_module") as mock_import:
            mock_module = MagicMock()
            mock_module.run = AsyncMock(side_effect=LLMTransientError("Connection error"))
            mock_import.return_value = mock_module
            result = await executor._invoke_job(task, None, "test_run")

        assert "LLM unavailable" in result
        assert "Connection error" in result

    @pytest.mark.asyncio
    async def test_config_error_returns_degraded_not_raises(self, tmp_path):
        mgr = _seed_task(tmp_path, title="Memory Review", job="memory-review")
        executor = _make_executor(tmp_path, routine_manager=mgr)
        task_list = mgr.load_task_list()
        task = task_list.tasks[0]

        with patch("importlib.import_module") as mock_import:
            mock_module = MagicMock()
            mock_module.run = AsyncMock(side_effect=LLMConfigError("model not found"))
            mock_import.return_value = mock_module
            result = await executor._invoke_job(task, None, "test_run")

        assert "LLM unavailable" in result

    @pytest.mark.asyncio
    async def test_non_llm_error_still_raises(self, tmp_path):
        mgr = _seed_task(tmp_path, title="Memory Review", job="memory-review")
        executor = _make_executor(tmp_path, routine_manager=mgr)
        task_list = mgr.load_task_list()
        task = task_list.tasks[0]

        with patch("importlib.import_module") as mock_import:
            mock_module = MagicMock()
            mock_module.run = AsyncMock(side_effect=ValueError("bad data"))
            mock_import.return_value = mock_module
            with pytest.raises(ValueError, match="bad data"):
                await executor._invoke_job(task, None, "test_run")
