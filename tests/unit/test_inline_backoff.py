"""Tests for #78: inline cron task backoff on LLM unavailability + probe cooldown.

When the LLM is unreachable, the scheduler's inline task path must back off
(exponential, symmetric with _tick_heartbeats/_tick_inspector) instead of
spinning every 1s tick. Probe cooldown caps the LLM connection attempt rate.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from src.everbot.core.jobs.llm_errors import LLMUnavailableError
from src.everbot.core.runtime.scheduler import (
    InlineSchedule,
    Scheduler,
    SchedulerTask,
)


def _one_inline_task(agent_name: str = "test_agent"):
    def _get_due(ts):
        return [SchedulerTask(id="t1", agent_name=agent_name, execution_mode="inline")]
    return _get_due


def _make_hb_runner(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from src.everbot.core.runtime.heartbeat import HeartbeatRunner
    session_manager = SimpleNamespace(
        get_primary_session_id=lambda agent_name: f"web_session_{agent_name}",
        get_heartbeat_session_id=lambda agent_name: f"heartbeat_session_{agent_name}",
    )
    return HeartbeatRunner(
        agent_name="test_agent",
        workspace_path=tmp_path,
        session_manager=session_manager,
        agent_factory=AsyncMock(),
        interval_minutes=1,
        active_hours=(0, 24),
        max_retries=3,
        on_result=None,
    )


# ── 方向 2: scheduler inline 退避 ──────────────────────────────


class TestInlineBackoff:
    def test_llm_unavailable_increases_backoff(self):
        async def _run_inline(agent_name, tasks, ts):
            raise LLMUnavailableError("llm down")

        schedule = InlineSchedule(agent_name="test_agent", base_interval_minutes=1, max_backoff_minutes=60)
        scheduler = Scheduler(
            get_due_tasks=_one_inline_task(),
            run_inline=_run_inline,
            inline_schedules={"test_agent": schedule},
        )

        ts = datetime(2026, 2, 15, 12, 0, tzinfo=timezone.utc)
        asyncio.run(scheduler._tick_tasks(ts))
        assert schedule.consecutive_failures == 1
        assert schedule.next_inline_at is not None
        assert schedule.next_inline_at > ts

    def test_consecutive_failures_grow_exponentially(self):
        async def _run_inline(agent_name, tasks, ts):
            raise LLMUnavailableError("llm down")

        schedule = InlineSchedule(agent_name="test_agent", base_interval_minutes=1, max_backoff_minutes=60)
        scheduler = Scheduler(
            get_due_tasks=_one_inline_task(),
            run_inline=_run_inline,
            inline_schedules={"test_agent": schedule},
        )
        ts = datetime(2026, 2, 15, 12, 0, tzinfo=timezone.utc)

        asyncio.run(scheduler._tick_tasks(ts))
        assert schedule.consecutive_failures == 1
        first_backoff = schedule.next_inline_at - ts  # base * 2^1 = 2 min

        # Force due again (simulate later tick past next_inline_at)
        ts2 = schedule.next_inline_at
        asyncio.run(scheduler._tick_tasks(ts2))
        assert schedule.consecutive_failures == 2
        second_backoff = schedule.next_inline_at - ts2  # base * 2^2 = 4 min
        assert second_backoff > first_backoff

    def test_success_resets_backoff(self):
        async def _ok_inline(agent_name, tasks, ts):
            return "HEARTBEAT_OK"

        schedule = InlineSchedule(agent_name="test_agent", consecutive_failures=5)
        scheduler = Scheduler(
            get_due_tasks=_one_inline_task(),
            run_inline=_ok_inline,
            inline_schedules={"test_agent": schedule},
        )
        ts = datetime(2026, 2, 15, 12, 0, tzinfo=timezone.utc)
        asyncio.run(scheduler._tick_tasks(ts))
        assert schedule.consecutive_failures == 0
        assert schedule.next_inline_at is None

    def test_backoff_capped_at_max(self):
        async def _run_inline(agent_name, tasks, ts):
            raise LLMUnavailableError("llm down")

        schedule = InlineSchedule(
            agent_name="test_agent",
            base_interval_minutes=1,
            max_backoff_minutes=10,
            consecutive_failures=99,  # already high
        )
        scheduler = Scheduler(
            get_due_tasks=_one_inline_task(),
            run_inline=_run_inline,
            inline_schedules={"test_agent": schedule},
        )
        ts = datetime(2026, 2, 15, 12, 0, tzinfo=timezone.utc)
        asyncio.run(scheduler._tick_tasks(ts))
        delta = schedule.next_inline_at - ts
        assert delta <= timedelta(minutes=10, seconds=5)

    def test_next_inline_at_gates_dispatch(self):
        """While next_inline_at is in the future, _run_inline must NOT be called."""
        calls = 0

        async def _run_inline(agent_name, tasks, ts):
            nonlocal calls
            calls += 1

        ts = datetime(2026, 2, 15, 12, 0, tzinfo=timezone.utc)
        schedule = InlineSchedule(
            agent_name="test_agent",
            next_inline_at=ts + timedelta(minutes=5),
            consecutive_failures=3,
        )
        scheduler = Scheduler(
            get_due_tasks=_one_inline_task(),
            run_inline=_run_inline,
            inline_schedules={"test_agent": schedule},
        )
        asyncio.run(scheduler._tick_tasks(ts))
        assert calls == 0
        # Backoff state untouched while gated
        assert schedule.consecutive_failures == 3

    def test_non_llm_exception_does_not_backoff(self):
        """A generic task error is swallowed and must NOT arm the backoff gate."""
        async def _run_inline(agent_name, tasks, ts):
            raise RuntimeError("some flaky task")

        schedule = InlineSchedule(agent_name="test_agent")
        scheduler = Scheduler(
            get_due_tasks=_one_inline_task(),
            run_inline=_run_inline,
            inline_schedules={"test_agent": schedule},
        )
        ts = datetime(2026, 2, 15, 12, 0, tzinfo=timezone.utc)
        asyncio.run(scheduler._tick_tasks(ts))  # must not raise
        assert schedule.consecutive_failures == 0
        assert schedule.next_inline_at is None

    def test_lazy_schedule_created_when_not_registered(self):
        """Backoff works even without a pre-registered InlineSchedule."""
        async def _run_inline(agent_name, tasks, ts):
            raise LLMUnavailableError("llm down")

        scheduler = Scheduler(
            get_due_tasks=_one_inline_task("agentX"),
            run_inline=_run_inline,
        )
        ts = datetime(2026, 2, 15, 12, 0, tzinfo=timezone.utc)
        asyncio.run(scheduler._tick_tasks(ts))
        sched = scheduler._get_inline_schedule("agentX")
        assert sched.consecutive_failures == 1
        assert sched.next_inline_at is not None

    def test_multiple_agents_backoff_independently(self):
        async def _run_inline(agent_name, tasks, ts):
            if agent_name == "down":
                raise LLMUnavailableError("llm down")
            return "HEARTBEAT_OK"

        def _get_due(ts):
            return [
                SchedulerTask(id="a", agent_name="down", execution_mode="inline"),
                SchedulerTask(id="b", agent_name="up", execution_mode="inline"),
            ]

        down = InlineSchedule(agent_name="down")
        up = InlineSchedule(agent_name="up")
        scheduler = Scheduler(
            get_due_tasks=_get_due,
            run_inline=_run_inline,
            inline_schedules={"down": down, "up": up},
        )
        ts = datetime(2026, 2, 15, 12, 0, tzinfo=timezone.utc)
        asyncio.run(scheduler._tick_tasks(ts))
        assert down.consecutive_failures == 1
        assert down.next_inline_at is not None
        assert up.consecutive_failures == 0
        assert up.next_inline_at is None

    def test_lazy_schedule_base_interval_from_env(self, monkeypatch):
        """ALFRED_INLINE_BACKOFF_BASE_MIN overrides the lazy default base interval."""
        monkeypatch.setenv("ALFRED_INLINE_BACKOFF_BASE_MIN", "5")
        scheduler = Scheduler()
        sched = scheduler._get_inline_schedule("agentY")
        assert sched.base_interval_minutes == 5

    def test_state_persistence_round_trip(self, tmp_path):
        state_file = tmp_path / "scheduler_state.json"
        schedule = InlineSchedule(
            agent_name="test_agent",
            consecutive_failures=3,
            next_inline_at=datetime(2026, 2, 15, 12, 0, tzinfo=timezone.utc),
        )
        Scheduler(inline_schedules={"test_agent": schedule}, state_file=state_file)._save_state()

        # Fresh scheduler restores from disk (lazy-created entry)
        restored = Scheduler(state_file=state_file)
        sched2 = restored._get_inline_schedule("test_agent")
        assert sched2.consecutive_failures == 3
        assert sched2.next_inline_at == datetime(2026, 2, 15, 12, 0, tzinfo=timezone.utc)


    def test_storm_suppressed_then_recovers(self):
        """Acceptance: many 1s ticks during an outage → few dispatches, not one per tick.

        Then on recovery the inline path resumes immediately.
        """
        llm_down = True
        dispatches = 0

        async def _run_inline(agent_name, tasks, ts):
            nonlocal dispatches
            dispatches += 1
            if llm_down:
                raise LLMUnavailableError("llm down")

        schedule = InlineSchedule(agent_name="test_agent", base_interval_minutes=1, max_backoff_minutes=60)
        scheduler = Scheduler(
            get_due_tasks=_one_inline_task(),
            run_inline=_run_inline,
            inline_schedules={"test_agent": schedule},
        )

        # 600 ticks at 1s cadence over the outage window (10 min).
        start = datetime(2026, 2, 15, 12, 0, tzinfo=timezone.utc)
        for i in range(600):
            asyncio.run(scheduler._tick_tasks(start + timedelta(seconds=i)))

        # Without backoff this would be 600 dispatches; with backoff it's a
        # handful (exponential: ~2,4,8,... min apart, capped at 60).
        assert dispatches < 15
        outage_dispatches = dispatches

        # Network recovers: next tick past next_inline_at dispatches and resets.
        llm_down = False
        recover_ts = schedule.next_inline_at + timedelta(seconds=1)
        asyncio.run(scheduler._tick_tasks(recover_ts))
        assert dispatches == outage_dispatches + 1
        assert schedule.consecutive_failures == 0
        assert schedule.next_inline_at is None


# ── 失败信号契约: HeartbeatRunner.is_llm_unavailable ───────────


class TestLLMUnavailableSignal:
    def _runner(self, tmp_path):
        return _make_hb_runner(tmp_path)

    def test_is_llm_unavailable_false_initially(self, tmp_path):
        runner = self._runner(tmp_path)
        assert runner.is_llm_unavailable is False

    def test_is_llm_unavailable_reflects_state(self, tmp_path):
        runner = self._runner(tmp_path)
        runner._llm_unavailable_since = datetime(2026, 2, 15, 12, 0)
        assert runner.is_llm_unavailable is True
        runner._llm_unavailable_since = None
        assert runner.is_llm_unavailable is False


# ── daemon._dispatch_inline 把信号翻译成 LLMUnavailableError ──


class TestDaemonInlineDispatch:
    def _daemon_with_runner(self, runner):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from src.everbot.cli.daemon import EverBotDaemon
        daemon = EverBotDaemon.__new__(EverBotDaemon)
        daemon.heartbeat_runners = {"a": runner}
        daemon._run_runner_with_options = AsyncMock(return_value=None)
        return daemon

    def test_raises_when_runner_llm_unavailable(self):
        from types import SimpleNamespace
        runner = SimpleNamespace(is_llm_unavailable=True)
        daemon = self._daemon_with_runner(runner)
        with pytest.raises(LLMUnavailableError):
            asyncio.run(daemon._dispatch_inline("a"))

    def test_no_raise_when_runner_available(self):
        from types import SimpleNamespace
        runner = SimpleNamespace(is_llm_unavailable=False)
        daemon = self._daemon_with_runner(runner)
        asyncio.run(daemon._dispatch_inline("a"))  # must not raise

    def test_missing_runner_is_noop(self):
        daemon = self._daemon_with_runner(None)
        daemon.heartbeat_runners = {}
        asyncio.run(daemon._dispatch_inline("nonexistent"))  # must not raise


# ── 方向 3: probe 冷却 ─────────────────────────────────────────


class TestProbeCooldown:
    def _runner_with_client(self, tmp_path, complete_result="ok", side_effect=None):
        from unittest.mock import AsyncMock
        runner = _make_hb_runner(tmp_path)
        mock_client = AsyncMock()
        if side_effect is not None:
            mock_client.complete = AsyncMock(side_effect=side_effect)
        else:
            mock_client.complete = AsyncMock(return_value=complete_result)
        runner._create_skill_llm_client = lambda: mock_client
        return runner, mock_client

    def test_no_cooldown_when_llm_available(self, tmp_path):
        """When not known-down, every probe hits the network."""
        runner, client = self._runner_with_client(tmp_path)
        assert asyncio.run(runner._probe_llm()) is True
        assert client.complete.await_count == 1

    def test_cooldown_skips_network_when_recently_failed(self, tmp_path):
        """While known-down and within cooldown, probe returns False with no network call."""
        runner, client = self._runner_with_client(tmp_path)
        runner._llm_unavailable_since = datetime.now()
        runner._last_probe_at = datetime.now()  # just probed
        assert asyncio.run(runner._probe_llm()) is False
        assert client.complete.await_count == 0  # network skipped

    def test_probes_again_after_cooldown_window(self, tmp_path):
        """Once cooldown elapses, the probe hits the network again."""
        runner, client = self._runner_with_client(tmp_path)
        runner._llm_unavailable_since = datetime.now() - timedelta(minutes=10)
        runner._last_probe_at = datetime.now() - timedelta(seconds=120)  # > 60s default
        assert asyncio.run(runner._probe_llm()) is True
        assert client.complete.await_count == 1

    def test_cooldown_window_configurable(self, tmp_path, monkeypatch):
        """ALFRED_SKILL_LLM_PROBE_COOLDOWN controls the cooldown window."""
        monkeypatch.setenv("ALFRED_SKILL_LLM_PROBE_COOLDOWN", "300")
        runner, client = self._runner_with_client(tmp_path)
        runner._llm_unavailable_since = datetime.now() - timedelta(minutes=10)
        runner._last_probe_at = datetime.now() - timedelta(seconds=120)  # < 300s
        assert asyncio.run(runner._probe_llm()) is False
        assert client.complete.await_count == 0
