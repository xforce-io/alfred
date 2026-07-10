"""Unit tests for CronExecutor task execution engine."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.everbot.core.runtime.cron import CronExecutor, CronTickResult, TaskResult
from src.everbot.core.runtime.cron_delivery import CronDelivery
from src.everbot.core.tasks.execution_gate import GateVerdict
from src.everbot.core.tasks.routine_manager import RoutineManager
from src.everbot.core.tasks.task_manager import TaskState
from src.everbot.core.jobs.llm_errors import LLMTransientError, LLMConfigError
from src.everbot.core.agent.provider.milkie.provider import MilkieAgentError


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
        assert r.user_visible_output is None

    def test_user_visible_output_with_done_results(self):
        r = CronTickResult(
            executed=2,
            results=[
                TaskResult(task_id="a", status="done", output="result A"),
                TaskResult(task_id="b", status="failed", output="err"),
                TaskResult(task_id="c", status="done", output="result C"),
            ],
        )
        assert r.user_visible_output is not None
        assert "result A" in r.user_visible_output
        assert "result C" in r.user_visible_output
        assert "err" not in r.user_visible_output

    def test_user_visible_output_all_silent(self):
        """Done jobs returning None contribute nothing — aggregate stays None."""
        r = CronTickResult(
            executed=2,
            results=[
                TaskResult(task_id="a", status="done", output=None),
                TaskResult(task_id="b", status="done", output=None),
            ],
        )
        assert r.user_visible_output is None


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
            await executor.tick(
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


class TestIsolatedAgentRetries:
    @pytest.mark.asyncio
    async def test_transient_remote_disconnect_is_not_retried_inside_agent_runner(self, tmp_path):
        mgr = _seed_task(tmp_path, title="Transient task", execution_mode="isolated")
        executor = _make_executor(tmp_path, routine_manager=mgr)
        task = mgr.load_task_list().tasks[0]

        agent = MagicMock()
        agent.executor.context = MagicMock()
        executor._create_job_agent = AsyncMock(return_value=agent)
        run_agent = AsyncMock(
            side_effect=ConnectionError("peer closed connection without sending complete message body")
        )

        with patch.object(executor, "_build_job_system_prompt", return_value="system"):
            with pytest.raises(ConnectionError):
                await executor._run_isolated_agent(task, "run_123", run_agent=run_agent)

        assert run_agent.await_count == 1
        executor.session_manager.save_session.assert_awaited()
        executor.session_manager.mark_session_archived.assert_awaited()

    @pytest.mark.asyncio
    async def test_scheduler_owns_retry_then_delivers_success_once(self, tmp_path):
        mgr = _seed_task(
            tmp_path, title="Transient task", execution_mode="isolated",
        )
        task_list = mgr.load_task_list()
        task = task_list.tasks[0]
        task.max_retry = 1
        mgr.flush(task_list)

        executor = _make_executor(tmp_path, routine_manager=mgr)
        agent = SimpleNamespace(last_run_id="milkie-run")
        executor._create_job_agent = AsyncMock(return_value=agent)
        executor._build_job_system_prompt = MagicMock(return_value="system")
        executor._record_skill_log = MagicMock()
        executor.delivery.deposit_job_event = AsyncMock()
        executor.delivery.inject_to_history = AsyncMock()
        executor.delivery._emit_realtime = AsyncMock()

        error = MilkieAgentError(
            "Model provider connection failed.",
            envelope={
                "code": "MODEL_CONNECTION_ERROR", "retryable": True,
                "phase": "stream_open", "provider": "volcengine", "model": "glm-5.2",
            },
            run_id="failed-run",
        )
        run_agent = AsyncMock(side_effect=[error, "final report"])

        first = await executor.tick(
            task_list, run_agent=run_agent, inject_context=AsyncMock(), run_id="cron-1",
        )
        assert first.failed == 1
        assert run_agent.await_count == 1
        assert task.state == "pending"
        assert task.retry == 1
        assert task.last_error_code == "MODEL_CONNECTION_ERROR"
        assert task.last_error_retryable is True

        task.next_run_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        second = await executor.tick(
            task_list, run_agent=run_agent, inject_context=AsyncMock(), run_id="cron-2",
        )
        assert second.executed == 1
        assert run_agent.await_count == 2
        successful_pushes = [
            call for call in executor.delivery._emit_realtime.await_args_list
            if call.kwargs.get("transcript_worthy") is True
        ]
        assert len(successful_pushes) == 1
        assert successful_pushes[0].args[0] == "final report"

    @pytest.mark.asyncio
    async def test_staged_agent_resumes_from_analyze_after_restart(self, tmp_path):
        executor = _make_executor(tmp_path)
        agent = SimpleNamespace(last_run_id="milkie-run")
        executor._create_job_agent = AsyncMock(return_value=agent)
        executor._build_job_system_prompt = MagicMock(return_value="system")
        executor._append_run_provenance = MagicMock(side_effect=lambda result, _agent: result)
        executor._observe_provenance = MagicMock()
        executor._record_skill_log = MagicMock()
        executor.delivery.deposit_job_event = AsyncMock()
        executor.delivery.inject_to_history = AsyncMock()
        executor.delivery._emit_realtime = AsyncMock()
        task = SimpleNamespace(
            id="staged", title="Staged", description="", timeout_seconds=30,
            job=None, execution_id="staged:2026-07-10T10:00:00Z",
            staged={
                "fetch": {"prompt": "fetch fixture"},
                "analyze": {"prompt": "analyze fixture"},
                "destination": "primary",
            },
        )
        run_agent = AsyncMock(side_effect=["fetched artifact", ConnectionError("model down"), "final report"])

        with pytest.raises(ConnectionError):
            await executor._run_isolated_agent(task, "cron-1", run_agent=run_agent)
        await executor._run_isolated_agent(task, "cron-2", run_agent=run_agent)

        assert run_agent.await_count == 3
        assert run_agent.await_args_list[2].args[1].startswith("analyze fixture")
        assert "fetched artifact" in run_agent.await_args_list[2].args[1]
        completed_events = [
            call for call in executor.delivery.deposit_job_event.await_args_list
            if call.kwargs.get("event_type") == "job_completed"
        ]
        assert len(completed_events) == 1
        executor.delivery.inject_to_history.assert_awaited_once()
        successful_pushes = [
            call for call in executor.delivery._emit_realtime.await_args_list
            if call.kwargs.get("transcript_worthy") is True
        ]
        assert len(successful_pushes) == 1


class TestSkillLogRecording:
    """Lock in: isolated-agent runs MUST record skill invocations to skill_logs.

    Regression guard for the Apr 2026 cron.py refactor that silently dropped
    this call site, leaving SLM evaluate/evolve starved of input for over a
    month before being noticed.
    """

    def _write_trajectory(self, path: Path, skill_calls: list[dict]) -> None:
        """Write a minimal trajectory JSON containing _load_resource_skill calls."""
        import json as _json
        trajectory = []
        for call in skill_calls:
            trajectory.append({
                "role": "assistant",
                "tool_calls": [{"function": {
                    "name": "_load_resource_skill",
                    "arguments": _json.dumps(call),
                }}],
            })
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json.dumps({"trajectory": trajectory}), encoding="utf-8")

    def test_extract_skills_from_trajectory_dedupes_and_skips_unrelated(self, tmp_path):
        executor = _make_executor(tmp_path)
        traj_path = tmp_path / "trajectory_sess1.json"
        self._write_trajectory(traj_path, [
            {"skill_name": "paper-discovery", "mode": "full"},
            {"skill_name": "paper-discovery", "mode": "lazy"},  # duplicate
            {"skill_name": "web", "mode": "full"},
        ])
        udm = MagicMock()
        udm.get_session_trajectory_path.return_value = traj_path
        with patch("src.everbot.infra.user_data.get_user_data_manager", return_value=udm):
            skills = executor._extract_skills_from_trajectory("sess1")
        assert sorted(skills) == ["paper-discovery", "web"]

    def test_extract_skills_from_trajectory_missing_file_returns_empty(self, tmp_path):
        executor = _make_executor(tmp_path)
        udm = MagicMock()
        udm.get_session_trajectory_path.return_value = tmp_path / "nonexistent.json"
        with patch("src.everbot.infra.user_data.get_user_data_manager", return_value=udm):
            assert executor._extract_skills_from_trajectory("sess_missing") == []

    def test_record_skill_log_invokes_recorder_per_unique_skill(self, tmp_path):
        executor = _make_executor(tmp_path)
        recorder = MagicMock()
        executor._skill_log_recorder = recorder
        traj_path = tmp_path / "trajectory_sess2.json"
        self._write_trajectory(traj_path, [
            {"skill_name": "paper-discovery", "mode": "full"},
            {"skill_name": "web", "mode": "full"},
        ])
        udm = MagicMock()
        udm.get_session_trajectory_path.return_value = traj_path
        task = MagicMock(description="daily papers task")
        with patch("src.everbot.infra.user_data.get_user_data_manager", return_value=udm):
            executor._record_skill_log(task, "report content", "sess2")
        # one record per unique skill, with task description as context_before
        assert recorder.maybe_record.call_count == 2
        recorded_skills = sorted(c.args[0] for c in recorder.maybe_record.call_args_list)
        assert recorded_skills == ["paper-discovery", "web"]
        for call in recorder.maybe_record.call_args_list:
            assert call.kwargs["session_id"] == "sess2"
            assert call.kwargs["skill_output"] == "report content"
            assert call.kwargs["context_before"] == "daily papers task"

    def test_record_skill_log_no_recorder_is_noop(self, tmp_path):
        executor = _make_executor(tmp_path)
        executor._skill_log_recorder = None
        # Should not raise even if trajectory lookup would fail.
        executor._record_skill_log(MagicMock(description=""), "out", "sess3")

    @pytest.mark.asyncio
    async def test_isolated_agent_success_calls_record_skill_log(self, tmp_path):
        """End-to-end: successful isolated agent run must invoke _record_skill_log.

        This is the exact wiring that was severed by commit 2c1da6f in Apr 2026.
        """
        mgr = _seed_task(tmp_path, title="Daily papers", execution_mode="isolated",
                         description="generate daily paper digest")
        executor = _make_executor(tmp_path, routine_manager=mgr)
        task = mgr.load_task_list().tasks[0]

        agent = MagicMock()
        agent.executor.context = MagicMock()
        executor._create_job_agent = AsyncMock(return_value=agent)
        run_agent = AsyncMock(return_value="report content")

        with patch.object(executor, "_build_job_system_prompt", return_value="sys"), \
             patch.object(executor, "_record_skill_log") as mock_record:
            result = await executor._run_isolated_agent(task, "run_xyz", run_agent=run_agent)

        assert result == "report content"
        mock_record.assert_called_once()
        call_args = mock_record.call_args
        assert call_args.args[0] is task
        assert call_args.args[1] == "report content"


class TestCreateJobAgentProviderRouting:
    """CronExecutor._create_job_agent must route creation through the per-agent
    provider (milkie/dolphin selection), NOT the raw injected agent_factory.
    No tools_override → full tool access.
    """

    @pytest.mark.asyncio
    async def test_routes_through_provider_full_access(self, tmp_path, monkeypatch):
        import importlib

        sentinel_agent = MagicMock()
        create_agent = AsyncMock(return_value=sentinel_agent)
        factory_provider = MagicMock(create_agent=create_agent)

        # Provider used for set_session_id / init_trajectory side-effects.
        runtime_provider = MagicMock(
            set_session_id=MagicMock(),
            set_variable=MagicMock(),
            init_trajectory=MagicMock(),
        )

        # cron.py imports get_provider / get_provider_for_agent locally from
        # the provider package at call time → patch on the package module.
        provider_mod = importlib.import_module(
            CronExecutor.__module__.rsplit(".", 2)[0] + ".agent.provider"
        )
        monkeypatch.setattr(
            provider_mod, "get_provider_for_agent", lambda name: factory_provider
        )
        # Operations route via provider_for(agent) (per-agent type dispatch); the
        # created agent is a MagicMock (not a milkie handle) so patch that seam.
        monkeypatch.setattr(provider_mod, "provider_for", lambda agent: runtime_provider)

        # Neutralize user-data manager so trajectory init does not touch real paths.
        user_data_mod = importlib.import_module("src.everbot.infra.user_data")
        traj_path = tmp_path / "trajectory.jsonl"
        user_data = MagicMock()
        user_data.get_session_trajectory_path.return_value = traj_path
        monkeypatch.setattr(
            user_data_mod, "get_user_data_manager", lambda: user_data
        )

        raw_factory = AsyncMock(return_value=MagicMock())
        executor = _make_executor(tmp_path, agent_factory=raw_factory)

        result = await executor._create_job_agent("job_session_42")

        assert result is sentinel_agent
        # Routed through provider with name + workspace and NO tools_override.
        create_agent.assert_awaited_once_with("test_agent", tmp_path)
        assert "tools_override" not in create_agent.await_args.kwargs
        # Raw injected agent_factory must NOT be used for creation.
        raw_factory.assert_not_awaited()
        # Session-scoping side-effects still applied via runtime provider.
        runtime_provider.set_session_id.assert_called_once_with(
            sentinel_agent, "job_session_42"
        )
        runtime_provider.init_trajectory.assert_called_once()


class TestBuildJobSystemPromptMilkieSafe:
    """_build_job_system_prompt must NOT crash on a milkie handle (no .executor).

    dolphin: reads context.workspace_instructions (unchanged).
    milkie: routes through provider.get_variable (may be None — tolerated).
    """

    def _patch_provider(self, monkeypatch, provider):
        import importlib
        provider_mod = importlib.import_module(
            CronExecutor.__module__.rsplit(".", 2)[0] + ".agent.provider"
        )
        # _build_job_system_prompt now dispatches via provider_for(agent).
        monkeypatch.setattr(provider_mod, "provider_for", lambda agent: provider)

    def test_milkie_handle_routes_through_get_variable(self, monkeypatch):
        from src.everbot.core.tasks.task_manager import Task

        # Milkie handle: bare object WITHOUT .executor.
        handle = SimpleNamespace(base_url="http://x", context_id="c1")

        class _MilkieProvider:
            def needs_history_restore(self):
                return False

            def get_variable(self, agent, key):
                assert agent is handle
                assert key == "workspace_instructions"
                return "WS BASE"

        self._patch_provider(monkeypatch, _MilkieProvider())
        task = Task(id="t1", title="T", description="do the thing")
        out = CronExecutor._build_job_system_prompt(handle, task)
        assert out == "WS BASE\n\ndo the thing"

    def test_milkie_handle_tolerates_none_workspace(self, monkeypatch):
        from src.everbot.core.tasks.task_manager import Task

        handle = SimpleNamespace(base_url="http://x", context_id="c1")

        class _MilkieProvider:
            def needs_history_restore(self):
                return False

            def get_variable(self, agent, key):
                return None  # serve has no var set yet

        self._patch_provider(monkeypatch, _MilkieProvider())
        task = Task(id="t2", title="T", description="just task")
        out = CronExecutor._build_job_system_prompt(handle, task)
        assert out == "just task"

    def test_dolphin_path_reads_context_attribute(self, monkeypatch):
        from src.everbot.core.tasks.task_manager import Task

        ctx = SimpleNamespace(workspace_instructions="DOLPHIN WS")
        agent = SimpleNamespace(executor=SimpleNamespace(context=ctx))

        class _DolphinProvider:
            def needs_history_restore(self):
                return True

        self._patch_provider(monkeypatch, _DolphinProvider())
        task = Task(id="t3", title="T", description="cron job")
        out = CronExecutor._build_job_system_prompt(agent, task)
        assert out == "DOLPHIN WS\n\ncron job"


class TestTranscriptWorthyDelivery:
    """#60:内容型 job/agent 成功投递标记 transcript_worthy=True(进逐字稿 projection);
    失败消息不标记。"""

    @pytest.mark.asyncio
    async def test_isolated_job_success_marks_transcript_worthy(self, tmp_path):
        executor = _make_executor(tmp_path)
        executor._invoke_job = AsyncMock(return_value="REPORT BODY")
        executor.delivery.deposit_job_event = AsyncMock()
        executor.delivery.inject_to_history = AsyncMock()
        executor.delivery._emit_realtime = AsyncMock()
        task = SimpleNamespace(job="mod.fn", title="Daily", id="t1", timeout_seconds=60)

        out = await executor._run_isolated_job(task, "run-1")

        assert out == "REPORT BODY"
        executor.delivery._emit_realtime.assert_awaited_once()
        assert executor.delivery._emit_realtime.call_args.kwargs.get("transcript_worthy") is True

    @pytest.mark.asyncio
    async def test_isolated_job_failure_not_transcript_worthy(self, tmp_path):
        executor = _make_executor(tmp_path)
        executor._invoke_job = AsyncMock(side_effect=RuntimeError("boom"))
        executor.delivery.deposit_job_event = AsyncMock()
        executor.delivery.inject_to_history = AsyncMock()
        executor.delivery._emit_realtime = AsyncMock()
        task = SimpleNamespace(job="mod.fn", title="Daily", id="t1", timeout_seconds=60)

        with patch("src.everbot.core.tasks.task_manager.format_retry_hint", return_value=""):
            with pytest.raises(RuntimeError):
                await executor._run_isolated_job(task, "run-1")

        executor.delivery._emit_realtime.assert_awaited_once()
        assert executor.delivery._emit_realtime.call_args.kwargs.get("transcript_worthy", False) is False

    @pytest.mark.asyncio
    async def test_isolated_agent_success_marks_transcript_worthy(self, tmp_path):
        executor = _make_executor(tmp_path)
        executor._create_job_agent = AsyncMock(return_value=SimpleNamespace(name="a"))
        executor._build_job_system_prompt = MagicMock(return_value="sys")
        executor._record_skill_log = MagicMock()
        executor.delivery.deposit_job_event = AsyncMock()
        executor.delivery.inject_to_history = AsyncMock()
        executor.delivery._emit_realtime = AsyncMock()
        task = SimpleNamespace(title="Daily", id="t1", timeout_seconds=60)

        async def _run_agent(agent, prompt, system_prompt_override=None):
            return "AGENT REPORT"

        with patch("src.everbot.core.runtime.cron.build_job_session_id", return_value="job_t1"), \
             patch("src.everbot.core.runtime.cron.build_isolated_task_prompt", return_value="prompt"):
            out = await executor._run_isolated_agent(task, "run-1", run_agent=_run_agent)

        assert out == "AGENT REPORT"
        assert executor.delivery._emit_realtime.call_args.kwargs.get("transcript_worthy") is True


# ---- #130 T1: provenance footer wiring (CronExecutor._append_run_provenance) ----

_PROV_BLOCK = ('<PROVENANCE>{"signals":[{"title":"Hormuz","url":"https://cnbc.com/x"}]}'
               '</PROVENANCE>')
# Trusted producer run: run_command invoking rhino_report.py, request+response paired by
# toolCallId (only such output is honored — see provenance_footer security contract).
_PROV_EVENTS = [
    {"type": "tool.requested",
     "payload": {"toolName": "run_command", "toolCallId": "c1",
                 "input": {"command": "python /repo/skills/gray-rhino/scripts/"
                                       "rhino_report.py --format text"}}},
    {"type": "tool.responded",
     "payload": {"toolName": "run_command", "toolCallId": "c1",
                 "output": {"stdout": "report body\n" + _PROV_BLOCK}}},
]


def test_append_run_provenance_adds_footer(tmp_path, monkeypatch):
    """LLM prose dropped the links; mechanically pull top-1 link from run events."""
    from src.everbot.core.runtime import provenance_gate
    monkeypatch.setattr(provenance_gate, "read_run_events", lambda *a, **k: _PROV_EVENTS)
    executor = _make_executor(tmp_path)
    agent = SimpleNamespace(last_run_id="run-1")

    out = executor._append_run_provenance("# Gray Rhino\nSignal: Hormuz (no link)", agent)
    assert "https://cnbc.com/x" in out
    assert out.startswith("# Gray Rhino")


def test_append_run_provenance_noop_without_run_id(tmp_path):
    """agent without last_run_id -> returned unchanged (no event read)."""
    executor = _make_executor(tmp_path)
    result = "# report"
    assert executor._append_run_provenance(result, SimpleNamespace()) == result


def test_append_run_provenance_never_raises(tmp_path, monkeypatch):
    """Reading events raises -> swallowed, returns body (delivery must not crash)."""
    from src.everbot.core.runtime import provenance_gate
    def _boom(*a, **k):
        raise RuntimeError("disk gone")
    monkeypatch.setattr(provenance_gate, "read_run_events", _boom)
    executor = _make_executor(tmp_path)
    result = "# report"
    assert executor._append_run_provenance(result, SimpleNamespace(last_run_id="r")) == result


@pytest.mark.asyncio
async def test_isolated_agent_delivers_footer_appended_result(tmp_path, monkeypatch):
    """End-to-end wiring: LLM prose dropped links, delivered report still carries them."""
    from src.everbot.core.runtime import provenance_gate
    monkeypatch.setattr(provenance_gate, "read_run_events", lambda *a, **k: _PROV_EVENTS)

    executor = _make_executor(tmp_path)
    agent = SimpleNamespace(last_run_id="run-1")
    executor._create_job_agent = AsyncMock(return_value=agent)
    executor._build_job_system_prompt = MagicMock(return_value="sys")
    executor._record_skill_log = MagicMock()
    executor.delivery.deposit_job_event = AsyncMock()
    executor.delivery.inject_to_history = AsyncMock()
    captured = {}
    async def _capture(result, run_id, **k):
        captured["result"] = result
    executor.delivery._emit_realtime = AsyncMock(side_effect=_capture)

    run_agent = AsyncMock(return_value="# Gray Rhino\nHormuz (LLM stripped the link)")
    task = SimpleNamespace(id="t1", title="Gray Rhino", description="d",
                           timeout_seconds=30, job=None)

    out = await executor._run_isolated_agent(task, "run-1", run_agent=run_agent)

    assert "https://cnbc.com/x" in out                      # return value carries link
    assert "https://cnbc.com/x" in captured["result"]       # push (_emit_realtime) carries link
    # detail (deposit_job_event) carries it too — all three delivery paths consistent
    _, kwargs = executor.delivery.deposit_job_event.call_args
    assert "https://cnbc.com/x" in kwargs["detail"]


@pytest.mark.asyncio
async def test_isolated_agent_projection_anchor_is_milkie_run_id(tmp_path, monkeypatch):
    """#130 T2: the delivered projection anchor must be the milkie run id (deref-able by
    get_execution/get_lineage under milkie#200's delivered-runId allowlist), not the job
    session id — otherwise readByRunId can't resolve it."""
    from src.everbot.core.runtime import provenance_gate
    monkeypatch.setattr(provenance_gate, "read_run_events", lambda *a, **k: [])
    executor = _make_executor(tmp_path)
    agent = SimpleNamespace(last_run_id="31850ca6-uuid")
    executor._create_job_agent = AsyncMock(return_value=agent)
    executor._build_job_system_prompt = MagicMock(return_value="sys")
    executor._record_skill_log = MagicMock()
    executor.delivery.deposit_job_event = AsyncMock()
    executor.delivery.inject_to_history = AsyncMock()
    executor.delivery._emit_realtime = AsyncMock()
    run_agent = AsyncMock(return_value="# report")
    task = SimpleNamespace(id="t1", title="gr", description="d",
                           timeout_seconds=30, job=None)

    await executor._run_isolated_agent(task, "run-1", run_agent=run_agent)

    _, kwargs = executor.delivery._emit_realtime.call_args
    assert kwargs["source_session_id"] == "31850ca6-uuid"
