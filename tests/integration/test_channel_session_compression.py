"""Integration test: channel sessions (tg_session_, etc.) trigger history compression.

Regression test for the bug where only 'primary' (web_session_) sessions were
compressed on save, causing channel sessions to accumulate unbounded history
and eventually degrade LLM quality (empty responses).

Tests cover two layers:
1. Persistence layer: save/load cycle keeps history bounded
2. End-to-end: after save → restore → dolphin context assembly, the messages
   actually sent to LLM are bounded (covers both everbot + dolphin layers)
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from dolphin.core.context.context import Context
from dolphin.core.common.constants import KEY_HISTORY
from dolphin.sdk.agent.dolphin_agent_snapshot import DolphinAgentSnapshot

from src.everbot.core.session.compressor import COMPRESS_THRESHOLD, WINDOW_SIZE, SUMMARY_TAG
from src.everbot.core.session.session import SessionManager, SessionData
from src.everbot.core.session.persistence import SessionPersistence


@pytest.fixture(autouse=True)
def _skip_memory_extraction():
    """Disable memory extraction during tests — it needs a full LLM config."""
    with patch("src.everbot.core.session.session.SessionManager.infer_session_type",
               wraps=SessionManager.infer_session_type):
        with patch("src.everbot.core.memory.manager.MemoryManager.process_session_end",
                   new_callable=AsyncMock, return_value=None):
            yield


def _make_mock_agent(context: Context, name: str = "demo_agent"):
    class MockAgent:
        def __init__(self, ctx):
            self.executor = type("obj", (object,), {"context": ctx})
            self.name = name
            self.snapshot = DolphinAgentSnapshot(self)

        def get_context(self):
            return self.executor.context

    return MockAgent(context)


def _make_history(n: int) -> list:
    """Generate n user+assistant message pairs (2n messages total)."""
    msgs = []
    for i in range(n):
        msgs.append({"role": "user", "content": f"msg-{i}"})
        msgs.append({"role": "assistant", "content": f"reply-{i}"})
    return msgs


def _append_turn(history: list, turn_id: int) -> list:
    """Append one user+assistant turn to history."""
    return history + [
        {"role": "user", "content": f"turn-{turn_id}-question"},
        {"role": "assistant", "content": f"turn-{turn_id}-answer"},
    ]


# ── Layer 1: Persistence-level compression ──────────────────────────


@pytest.mark.asyncio
async def test_channel_session_history_stays_bounded():
    """Simulate many save-restore cycles on a channel session.

    After enough turns to exceed the compression threshold, the persisted
    history must stay bounded, not grow linearly.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        session_dir = Path(tmpdir)
        manager = SessionManager(session_dir)
        session_id = "tg_session_demo_agent__12345"

        assert SessionManager.infer_session_type(session_id) == "channel"

        with patch(
            "src.everbot.core.session.compressor.SessionCompressor._generate_summary",
            new_callable=AsyncMock,
            return_value="之前的对话摘要内容",
        ):
            for turn in range(100):
                loaded = await manager.load_session(session_id)
                history = loaded.history_messages if loaded and loaded.history_messages else []
                history = _append_turn(history, turn)

                context = Context()
                context.set_variable(KEY_HISTORY, history)
                agent = _make_mock_agent(context)
                await manager.save_session(session_id, agent)

            final = await manager.load_session(session_id)
            assert final is not None

            max_expected = COMPRESS_THRESHOLD + 2
            assert len(final.history_messages) <= max_expected, (
                f"History grew to {len(final.history_messages)} messages, "
                f"expected at most {max_expected} (bounded by compression cycle)"
            )
            assert len(final.history_messages) < 200, (
                "History was never compressed — still at raw size"
            )
            assert SUMMARY_TAG in final.history_messages[0]["content"]
            assert final.history_messages[-1]["content"] == "turn-99-answer"


