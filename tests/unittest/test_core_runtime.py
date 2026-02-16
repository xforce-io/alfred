"""Unit tests for core runtime abstractions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
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
    agent = _DummyAgent(events=[{"type": "delta", "content": "ok"}])
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
    assert scheduler._agent_schedules["alice"].next_heartbeat_at == now + timedelta(minutes=30)


@pytest.mark.asyncio
async def test_scheduler_respects_active_hours():
    """Heartbeat is skipped outside active hours."""
    # 3 AM is outside default (8, 22)
    now = datetime(2026, 2, 12, 3, 0, 0)
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
