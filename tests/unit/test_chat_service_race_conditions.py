"""Targeted race-condition tests for ChatService session bootstrap."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.everbot.core.channel.core_service import ChannelCoreService
from src.everbot.core.session.session import SessionManager
from src.everbot.web.services.chat_service import ChatService


class _BootstrapWebSocket:
    """Minimal WebSocket that disconnects right after bootstrap."""

    def __init__(self, name: str):
        self.name = name
        self.sent: list[dict] = []
        self.accepted = False
        self.closed = False

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def receive_json(self) -> dict:
        await asyncio.sleep(0.01)
        raise RuntimeError(f"{self.name} disconnect")

    async def close(self) -> None:
        self.closed = True


class _FakeContext:
    """Context stub required by session restore/bootstrap paths."""

    def __init__(self):
        self._vars = {
            "workspace_instructions": "Test workspace instructions.",
            "model_name": "gpt-4",
        }

    def get_var_value(self, name: str):
        return self._vars.get(name)

    def set_variable(self, name: str, value) -> None:
        self._vars[name] = value

    def init_trajectory(self, path: str, overwrite: bool = True) -> None:  # noqa: ARG002
        trajectory_path = Path(path)
        trajectory_path.parent.mkdir(parents=True, exist_ok=True)
        if overwrite or not trajectory_path.exists():
            trajectory_path.write_text("{}", encoding="utf-8")

    def set_session_id(self, session_id: str) -> None:
        self._vars["session_id"] = session_id


class _FakeSnapshot:
    """Snapshot stub required by SessionManager."""

    def __init__(self, context: _FakeContext):
        self._context = context

    def export_portable_session(self) -> dict:
        return {"history_messages": [], "variables": dict(self._context._vars)}

    def import_portable_session(self, state: dict, repair: bool = False, trusted: bool = False) -> dict:  # noqa: ARG002
        return state


class _FakeAgent:
    """Agent stub used for bootstrap-only concurrency tests."""

    def __init__(self, name: str):
        self.name = name
        self.executor = SimpleNamespace(context=_FakeContext())
        self.snapshot = _FakeSnapshot(self.executor.context)
        self.state = None


def _make_user_data(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        sessions_dir=tmp_path,
        get_session_trajectory_path=lambda agent_name, session_id: tmp_path / f"{agent_name}_{session_id}.jsonl",
    )


@pytest.mark.asyncio
async def test_same_session_concurrent_bootstrap_reuses_single_agent(tmp_path: Path):
    """Concurrent bootstrap on the same session should create one shared agent."""
    service = ChatService.__new__(ChatService)
    service._active_connections = {}
    service._connections_by_agent = {}
    service._last_activity = {}
    service._last_agent_broadcast = {}
    service.user_data = _make_user_data(tmp_path)
    service.session_manager = SessionManager(tmp_path)

    create_calls = 0
    created_names: list[str] = []
    create_lock = asyncio.Lock()

    async def create_agent_instance(agent_name: str):
        nonlocal create_calls
        async with create_lock:
            create_calls += 1
            current = create_calls
        await asyncio.sleep(0.05)
        agent = _FakeAgent(f"{agent_name}-{current}")
        created_names.append(agent.name)
        return agent

    service.agent_service = SimpleNamespace(create_agent_instance=create_agent_instance)
    service._core = ChannelCoreService(service.session_manager, service.agent_service, service.user_data)

    ws1 = _BootstrapWebSocket("ws1")
    ws2 = _BootstrapWebSocket("ws2")
    session_id = "web_session_demo_agent"

    await asyncio.gather(
        service.handle_chat_session(ws1, "demo_agent", requested_session_id=session_id),
        service.handle_chat_session(ws2, "demo_agent", requested_session_id=session_id),
    )

    cached = service.session_manager.get_cached_agent(session_id)
    assert create_calls == 1
    assert created_names == ["demo_agent-1"]
    assert getattr(cached, "name", None) == "demo_agent-1"