@pytest.mark.asyncio
async def test_primary_session_history_also_stays_bounded():
    """Same boundedness check for primary sessions (no regression)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        session_dir = Path(tmpdir)
        manager = SessionManager(session_dir)
        session_id = "web_session_demo_agent"

        assert SessionManager.infer_session_type(session_id) == "primary"

        with patch(
            "src.everbot.core.session.compressor.SessionCompressor._generate_summary",
            new_callable=AsyncMock,
            return_value="primary session摘要",
        ):
            for turn in range(100):
                loaded = await manager.load_session(session_id)
                history = loaded.history_messages if loaded and loaded.history_messages else []
                history = _append_turn(history, turn)

                context = Context()
                context.set_variable(KEY_HISTORY, history)
                agent = _make_mock_agent(context)
                await manager.save_session(session_id, agent)

            final = await manager.load_session(session_id)
            assert final is not None
            max_expected = COMPRESS_THRESHOLD + 2
            assert len(final.history_messages) <= max_expected
            assert len(final.history_messages) < 200


@pytest.mark.asyncio
async def test_channel_session_below_threshold_no_compression():
    """A channel session with short history should NOT be compressed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        session_dir = Path(tmpdir)
        manager = SessionManager(session_dir)
        session_id = "tg_session_demo_agent__12345"

        context = Context()
        history = _make_history(10)  # 20 messages, well below threshold
        context.set_variable(KEY_HISTORY, history)

        agent = _make_mock_agent(context)
        await manager.save_session(session_id, agent)

        loaded = await manager.load_session(session_id)
        assert loaded is not None
        assert len(loaded.history_messages) == 20
        assert SUMMARY_TAG not in (loaded.history_messages[0].get("content") or "")


# ── Layer 2: End-to-end — save, restore, verify LLM context bounded ─


@pytest.mark.asyncio
async def test_e2e_restored_channel_session_llm_context_bounded():
    """End-to-end: save a large channel session → restore to agent →
    verify that dolphin's assembled LLM messages are bounded.

    This crosses both the everbot persistence layer and the dolphin
    context assembly layer, catching issues in either.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        session_dir = Path(tmpdir)
        manager = SessionManager(session_dir)
        session_id = "tg_session_demo_agent__12345"

        # Step 1: Build a large history (200 messages) and save with compression
        with patch(
            "src.everbot.core.session.compressor.SessionCompressor._generate_summary",
            new_callable=AsyncMock,
            return_value="这是100轮对话的摘要",
        ):
            history = _make_history(100)  # 200 messages
            context = Context()
            context.set_variable(KEY_HISTORY, history)
            agent = _make_mock_agent(context)
            await manager.save_session(session_id, agent)

        # Verify persistence compressed it
        saved = await manager.load_session(session_id)
        assert len(saved.history_messages) < 200

        # Step 2: Create a fresh agent and restore the compressed session
        fresh_context = Context()
        fresh_agent = _make_mock_agent(fresh_context)
        await manager.restore_to_agent(fresh_agent, saved)

        # Step 3: Simulate what dolphin does before an LLM call —
        # assemble messages via context_manager.to_dph_messages()
        ctx = fresh_agent.executor.context
        cm = ctx.context_manager
        if cm is not None:
            llm_messages = cm.to_dph_messages()
            msg_count = len(llm_messages.get_messages())
        else:
            # Fallback: check history directly from context
            llm_messages = ctx.get_messages()
            msg_count = len(llm_messages.get_messages())

        # The messages sent to LLM must be bounded
        max_llm_messages = SessionPersistence.MAX_RESTORED_HISTORY_MESSAGES + 10  # small headroom
        assert msg_count <= max_llm_messages, (
            f"LLM would receive {msg_count} messages after restore, "
            f"expected at most ~{max_llm_messages}. "
            f"Context is unbounded — compression or restore truncation failed."
        )
        # Must not be the raw 200
        assert msg_count < 200, (
            f"LLM context has {msg_count} messages — neither compression "
            f"nor restore truncation reduced the history."
        )
