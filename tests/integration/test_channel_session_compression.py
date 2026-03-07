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

    Uses long-content messages so the token budget (COMPACT_TOKEN_BUDGET=40K)
    is exceeded during the save cycles, triggering LLM summary compression.
    After compression fires, the persisted history must stay bounded — not grow
    linearly with turn count.
    """
    # Each message needs ~600 chars so that ~200 messages exceed the 40K-token
    # budget (200 × (600+12) / 3 ≈ 40,800 tokens > COMPACT_TOKEN_BUDGET).
    _LONG = "x" * 600

    def _append_long_turn(history: list, turn_id: int) -> list:
        return history + [
            {"role": "user", "content": f"{_LONG} turn-{turn_id}-question"},
            {"role": "assistant", "content": f"{_LONG} turn-{turn_id}-answer"},
        ]

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
                history = _append_long_turn(history, turn)

                context = Context()
                context.set_variable(KEY_HISTORY, history)
                agent = _make_mock_agent(context)
                await manager.save_session(session_id, agent)

            final = await manager.load_session(session_id)
            assert final is not None

            # Token-based compression must have fired: summary injected at head.
            assert SUMMARY_TAG in final.history_messages[0]["content"], (
                "Compression never triggered — token budget may not have been exceeded"
            )
            # History must be bounded below the raw accumulated size.
            assert len(final.history_messages) < 200, (
                f"History grew to {len(final.history_messages)} messages — "
                "compression did not reduce history size"
            )
            # Most-recent turn must still be verbatim in the kept window.
            assert final.history_messages[-1]["content"].endswith("turn-99-answer")


@pytest.mark.asyncio
async def test_primary_session_history_also_stays_bounded():
    """Same boundedness check for primary sessions (no regression).

    Uses long-content messages to exceed the token budget and trigger compression.
    """
    _LONG = "x" * 600

    def _append_long_turn(history: list, turn_id: int) -> list:
        return history + [
            {"role": "user", "content": f"{_LONG} turn-{turn_id}-question"},
            {"role": "assistant", "content": f"{_LONG} turn-{turn_id}-answer"},
        ]

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
                history = _append_long_turn(history, turn)

                context = Context()
                context.set_variable(KEY_HISTORY, history)
                agent = _make_mock_agent(context)
                await manager.save_session(session_id, agent)

            final = await manager.load_session(session_id)
            assert final is not None
            assert SUMMARY_TAG in final.history_messages[0]["content"], (
                "Compression never triggered for primary session"
            )
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
async def test_e2e_restore_preserves_compressed_result():
    """End-to-end: save compresses history → restore passes the result through intact.

    Verifies the responsibility boundary:
    - save() compresses history (token-budget based, via SessionCompressor).
    - restore_to_agent() performs data hygiene only (strips empty/heartbeat
      messages) and must NOT further truncate what save() already compressed.
    - Token-based context guardrail for LLM calls is Dolphin's responsibility
      and is not tested here.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        session_dir = Path(tmpdir)
        manager = SessionManager(session_dir)
        session_id = "tg_session_demo_agent__12345"

        # Step 1: Build a large history (200 messages) and save with compression.
        # Messages need enough content to exceed the 40K-token budget so that
        # save() actually compresses (200 × (600+12) / 3 ≈ 40,800 tokens).
        _LONG = "x" * 600

        def _make_long_history(n: int) -> list:
            msgs = []
            for i in range(n):
                msgs.append({"role": "user", "content": f"{_LONG} msg-{i}"})
                msgs.append({"role": "assistant", "content": f"{_LONG} reply-{i}"})
            return msgs

        with patch(
            "src.everbot.core.session.compressor.SessionCompressor._generate_summary",
            new_callable=AsyncMock,
            return_value="这是100轮对话的摘要",
        ):
            history = _make_long_history(100)  # 200 messages with long content
            context = Context()
            context.set_variable(KEY_HISTORY, history)
            agent = _make_mock_agent(context)
            await manager.save_session(session_id, agent)

        saved = await manager.load_session(session_id)
        assert len(saved.history_messages) < 200, "save() should have compressed history"
        compressed_count = len(saved.history_messages)

        # Step 2: Restore into a fresh agent; intercept import_portable_session
        # to capture exactly what EverBot passes to Dolphin.
        fresh_context = Context()
        fresh_agent = _make_mock_agent(fresh_context)
        imported_state: dict = {}
        original_import = fresh_agent.snapshot.import_portable_session

        def capture_import(state, **kwargs):
            imported_state.update(state)
            return original_import(state, **kwargs)

        fresh_agent.snapshot.import_portable_session = capture_import
        await manager.restore_to_agent(fresh_agent, saved)

        # Step 3: restore must pass the compressed history intact — no additional loss.
        # (The test history has no heartbeat/empty messages, so hygiene filters are no-ops.)
        assert "history_messages" in imported_state, "import_portable_session was not called"
        assert len(imported_state["history_messages"]) == compressed_count, (
            f"restore introduced additional truncation: "
            f"save produced {compressed_count} messages but "
            f"restore passed {len(imported_state['history_messages'])} to Dolphin"
        )
