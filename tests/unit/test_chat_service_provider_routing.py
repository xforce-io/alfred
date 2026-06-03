"""Guard: ChatService web path must dispatch provider ops via provider_for(agent).

chat_service.handle_chat_session has 7 agent-relative provider operations
(get_variable ×2 during bootstrap, interrupt/resume/is_user_interrupt_paused in
the message loop). These MUST route through provider_for(agent) — per-agent type
dispatch — not the global get_provider(), otherwise a MilkieAgentHandle crashes
with AttributeError (or a dolphin agent meets a milkie global) at the operation
layer. This mirrors the *MilkieSafe suites elsewhere.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.everbot.core.channel.core_service import ChannelCoreService
from src.everbot.core.session.session import SessionManager
import src.everbot.web.services.chat_service as cs_mod
from src.everbot.web.services.chat_service import ChatService


class _FakeContext:
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
    def __init__(self, context: _FakeContext):
        self._context = context

    def export_portable_session(self) -> dict:
        return {"history_messages": [], "variables": dict(self._context._vars)}

    def import_portable_session(self, state: dict, repair: bool = False, trusted: bool = False) -> dict:  # noqa: ARG002
        return state


class _FakeAgent:
    """Stands in for a MilkieAgentHandle: deliberately has NO .executor attribute
    used directly by chat_service (it routes everything through the provider)."""

    def __init__(self, name: str):
        self.name = name
        self.executor = SimpleNamespace(context=_FakeContext())
        self.snapshot = _FakeSnapshot(self.executor.context)
        self.state = None


class _SpyProvider:
    """Records the agent object passed to each operation."""

    def __init__(self):
        self.get_variable_agents: list = []
        self.is_paused_agents: list = []
        self.resume_agents: list = []
        self.interrupt_agents: list = []
        self.paused = True  # force Case 1 (paused) so resume() fires

    def get_variable(self, agent, key):
        self.get_variable_agents.append((agent, key))
        return "WS" if key == "workspace_instructions" else "model"

    def is_user_interrupt_paused(self, agent) -> bool:
        self.is_paused_agents.append(agent)
        return self.paused

    async def resume(self, agent, message) -> None:
        self.resume_agents.append((agent, message))

    async def interrupt(self, agent) -> None:
        self.interrupt_agents.append(agent)


class _ScriptedWebSocket:
    """Sends one user message, then disconnects."""

    def __init__(self):
        self.sent: list[dict] = []
        self.accepted = False
        self.closed = False
        self._messages = [{"message": "hello"}]

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def receive_json(self) -> dict:
        await asyncio.sleep(0.01)
        if self._messages:
            return self._messages.pop(0)
        raise RuntimeError("disconnect")

    async def close(self) -> None:
        self.closed = True


def _make_user_data(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        sessions_dir=tmp_path,
        get_session_trajectory_path=lambda agent_name, session_id: tmp_path / f"{agent_name}_{session_id}.jsonl",
    )


def _make_service(tmp_path: Path, agent: _FakeAgent) -> ChatService:
    service = ChatService.__new__(ChatService)
    service._active_connections = {}
    service._connections_by_agent = {}
    service._last_activity = {}
    service._last_agent_broadcast = {}
    service._bootstrap_locks = {}
    service.user_data = _make_user_data(tmp_path)
    service.session_manager = SessionManager(tmp_path)

    async def create_agent_instance(agent_name: str):  # noqa: ARG001
        return agent

    service.agent_service = SimpleNamespace(create_agent_instance=create_agent_instance)
    service._core = ChannelCoreService(service.session_manager, service.agent_service, service.user_data)
    return service


@pytest.mark.asyncio
async def test_web_path_dispatches_via_provider_for_agent(tmp_path: Path, monkeypatch):
    """Bootstrap get_variable + message-loop is_user_interrupt_paused/resume must all
    receive THE agent object via provider_for(agent), not the global get_provider()."""
    agent = _FakeAgent("milkie-handle")
    spy = _SpyProvider()

    seen_agents: list = []

    def fake_provider_for(a):
        seen_agents.append(a)
        return spy

    # Patch the seam the source now references. If the source still called
    # get_provider(), this patch would have no effect and the asserts below fail.
    monkeypatch.setattr(cs_mod, "provider_for", fake_provider_for)

    # Make _process_message a no-op so the turn does not require a real backend.
    async def _noop_process(self, websocket, agent_, agent_name, session_id, message):  # noqa: ARG001
        return None

    monkeypatch.setattr(ChatService, "_process_message", _noop_process)

    service = _make_service(tmp_path, agent)
    ws = _ScriptedWebSocket()

    await service.handle_chat_session(ws, "demo_agent", requested_session_id="web_session_demo_agent")

    # provider_for was invoked with the exact agent object (proves type dispatch).
    assert agent in seen_agents
    # Bootstrap get_variable routed through the spy with the agent.
    assert any(a is agent and key == "workspace_instructions" for a, key in spy.get_variable_agents)
    assert any(a is agent and key == "model_name" for a, key in spy.get_variable_agents)
    # Message loop: paused-case check + resume both got the agent via provider_for.
    assert agent in spy.is_paused_agents
    assert any(a is agent and msg == "hello" for a, msg in spy.resume_agents)


@pytest.mark.asyncio
async def test_message_loop_seam_is_provider_for_not_get_provider(tmp_path: Path, monkeypatch):
    """Count provider_for invocations to prove the MESSAGE LOOP (post-bootstrap)
    routes through provider_for(agent). If the loop still called the global
    get_provider(), provider_for would be invoked only during bootstrap and the
    post-bootstrap counter would stay at zero."""
    agent = _FakeAgent("milkie-handle")
    spy = _SpyProvider()

    calls = {"bootstrap_done": False, "post_bootstrap": 0}

    def counting_provider_for(a):  # noqa: ARG001
        if calls["bootstrap_done"]:
            calls["post_bootstrap"] += 1
        return spy

    # Bootstrap consumes the two get_variable calls; flip the flag once they ran.
    real_get_variable = spy.get_variable

    def get_variable(a, key):
        if key == "model_name":
            calls["bootstrap_done"] = True
        return real_get_variable(a, key)

    spy.get_variable = get_variable
    monkeypatch.setattr(cs_mod, "provider_for", counting_provider_for)

    async def _noop_process(self, *a, **k):  # noqa: ARG001, ARG002
        return None

    monkeypatch.setattr(ChatService, "_process_message", _noop_process)

    service = _make_service(tmp_path, agent)
    ws = _ScriptedWebSocket()

    await service.handle_chat_session(ws, "demo_agent", requested_session_id="web_session_demo_agent")

    # The single user message hits provider_for(agent) at least once in the loop
    # (is_user_interrupt_paused). Zero would mean the loop bypassed provider_for.
    assert calls["post_bootstrap"] >= 1
    assert agent in spy.is_paused_agents
