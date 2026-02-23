"""
Integration test: workspace_instructions survives session restore.

When a Telegram/chat session is restored from disk, workspace_instructions is
intentionally filtered out by _NON_RESTORABLE_VARS in persistence.py.  The fix
in core_service.py reloads fresh instructions from disk so the LLM still
receives a proper system prompt.
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


# ---------------------------------------------------------------------------
# Helpers (mirrors patterns from tests/unit/test_channel_core_service.py)
# ---------------------------------------------------------------------------

FAKE_WORKSPACE_INSTRUCTIONS = "# 行为规范\n\nYou are a helpful agent.\n\n---\n\n# 心跳任务\n\nRun daily check."


class _DummyContext:
    def __init__(self, initial_vars: dict | None = None):
        self._vars: dict = initial_vars or {}

    def get_var_value(self, name: str):
        return self._vars.get(name)

    def set_variable(self, name: str, value):
        self._vars[name] = value

    def init_trajectory(self, _path: str, overwrite: bool = False):
        pass


class _CallTrackingAgent:
    """Agent that records kwargs passed to continue_chat."""

    name = "test_agent"

    def __init__(self, ctx: _DummyContext):
        self.executor = SimpleNamespace(context=ctx)
        self.state = AgentState.INITIALIZED
        self.calls: list[dict] = []
        self.snapshot = SimpleNamespace(
            export_portable_session=lambda: {"history_messages": []},
        )

    async def continue_chat(self, **kwargs):
        self.calls.append(kwargs)
        yield {"_progress": [{"id": "p1", "status": "running", "stage": "llm", "delta": "OK"}]}


def _make_session_manager_mock(tmp_path: Path, *, clear_workspace_on_restore: bool = True):
    """Session manager whose restore_to_agent clears workspace_instructions."""
    timeline_events: list = []

    def append_timeline_event(_sid, event):
        timeline_events.append(dict(event))

    lock_dir = tmp_path / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)

    persistence_mock = SimpleNamespace(
        _get_lock_path=lambda session_id: lock_dir / f".{session_id}.lock",
    )

    # Simulates the real restore that strips workspace_instructions
    async def _restore_to_agent(agent, session_data):
        if clear_workspace_on_restore:
            agent.executor.context._vars.pop("workspace_instructions", None)

    session_data = SimpleNamespace(
        mailbox=[],
        timeline=[],
        variables={},
        history_messages=[],
    )

    return SimpleNamespace(
        persistence=persistence_mock,
        save_session=AsyncMock(),
        load_session=AsyncMock(return_value=session_data),
        restore_timeline=lambda sid, timeline: None,
        restore_to_agent=_restore_to_agent,
        acquire_session=AsyncMock(return_value=True),
        release_session=lambda sid: None,
        ack_mailbox_events=AsyncMock(return_value=True),
        append_timeline_event=append_timeline_event,
        _timeline_events=timeline_events,
    )


def _make_user_data_mock(tmp_path: Path, agent_name: str = "test_agent"):
    """User data with a real workspace directory containing instruction files."""
    agents_dir = tmp_path / "agents"
    agent_dir = agents_dir / agent_name
    agent_dir.mkdir(parents=True, exist_ok=True)

    # Write workspace files so WorkspaceLoader can read them
    (agent_dir / "AGENTS.md").write_text("You are a helpful agent.", encoding="utf-8")
    (agent_dir / "HEARTBEAT.md").write_text("Run daily check.", encoding="utf-8")

    def _get_agent_dir(name: str) -> Path:
        return agents_dir / name

    def _get_session_trajectory_path(name: str, session_id: str) -> Path:
        return tmp_path / f"{name}_{session_id}.jsonl"

    return SimpleNamespace(
        agents_dir=agents_dir,
        sessions_dir=tmp_path / "sessions",
        get_agent_dir=_get_agent_dir,
        get_session_trajectory_path=_get_session_trajectory_path,
    )


def _make_core_service(tmp_path: Path, agent_name: str = "test_agent"):
    sm = _make_session_manager_mock(tmp_path)
    ud = _make_user_data_mock(tmp_path, agent_name)
    core = ChannelCoreService.__new__(ChannelCoreService)
    core.session_manager = sm
    core.user_data = ud
    core.agent_service = None
    return core


class _EventCollector:
    def __init__(self):
        self.events: list[OutboundMessage] = []

    async def __call__(self, msg: OutboundMessage):
        self.events.append(msg)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_workspace_instructions_reloaded_after_restore():
    """workspace_instructions is reloaded from disk after session restore clears it."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        agent_name = "test_agent"
        core = _make_core_service(tmp_path, agent_name)

        # Agent starts with workspace_instructions set (as it would after create_agent)
        ctx = _DummyContext({"workspace_instructions": FAKE_WORKSPACE_INSTRUCTIONS})
        agent = _CallTrackingAgent(ctx)
        collector = _EventCollector()

        await core.process_message(
            agent, agent_name, "session_1", "hello", collector,
        )

        # The system prompt built for the LLM should contain workspace content
        cached = core._runtime_workspace_instructions_by_agent.get(agent_name, "")
        assert "行为规范" in cached or "helpful agent" in cached, (
            f"workspace_instructions was not reloaded after restore. Cached value: {cached!r}"
        )


@pytest.mark.asyncio
async def test_workspace_instructions_not_reloaded_when_present():
    """When workspace_instructions survives restore (no filtering), no extra reload happens."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        agent_name = "test_agent"

        sm = _make_session_manager_mock(tmp_path, clear_workspace_on_restore=False)
        ud = _make_user_data_mock(tmp_path, agent_name)
        core = ChannelCoreService.__new__(ChannelCoreService)
        core.session_manager = sm
        core.user_data = ud
        core.agent_service = None

        original = "Original workspace instructions from create_agent"
        ctx = _DummyContext({"workspace_instructions": original})
        agent = _CallTrackingAgent(ctx)
        collector = _EventCollector()

        await core.process_message(
            agent, agent_name, "session_1", "hello", collector,
        )

        # Should keep original, not overwrite with disk content
        cached = core._runtime_workspace_instructions_by_agent.get(agent_name, "")
        assert cached == original


@pytest.mark.asyncio
async def test_cache_runtime_workspace_instructions_empty_after_restore():
    """Unit-level: _cache_runtime_workspace_instructions handles None gracefully."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        core = _make_core_service(tmp_path)

        ctx = _DummyContext({})  # No workspace_instructions
        core._cache_runtime_workspace_instructions("test_agent", ctx)

        # Should NOT cache an empty/None value
        assert core._runtime_workspace_instructions_by_agent.get("test_agent", "") == ""
