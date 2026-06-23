"""Unit tests for core runtime abstractions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest

from src.everbot.core.runtime import (
    AgentSchedule,
    RuntimeDeps,
    Scheduler,
    SchedulerTask,
    TurnExecutor,
)


@dataclass
class _Session:
    session_id: str
    agent_name: str
    session_type: str
    mailbox: List[Dict[str, Any]] = field(default_factory=list)
    variables: Dict[str, Any] = field(default_factory=dict)


def _make_llm_event(delta: str = "", answer: str = "") -> Dict[str, Any]:
    """Build a raw dolphin-style event with an LLM progress entry."""
    return {"_progress": [{"stage": "llm", "delta": delta, "answer": answer}]}


def _make_tool_call_event(name: str = "_bash", args: str = "ls", pid: str = "p1") -> Dict[str, Any]:
    return {"_progress": [{"stage": "tool_call", "tool_name": name, "args": args, "id": pid, "status": "running"}]}


def _make_tool_output_event(output: str = "ok", pid: str = "p1") -> Dict[str, Any]:
    return {"_progress": [{"stage": "tool_output", "tool_name": "_bash", "output": output, "id": pid, "status": "completed"}]}


class _DummyAgent:
    def __init__(self, events: List[Dict[str, Any]]):
        self._events = events
        self.calls: List[Dict[str, Any]] = []

    async def continue_chat(self, **kwargs):
        self.calls.append(dict(kwargs))
        for event in self._events:
            yield event


# ---------------------------------------------------------------------------
# TurnExecutor tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_executor_primary_strategy_builds_mailbox_message_and_acks():
    session = _Session(
        session_id="web_session_demo",
        agent_name="demo",
        session_type="primary",
        mailbox=[
            {
                "event_id": "evt_1",
                "event_type": "heartbeat_result",
                "summary": "background update",
            }
        ],
    )
    agent = _DummyAgent(events=[{"type": "delta", "content": "ok"}])
    ack_calls: list[tuple[str, list[str]]] = []
    save_calls: list[str] = []

    deps = RuntimeDeps(
        load_workspace_instructions=lambda agent_name: f"SYS:{agent_name}",
        heartbeat_instructions="HB",
    )
    executor = TurnExecutor(deps)

    async def _load_session(_sid: str):
        return session

    async def _get_or_create(_session: Any):
        return agent

    async def _save_session(sid: str, _agent: Any):
        save_calls.append(sid)

    async def _ack(sid: str, ids: list[str]):
        ack_calls.append((sid, list(ids)))

    result = await executor.execute_turn(
        session_id=session.session_id,
        trigger="hello",
        load_session=_load_session,
        get_or_create_agent=_get_or_create,
        save_session=_save_session,
        ack_mailbox_events=_ack,
    )

    assert result.session_id == session.session_id
    assert len(result.events) == 1
    assert save_calls == [session.session_id]
    assert ack_calls == [(session.session_id, ["evt_1"])]
    assert agent.calls, "Expected continue_chat to be called."
    assert "## Background Updates" in agent.calls[0]["message"]
    assert agent.calls[0]["system_prompt"] == "SYS:demo"


@pytest.mark.asyncio
async def test_turn_executor_heartbeat_strategy_includes_due_tasks():
    session = _Session(
        session_id="heartbeat_session_demo",
        agent_name="demo",
        session_type="heartbeat",
    )
    agent = _DummyAgent(events=[_make_llm_event(delta="done")])
    deps = RuntimeDeps(
        load_workspace_instructions=lambda agent_name: f"SYS:{agent_name}",
        list_due_tasks=lambda _agent_name: [{"id": "t1", "title": "Task", "description": "desc"}],
        heartbeat_instructions="HB_INSTR",
    )
    executor = TurnExecutor(deps)

    async def _load_session(_sid: str):
        return session

    async def _get_or_create(_session: Any):
        return agent

    async def _save_session(_sid: str, _agent: Any):
        return None

    await executor.execute_turn(
        session_id=session.session_id,
        trigger="tick",
        load_session=_load_session,
        get_or_create_agent=_get_or_create,
        save_session=_save_session,
    )

    assert agent.calls, "Expected continue_chat to be called."
    assert "## Due Tasks" in agent.calls[0]["message"]
    assert "[t1] Task: desc" in agent.calls[0]["message"]
    assert "HB_INSTR" in agent.calls[0]["system_prompt"]


@pytest.mark.asyncio
async def test_turn_executor_heartbeat_enforces_tool_budget():
    """Heartbeat turn stops after exceeding HEARTBEAT_POLICY.max_tool_calls."""
    session = _Session(
        session_id="heartbeat_session_demo",
        agent_name="demo",
        session_type="heartbeat",
    )
    # Generate 20 tool call rounds (exceeds heartbeat max_tool_calls=10)
    events: List[Dict[str, Any]] = []
    for i in range(20):
        pid = f"p{i}"
        events.append(_make_tool_call_event(name="_bash", args=f"cmd_{i}", pid=pid))
        events.append(_make_tool_output_event(output=f"output_{i}", pid=pid))
        events.append(_make_llm_event(delta=f"round {i} "))

    agent = _DummyAgent(events=events)
    deps = RuntimeDeps(
        load_workspace_instructions=lambda _: "SYS",
        heartbeat_instructions="HB",
    )
    executor = TurnExecutor(deps)

    async def _load_session(_sid: str):
        return session

    async def _get_or_create(_session: Any):
        return agent

    saved: list[str] = []

    async def _save_session(_sid: str, _agent: Any):
        saved.append(_sid)

    result = await executor.execute_turn(
        session_id=session.session_id,
        trigger="tick",
        load_session=_load_session,
        get_or_create_agent=_get_or_create,
        save_session=_save_session,
    )

    # Should have a _turn_error event from budget exceeded
    error_events = [e for e in result.events if "_turn_error" in e]
    assert error_events, "Expected TOOL_CALL_BUDGET_EXCEEDED error"
    assert "TOOL_CALL_BUDGET_EXCEEDED" in error_events[0]["_turn_error"]
    # Session should still be saved even after error
    assert saved == [session.session_id]


@pytest.mark.asyncio
async def test_turn_executor_primary_bypasses_orchestrator():
    """Primary sessions stream raw events without TurnOrchestrator wrapping."""
    session = _Session(
        session_id="web_session_demo",
        agent_name="demo",
        session_type="primary",
        mailbox=[],
    )
    raw_event = {"type": "delta", "content": "hello"}
    agent = _DummyAgent(events=[raw_event])
    deps = RuntimeDeps(load_workspace_instructions=lambda _: "SYS")
    executor = TurnExecutor(deps)

    async def _load_session(_sid: str):
        return session

    async def _get_or_create(_session: Any):
        return agent

    async def _save_session(_sid: str, _agent: Any):
        return None

    result = await executor.execute_turn(
        session_id=session.session_id,
        trigger="hi",
        load_session=_load_session,
        get_or_create_agent=_get_or_create,
        save_session=_save_session,
    )

    # Primary sessions pass through raw events unchanged
    assert result.events == [raw_event]


# ---------------------------------------------------------------------------
# Scheduler tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_splits_inline_and_claims_isolated():
    now = datetime(2026, 2, 12, 12, 0, 0)
    tasks = [
        SchedulerTask(id="i1", agent_name="a1", execution_mode="inline"),
        SchedulerTask(id="i2", agent_name="a1", execution_mode="inline"),
        SchedulerTask(id="j1", agent_name="a1", execution_mode="isolated"),
        SchedulerTask(id="j2", agent_name="a2", execution_mode="isolated"),
    ]
    inline_calls: list[tuple[str, list[str]]] = []
    isolated_calls: list[str] = []
    claim_calls: list[str] = []

    async def _claim_task(task_id: str) -> bool:
        claim_calls.append(task_id)
        return task_id == "j1"

    async def _run_inline(agent_name: str, inline_tasks: list[SchedulerTask], _ts: datetime):
        inline_calls.append((agent_name, [t.id for t in inline_tasks]))

    async def _run_isolated(task: SchedulerTask, _ts: datetime):
        isolated_calls.append(task.id)

    scheduler = Scheduler(
        get_due_tasks=lambda _now: tasks,
        claim_task=_claim_task,
        run_inline=_run_inline,
        run_isolated=_run_isolated,
        tick_interval_seconds=0.01,
    )

    await scheduler.tick(now)

    assert inline_calls == [("a1", ["i1", "i2"])]
    assert claim_calls == ["j1", "j2"]
    assert isolated_calls == ["j1"]


@pytest.mark.asyncio
async def test_scheduler_triggers_heartbeat_on_due_interval():
    """Scheduler fires heartbeat when next_heartbeat_at is in the past or None."""
    now = datetime(2026, 2, 12, 12, 0, 0)
    heartbeat_calls: list[str] = []

    async def _run_heartbeat(agent_name: str, ts: datetime):
        heartbeat_calls.append(agent_name)

    scheduler = Scheduler(
        run_heartbeat=_run_heartbeat,
        agent_schedules={
            "alice": AgentSchedule(agent_name="alice", interval_minutes=30),
            "bob": AgentSchedule(agent_name="bob", interval_minutes=60,
                                 next_heartbeat_at=now + timedelta(minutes=10)),
        },
        tick_interval_seconds=0.01,
    )

    await scheduler.tick(now)

    # Alice has no next_heartbeat_at → should fire.
    # Bob's next is 10 min in the future → should NOT fire.
    assert heartbeat_calls == ["alice"]

    # After alice fires, her next_heartbeat_at should be set
    assert (
        scheduler._agent_schedules["alice"].next_heartbeat_at
        == (now + timedelta(minutes=30)).replace(tzinfo=timezone.utc)
    )


@pytest.mark.asyncio
async def test_scheduler_runs_heartbeat_during_local_active_hours(monkeypatch):
    """During local active hours heartbeat fires — even when the same instant is
    outside active_hours when read in UTC. Complements the night-silence case
    below; together they pin active_hours to local-time semantics (#117)."""
    import time

    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    try:
        # 05:00 UTC == 13:00 Beijing → inside active_hours (8, 22).
        # The pre-fix code compared UTC hour (5) and wrongly skipped.
        now = datetime(2026, 2, 12, 5, 0, 0, tzinfo=timezone.utc)
        heartbeat_calls: list[str] = []

        async def _run_heartbeat(agent_name: str, ts: datetime):
            heartbeat_calls.append(agent_name)

        scheduler = Scheduler(
            run_heartbeat=_run_heartbeat,
            agent_schedules={
                "alice": AgentSchedule(agent_name="alice", interval_minutes=30),
            },
        )

        await scheduler.tick(now)
        assert heartbeat_calls == ["alice"]
    finally:
        time.tzset()


@pytest.mark.asyncio
async def test_scheduler_active_hours_uses_local_timezone(monkeypatch):
    """active_hours is local-time semantics: Beijing 3 AM must be inactive
    even though the same instant is 19:00 UTC (inside default active_hours).

    Regression for #117: _is_active_time compared UTC hour against locally
    configured active_hours, shifting the active window by the UTC offset and
    letting heartbeat/inspector run all through the local night.
    """
    import time

    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    try:
        # 19:00 UTC == 03:00 Beijing next day → outside active_hours (8, 22)
        now = datetime(2026, 2, 12, 19, 0, 0, tzinfo=timezone.utc)
        heartbeat_calls: list[str] = []

        async def _run_heartbeat(agent_name: str, ts: datetime):
            heartbeat_calls.append(agent_name)

        scheduler = Scheduler(
            run_heartbeat=_run_heartbeat,
            agent_schedules={
                "alice": AgentSchedule(agent_name="alice", interval_minutes=30),
            },
        )

        await scheduler.tick(now)
        assert heartbeat_calls == []
    finally:
        time.tzset()


@pytest.mark.parametrize(
    "local_hour,expected_active",
    [
        (7, False),   # just before start → inactive
        (8, True),    # start is inclusive
        (21, True),
        (22, False),  # end is exclusive
        (23, False),
        (3, False),   # deep night
    ],
)
def test_is_active_time_local_half_open_interval(monkeypatch, local_hour, expected_active):
    """active_hours is a half-open [start, end) interval evaluated in local time."""
    import time
    from zoneinfo import ZoneInfo

    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    try:
        schedule = AgentSchedule(agent_name="alice", active_hours=(8, 22))
        ts = datetime(2026, 2, 12, local_hour, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        assert Scheduler._is_active_time(schedule, ts) is expected_active
    finally:
        time.tzset()


@pytest.mark.asyncio
async def test_scheduler_per_task_error_isolation():
    """One isolated task failing doesn't block subsequent tasks."""
    now = datetime(2026, 2, 12, 12, 0, 0)
    tasks = [
        SchedulerTask(id="bad", agent_name="a1", execution_mode="isolated"),
        SchedulerTask(id="good", agent_name="a1", execution_mode="isolated"),
    ]
    executed: list[str] = []

    async def _claim(task_id: str) -> bool:
        return True

    async def _run_isolated(task: SchedulerTask, _ts: datetime):
        if task.id == "bad":
            raise RuntimeError("boom")
        executed.append(task.id)

    scheduler = Scheduler(
        get_due_tasks=lambda _: tasks,
        claim_task=_claim,
        run_isolated=_run_isolated,
    )

    await scheduler.tick(now)
    assert executed == ["good"]


@pytest.mark.asyncio
async def test_scheduler_run_forever_stops():
    """run_forever exits when stop() is called."""
    scheduler = Scheduler(tick_interval_seconds=0.01)

    async def _stop_soon():
        await asyncio.sleep(0.05)
        scheduler.stop()

    import asyncio
    await asyncio.gather(scheduler.run_forever(), _stop_soon())
    assert not scheduler._running


@pytest.mark.asyncio
async def test_scheduler_normalizes_tick_time_to_utc_for_isolated_tasks():
    """Scheduler must pass tz-aware UTC timestamps into isolated task execution.

    Regression: a naive local datetime reached update_task_state(..., now=ts),
    which was then interpreted as UTC and pushed retry windows 8 hours later
    on Asia/Shanghai machines.
    """
    seen_ts: list[datetime] = []

    async def _claim(_task_id: str) -> bool:
        return True

    async def _run_isolated(_task: SchedulerTask, ts: datetime):
        seen_ts.append(ts)

    scheduler = Scheduler(
        get_due_tasks=lambda _now: [
            SchedulerTask(id="j1", agent_name="a1", execution_mode="isolated"),
        ],
        claim_task=_claim,
        run_isolated=_run_isolated,
    )

    await scheduler.tick(datetime(2026, 3, 22, 7, 44, 32))

    assert len(seen_ts) == 1
    assert seen_ts[0].tzinfo == timezone.utc
    assert seen_ts[0].isoformat() == "2026-03-22T07:44:32+00:00"
