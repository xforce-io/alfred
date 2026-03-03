"""
Tests for session reset + WebSocket reconnect context leak.

Bug: reset_agent_sessions() removes agent from _agents cache but does NOT
clear the agent's Dolphin context.  When a WebSocket reconnects after reset,
the old history leaks into the new session.

These tests MUST FAIL with the current code to prove the bug exists.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dolphin.core.common.constants import KEY_HISTORY
from src.everbot.web import app as web_app

from .conftest import FakeContext, FakeSnapshot, ScriptedAgent, receive_until


# ---------------------------------------------------------------------------
# Bug 2: reset_agent_sessions leaves stale history in agent Dolphin context
# ---------------------------------------------------------------------------


def _make_agent_with_history(name: str, history: list[dict]) -> ScriptedAgent:
    """Create a ScriptedAgent pre-loaded with history in its Dolphin context."""
    agent = ScriptedAgent(name=name, script=[
        {"_progress": [{"id": "llm-1", "stage": "llm", "delta": "ok"}]},
    ])
    agent.executor.context._history = list(history)
    agent.executor.context.set_variable(KEY_HISTORY, list(history))
    return agent


def test_reset_then_reconnect_gets_clean_history(client, isolated_web_env):
    """After reset, a new WebSocket connection MUST see an empty history.

    Regression: Previously, reset only popped the agent from _agents cache
    but the agent's Dolphin context kept old history.  A reconnect would
    reuse that stale context.
    """
    agent_name = "demo_agent"
    session_id = "web_session_demo_agent"

    # Prepare agent workspace so create_agent_instance can succeed
    agent_dir = isolated_web_env.user_data.get_agent_dir(agent_name)
    agent_dir.mkdir(parents=True, exist_ok=True)

    # ── Step 1: Simulate a previous session with history ──
    stale_history = [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
    ]
    stale_agent = _make_agent_with_history(agent_name, stale_history)
    isolated_web_env.session_manager.cache_agent(
        session_id, stale_agent, agent_name, "gpt-4",
    )
    # Persist to disk so reset can find it
    session_file = isolated_web_env.user_data.sessions_dir / f"{session_id}.json"
    session_file.write_text(json.dumps({
        "session_id": session_id,
        "agent_name": agent_name,
        "model_name": "gpt-4",
        "history_messages": stale_history,
        "variables": {},
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "timeline": [],
        "context_trace": {},
    }), encoding="utf-8")

    # ── Step 2: Call reset API ──
    resp = client.post(f"/api/agents/{agent_name}/sessions/reset")
    assert resp.status_code == 200
    assert resp.json()["removed_sessions"] >= 1

    # Cache should be cleared
    assert isolated_web_env.session_manager.get_cached_agent(session_id) is None

    # ── Step 3: Reconnect via WebSocket ──
    # Mock create_agent_instance to return a *fresh* agent
    fresh_agent = ScriptedAgent(name=agent_name, script=[
        {"_progress": [{"id": "llm-1", "stage": "llm", "delta": "你好！我是助手"}]},
    ])
    web_app.chat_service.agent_service.create_agent_instance = AsyncMock(
        return_value=fresh_agent
    )

    with client.websocket_connect(f"/ws/chat/{agent_name}") as ws:
        first = ws.receive_json()

        # After reset, we should get a welcome message (no history),
        # NOT a "history" payload with old messages.
        if first.get("type") == "history":
            history_msgs = first.get("messages", [])
            assert len(history_msgs) == 0, (
                f"Expected empty history after reset, but got {len(history_msgs)} "
                f"stale messages.  Reset did not clear agent Dolphin context."
            )
        else:
            # "message" type = welcome message = clean state ✓
            assert first["type"] == "message"


def test_reset_clears_agent_dolphin_context_not_just_cache(client, isolated_web_env):
    """Verify that reset_agent_sessions clears the Dolphin context of cached agents,
    not just removes them from the _agents dict.

    Even if code holds a reference to the old agent, its context should be empty.
    """
    agent_name = "demo_agent"
    session_id = "web_session_demo_agent"

    stale_history = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2"},
    ]
    agent = _make_agent_with_history(agent_name, stale_history)

    # Cache it
    isolated_web_env.session_manager.cache_agent(
        session_id, agent, agent_name, "gpt-4",
    )
    # Persist to disk
    session_file = isolated_web_env.user_data.sessions_dir / f"{session_id}.json"
    session_file.write_text(json.dumps({
        "session_id": session_id,
        "agent_name": agent_name,
        "model_name": "gpt-4",
        "history_messages": stale_history,
        "variables": {},
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "timeline": [],
        "context_trace": {},
    }), encoding="utf-8")

    # Keep a reference to the old agent (simulating a held reference)
    old_agent_ref = agent

    # Reset via API (uses the same session_manager under the hood)
    resp = client.post(f"/api/agents/{agent_name}/sessions/reset")
    assert resp.status_code == 200

    # The old agent's Dolphin context should be cleared
    ctx_history = old_agent_ref.executor.context._history
    assert len(ctx_history) == 0, (
        f"After reset, agent Dolphin context should be empty but has "
        f"{len(ctx_history)} messages.  reset_agent_sessions only popped the "
        f"cache dict without clearing the agent's internal state."
    )
