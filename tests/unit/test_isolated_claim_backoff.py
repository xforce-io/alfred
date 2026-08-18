"""Tests for #202: isolated claim backoff when the LLM is unavailable.

#78 backed off the inline path. Isolated claim still returned False on every
1s tick, flooding logs. The scheduler must gate isolated work per agent until
the next retry, and recover within 2 minutes after the probe is healthy.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from src.everbot.core.jobs.llm_errors import LLMUnavailableError
from src.everbot.core.runtime.scheduler import IsolatedSchedule, Scheduler, SchedulerTask


def _due_isolated(*task_ids: str, agent: str = "demo_agent"):
    def _get_due(ts):
        return [
            SchedulerTask(id=tid, agent_name=agent, execution_mode="isolated")
            for tid in task_ids
        ]

    return _get_due


class TestIsolatedClaimBackoff:
    def test_llm_unavailable_claim_backs_off(self):
        claims = []

        async def _claim(task_id):
            claims.append(task_id)
            raise LLMUnavailableError("llm down")

        async def _run(task, ts):
            raise AssertionError("must not run while LLM is down")

        schedule = IsolatedSchedule(
            agent_name="demo_agent",
            base_interval_minutes=1,
            max_backoff_minutes=2,
        )
        scheduler = Scheduler(
            get_due_tasks=_due_isolated("demo_agent:t1"),
            claim_task=_claim,
            run_isolated=_run,
            isolated_schedules={"demo_agent": schedule},
        )
        ts = datetime(2026, 8, 18, 8, 11, tzinfo=timezone.utc)
        asyncio.run(scheduler._tick_tasks(ts))
        assert claims == ["demo_agent:t1"]
        assert schedule.consecutive_failures == 1
        assert schedule.next_isolated_at == ts + timedelta(minutes=2)

    def test_backoff_gates_claim_until_next_retry(self):
        claims = []

        async def _claim(task_id):
            claims.append(task_id)
            raise LLMUnavailableError("llm down")

        async def _run(task, ts):
            return None

        ts = datetime(2026, 8, 18, 8, 11, tzinfo=timezone.utc)
        schedule = IsolatedSchedule(
            agent_name="demo_agent",
            next_isolated_at=ts + timedelta(minutes=2),
            consecutive_failures=1,
            max_backoff_minutes=2,
        )
        scheduler = Scheduler(
            get_due_tasks=_due_isolated("demo_agent:t1"),
            claim_task=_claim,
            run_isolated=_run,
            isolated_schedules={"demo_agent": schedule},
        )
        asyncio.run(scheduler._tick_tasks(ts + timedelta(seconds=30)))
        assert claims == []
        assert schedule.consecutive_failures == 1

    def test_agent_gate_covers_all_due_isolated_tasks(self):
        claims = []

        async def _claim(task_id):
            claims.append(task_id)
            raise LLMUnavailableError("llm down")

        async def _run(task, ts):
            return None

        schedule = IsolatedSchedule(agent_name="demo_agent", max_backoff_minutes=2)
        scheduler = Scheduler(
            get_due_tasks=_due_isolated("demo_agent:gray", "demo_agent:serenity"),
            claim_task=_claim,
            run_isolated=_run,
            isolated_schedules={"demo_agent": schedule},
        )
        ts = datetime(2026, 8, 18, 8, 11, tzinfo=timezone.utc)
        asyncio.run(scheduler._tick_tasks(ts))
        assert claims == ["demo_agent:gray"]
        assert schedule.next_isolated_at is not None

    def test_false_claim_does_not_backoff(self):
        claims = []

        async def _claim(task_id):
            claims.append(task_id)
            return False

        async def _run(task, ts):
            return None

        schedule = IsolatedSchedule(agent_name="demo_agent")
        scheduler = Scheduler(
            get_due_tasks=_due_isolated("demo_agent:t1"),
            claim_task=_claim,
            run_isolated=_run,
            isolated_schedules={"demo_agent": schedule},
        )
        ts = datetime(2026, 8, 18, 8, 11, tzinfo=timezone.utc)
        asyncio.run(scheduler._tick_tasks(ts))
        asyncio.run(scheduler._tick_tasks(ts + timedelta(seconds=1)))
        assert claims == ["demo_agent:t1", "demo_agent:t1"]
        assert schedule.consecutive_failures == 0
        assert schedule.next_isolated_at is None

    def test_success_clears_backoff(self):
        async def _claim(task_id):
            return True

        ran = []

        async def _run(task, ts):
            ran.append(task.id)

        schedule = IsolatedSchedule(
            agent_name="demo_agent",
            consecutive_failures=3,
            next_isolated_at=None,
        )
        scheduler = Scheduler(
            get_due_tasks=_due_isolated("demo_agent:t1"),
            claim_task=_claim,
            run_isolated=_run,
            isolated_schedules={"demo_agent": schedule},
        )
        ts = datetime(2026, 8, 18, 8, 40, tzinfo=timezone.utc)
        asyncio.run(scheduler._tick_tasks(ts))
        assert ran == ["demo_agent:t1"]
        assert schedule.consecutive_failures == 0
        assert schedule.next_isolated_at is None

    def test_other_agent_not_gated(self):
        claims = []

        async def _claim(task_id):
            claims.append(task_id)
            if task_id.startswith("down:"):
                raise LLMUnavailableError("llm down")
            return True

        ran = []

        async def _run(task, ts):
            ran.append(task.id)

        def _get_due(ts):
            return [
                SchedulerTask(id="down:t1", agent_name="down", execution_mode="isolated"),
                SchedulerTask(id="up:t1", agent_name="up", execution_mode="isolated"),
            ]

        scheduler = Scheduler(
            get_due_tasks=_get_due,
            claim_task=_claim,
            run_isolated=_run,
        )
        ts = datetime(2026, 8, 18, 8, 11, tzinfo=timezone.utc)
        asyncio.run(scheduler._tick_tasks(ts))
        assert "down:t1" in claims
        assert ran == ["up:t1"]

    def test_backoff_capped_at_two_minutes(self):
        async def _claim(task_id):
            raise LLMUnavailableError("llm down")

        async def _run(task, ts):
            return None

        schedule = IsolatedSchedule(
            agent_name="demo_agent",
            base_interval_minutes=1,
            max_backoff_minutes=2,
            consecutive_failures=99,
        )
        scheduler = Scheduler(
            get_due_tasks=_due_isolated("demo_agent:t1"),
            claim_task=_claim,
            run_isolated=_run,
            isolated_schedules={"demo_agent": schedule},
        )
        ts = datetime(2026, 8, 18, 8, 11, tzinfo=timezone.utc)
        asyncio.run(scheduler._tick_tasks(ts))
        assert schedule.next_isolated_at - ts <= timedelta(minutes=2, seconds=1)

    def test_ten_minute_window_claim_attempts_at_most_ten(self):
        """S1: 10 minutes of 1s ticks, each due task is claimed ≤ 10 times."""
        claims = []

        async def _claim(task_id):
            claims.append(task_id)
            raise LLMUnavailableError("llm down")

        async def _run(task, ts):
            return None

        scheduler = Scheduler(
            get_due_tasks=_due_isolated("demo_agent:gray", "demo_agent:serenity"),
            claim_task=_claim,
            run_isolated=_run,
        )
        t0 = datetime(2026, 8, 18, 8, 11, tzinfo=timezone.utc)

        async def _drive():
            for i in range(601):
                await scheduler.tick(t0 + timedelta(seconds=i))

        asyncio.run(_drive())
        assert claims.count("demo_agent:gray") <= 10
        assert claims.count("demo_agent:serenity") <= 10

    def test_recovers_within_two_minutes_after_probe_up(self):
        """S2: after LLM recovers, the next claim/run happens within 2 minutes."""
        llm_down = True
        ran = []

        async def _claim(task_id):
            if llm_down:
                raise LLMUnavailableError("llm down")
            return True

        async def _run(task, ts):
            ran.append(ts)

        scheduler = Scheduler(
            get_due_tasks=_due_isolated("demo_agent:t1"),
            claim_task=_claim,
            run_isolated=_run,
        )
        t0 = datetime(2026, 8, 18, 8, 11, tzinfo=timezone.utc)
        asyncio.run(scheduler.tick(t0))
        assert ran == []

        llm_down = False
        recovered_at = t0 + timedelta(seconds=10)
        deadline = recovered_at + timedelta(minutes=2)

        async def _drive():
            ts = recovered_at
            while ts <= deadline:
                await scheduler.tick(ts)
                if ran:
                    return
                ts += timedelta(seconds=1)

        asyncio.run(_drive())
        assert ran, "isolated task did not start within 2 minutes of probe recovery"
        assert ran[0] <= deadline

    def test_state_persistence_round_trip(self, tmp_path):
        state_file = tmp_path / "scheduler_state.json"
        nxt = datetime(2026, 8, 18, 8, 13, tzinfo=timezone.utc)
        schedule = IsolatedSchedule(
            agent_name="demo_agent",
            consecutive_failures=2,
            next_isolated_at=nxt,
        )
        Scheduler(
            isolated_schedules={"demo_agent": schedule},
            state_file=state_file,
        )._save_state()

        restored = Scheduler(state_file=state_file)
        sched2 = restored._get_isolated_schedule("demo_agent")
        assert sched2.consecutive_failures == 2
        assert sched2.next_isolated_at == nxt


class TestDaemonIsolatedClaimRaises:
    def _build(self, runner, tmp_path):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from src.everbot.cli.daemon import EverBotDaemon

        daemon = EverBotDaemon.__new__(EverBotDaemon)
        daemon.heartbeat_runners = {"a": runner}
        daemon._scheduler_cron_jobs = True
        daemon._scheduler_run_heartbeat = AsyncMock()
        daemon.user_data = SimpleNamespace(alfred_home=tmp_path)
        return daemon._build_scheduler()

    def test_claim_raises_when_llm_unavailable(self, tmp_path):
        from datetime import datetime
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        import pytest

        runner = SimpleNamespace(
            is_llm_unavailable=True,
            claim_isolated_task=AsyncMock(return_value=True),
            execute_isolated_claimed_task=AsyncMock(),
            list_due_inline_tasks=lambda now=None: [],
            list_due_isolated_tasks=lambda now=None: [{"id": "t1", "timeout_seconds": 30}],
            interval_minutes=30,
            night_interval_minutes=None,
            active_hours=(0, 24),
            inspect_interval_minutes=30,
            inspect_night_interval_minutes=None,
        )
        sched = self._build(runner, tmp_path)
        list(sched._get_due_tasks(datetime.now()) or [])
        with pytest.raises(LLMUnavailableError):
            asyncio.run(sched._claim_task("a:t1"))
        runner.claim_isolated_task.assert_not_awaited()
