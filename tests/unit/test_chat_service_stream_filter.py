"""
Unit tests for ChatService streaming event filtering.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from dolphin.core.agent.agent_state import AgentState
from dolphin.core.common.constants import KEY_HISTORY

from src.everbot.web.services.chat_service import ChatService


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


class _DummyWebSocket:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


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


@pytest.mark.asyncio
async def test_process_message_ignores_non_progress_events():
    websocket = _DummyWebSocket()
    agent = _DummyAgent(
        events=[
            {"workspace_instructions": "spam"},
            {"model_name": "qwen-plus"},
            {"_progress": [{"id": "p1", "status": "running", "stage": "llm", "delta": "Hi"}]},
        ]
    )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        service = ChatService.__new__(ChatService)
        service.session_manager = _make_session_manager_mock()
        service.user_data = _make_user_data_mock(tmp_path)
        service.session_events = {}
        service.current_turn_events = {}

        await service._process_message(websocket, agent, "demo_agent", "web_session_demo_agent", "hi")

    assert {"type": "delta", "content": "Hi"} in websocket.sent
    assert websocket.sent[-1]["type"] == "end"
    assert any(
        event.get("source_type") == "chat_user" and event.get("run_id")
        for event in service.session_manager._timeline_events
    )
    service.session_manager.save_session.assert_awaited()


@pytest.mark.asyncio
async def test_process_message_sends_fallback_message_and_end_on_unhandled_error():
    websocket = _DummyWebSocket()
    agent = _FailingAgent()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        service = ChatService.__new__(ChatService)
        service.session_manager = _make_session_manager_mock()
        service.user_data = _make_user_data_mock(tmp_path)
        service.session_events = {}
        service.current_turn_events = {}

        await service._process_message(websocket, agent, "demo_agent", "web_session_demo_agent", "hi")

    assert any(
        payload.get("type") == "message"
        and "本轮执行遇到错误" in payload.get("content", "")
        for payload in websocket.sent
    )
    assert websocket.sent[-1]["type"] == "end"
    service.session_manager.save_session.assert_awaited()


@pytest.mark.asyncio
async def test_process_message_first_turn_uses_arun_without_system_prompt_override():
    websocket = _DummyWebSocket()
    agent = _CallPathAgent(history_messages=[])

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        service = ChatService.__new__(ChatService)
        service.session_manager = _make_session_manager_mock()
        service.user_data = _make_user_data_mock(tmp_path)
        service.session_events = {}
        service.current_turn_events = {}

        await service._process_message(websocket, agent, "demo_agent", "web_session_demo_agent", "hi")

    # When a message is present, continue_chat is used even on first turn
    # (arun is reserved for daemon-initiated turns with no user message).
    assert len(agent.continue_calls) == 1
    assert len(agent.arun_calls) == 0


@pytest.mark.asyncio
async def test_process_message_followup_turn_uses_continue_chat_with_runtime_system_prompt():
    websocket = _DummyWebSocket()
    agent = _CallPathAgent(history_messages=[{"role": "user", "content": "prev"}])

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        service = ChatService.__new__(ChatService)
        service.session_manager = _make_session_manager_mock()
        service.user_data = _make_user_data_mock(tmp_path)
        service.session_events = {}
        service.current_turn_events = {}

        await service._process_message(websocket, agent, "demo_agent", "web_session_demo_agent", "follow up")

    assert len(agent.arun_calls) == 0
    assert len(agent.continue_calls) == 1
    assert agent.continue_calls[0]["system_prompt"] == "Test workspace instructions."


@pytest.mark.asyncio
async def test_process_message_stops_on_tool_call_budget_exceeded():
    websocket = _DummyWebSocket()
    events = [
        {"_progress": [{"id": f"p{i}", "status": "running", "stage": "tool_call", "tool_name": "_bash", "args": "echo hi"}]}
        for i in range(1, 55)
    ]
    agent = _DummyAgent(events=events)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        service = ChatService.__new__(ChatService)
        service.session_manager = _make_session_manager_mock()
        service.user_data = _make_user_data_mock(tmp_path)
        service.session_events = {}
        service.current_turn_events = {}

        await service._process_message(websocket, agent, "demo_agent", "web_session_demo_agent", "hi")

    assert any(
        payload.get("type") == "message"
        and "工具调用次数过多" in payload.get("content", "")
        for payload in websocket.sent
    )
    assert websocket.sent[-1]["type"] == "end"
    service.session_manager.save_session.assert_awaited()


@pytest.mark.asyncio
async def test_process_message_stops_on_repeated_tool_failures():
    websocket = _DummyWebSocket()
    events = [
        {
            "_progress": [
                {
                    "id": f"p{i}",
                    "status": "done",
                    "stage": "tool_output",
                    "tool_name": "_bash",
                    "args": "curl -I https://example.com",
                    "output": "Command exited with code 35\ncurl: (35) SSL_ERROR_SYSCALL",
                }
            ]
        }
        for i in range(1, 5)
    ]
    agent = _DummyAgent(events=events)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        service = ChatService.__new__(ChatService)
        service.session_manager = _make_session_manager_mock()
        service.user_data = _make_user_data_mock(tmp_path)
        service.session_events = {}
        service.current_turn_events = {}

        await service._process_message(websocket, agent, "demo_agent", "web_session_demo_agent", "hi")

    assert any(
        payload.get("type") == "message"
        and "检测到重复失败" in payload.get("content", "")
        for payload in websocket.sent
    )
    assert websocket.sent[-1]["type"] == "end"
    service.session_manager.save_session.assert_awaited()
