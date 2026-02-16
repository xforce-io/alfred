"""
Unit tests for ChannelCoreService.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from dolphin.core.agent.agent_state import AgentState
from dolphin.core.common.constants import KEY_HISTORY

from src.everbot.core.channel.core_service import ChannelCoreService
from src.everbot.core.channel.models import OutboundMessage


def _make_session_manager_mock():
    """Create a session_manager mock with all required attributes."""
    timeline_events = []

    def append_timeline_event(_sid, event):
        timeline_events.append(dict(event))

    class _LockCtx:
        def __enter__(self):
            return True

        def __exit__(self, _exc_type, _exc_val, _exc_tb):
            return False

    _tmp_lock_dir = Path(tempfile.mkdtemp())

    persistence_mock = SimpleNamespace(
        _get_lock_path=lambda session_id: _tmp_lock_dir / f".{session_id}.lock",
    )

    return SimpleNamespace(
        persistence=persistence_mock,
        save_session=AsyncMock(),
        load_session=AsyncMock(return_value=SimpleNamespace(mailbox=[], timeline=[])),
        restore_timeline=lambda sid, timeline: None,
        restore_to_agent=AsyncMock(return_value=None),
        acquire_session=AsyncMock(return_value=True),
        release_session=lambda sid: None,
        file_lock=lambda sid, blocking=False: _LockCtx(),
        ack_mailbox_events=AsyncMock(return_value=True),
        clear_timeline=lambda sid: None,
        append_timeline_event=append_timeline_event,
        get_primary_session_id=lambda agent_name: f"web_session_{agent_name}",
        migrate_legacy_sessions_for_agent=AsyncMock(return_value=False),
        _timeline_events=timeline_events,
    )


def _make_user_data_mock(sessions_dir: Path):
    """Create a user_data mock with sessions_dir."""
    def _get_session_trajectory_path(agent_name: str, session_id: str) -> Path:
        return sessions_dir / f"{agent_name}_{session_id}.jsonl"

    return SimpleNamespace(
        sessions_dir=sessions_dir,
        get_session_trajectory_path=_get_session_trajectory_path,
    )


class _DummyContext:
    def __init__(self):
        self._vars = {"workspace_instructions": "Test workspace instructions."}

    def get_var_value(self, name: str):
        return self._vars.get(name)

    def set_variable(self, _name: str, _value):
        self._vars[_name] = _value
        return None

    def init_trajectory(self, _path: str, overwrite: bool = False):  # noqa: ARG002
        return None


class _DummyAgent:
    name = "dummy_agent"

    def __init__(self, events):
        self._events = events
        self.executor = SimpleNamespace(context=_DummyContext())
        self.state = AgentState.INITIALIZED

    async def continue_chat(self, **_kwargs):
        for event in self._events:
            yield event


class _CallPathAgent:
    name = "dummy_agent"

    def __init__(self, history_messages):
        ctx = _DummyContext()
        ctx.set_variable(KEY_HISTORY, history_messages)
        self.executor = SimpleNamespace(context=ctx)
        self.state = AgentState.INITIALIZED
        self.arun_calls = []
        self.continue_calls = []

    async def arun(self, **kwargs):
        self.arun_calls.append(kwargs)
        yield {"_progress": [{"id": "p1", "status": "running", "stage": "llm", "delta": "OK"}]}

    async def continue_chat(self, **kwargs):
        self.continue_calls.append(kwargs)
        yield {"_progress": [{"id": "p1", "status": "running", "stage": "llm", "delta": "OK"}]}


class _FailingAgent:
    name = "dummy_agent"

    def __init__(self):
        self.executor = SimpleNamespace(context=_DummyContext())
        self.state = AgentState.INITIALIZED

    async def continue_chat(self, **_kwargs):
        raise RuntimeError("boom")
        yield {}  # pragma: no cover


def _make_core_service(tmp_path: Path):
    """Create a ChannelCoreService with mocked dependencies."""
    sm = _make_session_manager_mock()
    ud = _make_user_data_mock(tmp_path)
    core = ChannelCoreService.__new__(ChannelCoreService)
    core.session_manager = sm
    core.user_data = ud
    core.agent_service = None
    return core


class _EventCollector:
    """Collects OutboundMessage events for assertions."""

    def __init__(self):
        self.events: list[OutboundMessage] = []

    async def __call__(self, msg: OutboundMessage):
        self.events.append(msg)

    def payloads_by_type(self, msg_type: str) -> list[OutboundMessage]:
        return [e for e in self.events if e.msg_type == msg_type]

    @property
    def last(self) -> OutboundMessage:
        return self.events[-1]


@pytest.mark.asyncio
async def test_process_message_calls_on_event_with_delta():
    """LLM_DELTA events are delivered as delta OutboundMessages."""
    agent = _DummyAgent(
        events=[
            {"workspace_instructions": "spam"},
            {"model_name": "qwen-plus"},
            {"_progress": [{"id": "p1", "status": "running", "stage": "llm", "delta": "Hi"}]},
        ]
    )

    with tempfile.TemporaryDirectory() as tmp:
        core = _make_core_service(Path(tmp))
        collector = _EventCollector()

        await core.process_message(agent, "demo_agent", "web_session_demo_agent", "hi", collector)

    deltas = collector.payloads_by_type("delta")
    assert any(d.content == "Hi" for d in deltas)
    assert any(
        event.get("source_type") == "chat_user" and event.get("run_id")
        for event in core.session_manager._timeline_events
    )


@pytest.mark.asyncio
async def test_process_message_calls_on_event_with_end():
    """Turn completion sends an end OutboundMessage."""
    agent = _DummyAgent(
        events=[
            {"_progress": [{"id": "p1", "status": "running", "stage": "llm", "delta": "Hi"}]},
        ]
    )

    with tempfile.TemporaryDirectory() as tmp:
        core = _make_core_service(Path(tmp))
        collector = _EventCollector()

        await core.process_message(agent, "demo_agent", "web_session_demo_agent", "hi", collector)

    ends = collector.payloads_by_type("end")
    assert len(ends) >= 1


@pytest.mark.asyncio
async def test_process_message_busy_when_lock_fails():
    """When acquire_session returns False, a busy status + end is sent."""
    agent = _DummyAgent(events=[])

    with tempfile.TemporaryDirectory() as tmp:
        core = _make_core_service(Path(tmp))
        core.session_manager.acquire_session = AsyncMock(return_value=False)
        collector = _EventCollector()

        await core.process_message(agent, "demo_agent", "web_session_demo_agent", "hi", collector)

    statuses = collector.payloads_by_type("status")
    assert any("繁忙" in s.content for s in statuses)
    assert collector.last.msg_type == "end"


@pytest.mark.asyncio
async def test_process_message_error_sends_error_outbound():
    """When run_turn raises, error + end OutboundMessages are sent."""
    agent = _FailingAgent()

    with tempfile.TemporaryDirectory() as tmp:
        core = _make_core_service(Path(tmp))
        collector = _EventCollector()

        await core.process_message(agent, "demo_agent", "web_session_demo_agent", "hi", collector)

    texts = collector.payloads_by_type("text")
    assert any("本轮执行遇到错误" in t.content for t in texts)
    assert collector.last.msg_type == "end"


@pytest.mark.asyncio
async def test_process_message_saves_session_after_turn():
    """save_session is called after a successful turn."""
    agent = _DummyAgent(
        events=[
            {"_progress": [{"id": "p1", "status": "running", "stage": "llm", "delta": "Hi"}]},
        ]
    )

    with tempfile.TemporaryDirectory() as tmp:
        core = _make_core_service(Path(tmp))
        collector = _EventCollector()

        await core.process_message(agent, "demo_agent", "web_session_demo_agent", "hi", collector)

    core.session_manager.save_session.assert_awaited()


@pytest.mark.asyncio
async def test_process_message_acks_mailbox():
    """Mailbox events with ack_ids are acknowledged after turn."""
    agent = _DummyAgent(
        events=[
            {"_progress": [{"id": "p1", "status": "running", "stage": "llm", "delta": "Hi"}]},
        ]
    )

    with tempfile.TemporaryDirectory() as tmp:
        core = _make_core_service(Path(tmp))
        # Setup session with mailbox events
        core.session_manager.load_session = AsyncMock(return_value=SimpleNamespace(
            mailbox=[
                {"event_id": "evt1", "detail": "background task done", "source_agent": "bg"},
            ],
            timeline=[],
        ))
        collector = _EventCollector()

        await core.process_message(agent, "demo_agent", "web_session_demo_agent", "hi", collector)

    core.session_manager.ack_mailbox_events.assert_awaited()
