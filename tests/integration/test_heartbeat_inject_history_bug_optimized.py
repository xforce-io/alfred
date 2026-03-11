"""Spec tests for heartbeat history injection behavior (OPTIMIZED VERSION).

Validates the mailbox-only contract: deposit_mailbox_event writes to the
session mailbox without injecting into conversation history. This ensures
heartbeat results never pollute the LLM context with role-alternation
violations or phantom assistant replies.
"""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from dolphin.core.common.constants import KEY_HISTORY
from dolphin.core.context.context import Context
from dolphin.sdk.agent.dolphin_agent_snapshot import DolphinAgentSnapshot

from src.everbot.core.session.history_utils import (
    prepare_for_restore,
    _is_heartbeat,
)
from src.everbot.core.session.session import SessionManager

pytestmark = pytest.mark.asyncio


@pytest.fixture
def memory_patch():
    """Disable memory extraction to keep these tests focused and fast."""
    with patch(
        "src.everbot.core.memory.manager.MemoryManager.process_session_end",
        new_callable=AsyncMock,
        return_value=None,
    ):
        yield


@pytest.fixture
def session_manager(tmp_path: Path) -> SessionManager:
    """Create an isolated SessionManager rooted in a temporary directory."""
    return SessionManager(tmp_path)


def _make_mock_agent(context: Context | None = None, name: str = "demo_agent"):
    ctx = context or Context()

    class MockAgent:
        def __init__(self):
            self.executor = type("obj", (object,), {"context": ctx})
            self.name = name
            self.snapshot = DolphinAgentSnapshot(self)

        def get_context(self):
            return self.executor.context

    return MockAgent()


def _make_mailbox_event(content: str, run_id: str) -> dict:
    """Build a mailbox event that simulates heartbeat result."""
    return {
        "type": "heartbeat_result",
        "content": content,
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _extract_roles(messages: list[dict]) -> list[str]:
    """Return user/assistant roles in order from a message list."""
    return [
        msg.get("role")
        for msg in messages
        if isinstance(msg, dict) and msg.get("role") in ("user", "assistant")
    ]


def _find_consecutive_role_violations(messages: list[dict]) -> list[tuple[int, str]]:
    """Return indices where adjacent user/assistant roles are identical."""
    roles = _extract_roles(messages)
    return [
        (i, roles[i])
        for i in range(1, len(roles))
        if roles[i] == roles[i - 1]
    ]


def _history_snapshot(messages: list[dict]) -> list[tuple[str | None, str, str]]:
    """Build a compact snapshot for assertion failure output."""
    snapshot = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        metadata = msg.get("metadata") or {}
        run_id = metadata.get("run_id", "") if isinstance(metadata, dict) else ""
        content = str(msg.get("content") or "").replace("\n", "\\n")
        snapshot.append((msg.get("role"), content[:80], run_id))
    return snapshot


def _all_content(messages: list[dict]) -> str:
    """Concatenate textual content from a message list."""
    return " ".join(
        msg.get("content", "")
        for msg in messages
        if isinstance(msg, dict) and isinstance(msg.get("content"), str)
    )


async def _restore_history(
    mgr: SessionManager,
    sid: str,
    history: list[dict],
) -> list[dict]:
    """Persist raw history, restore it, and return the recovered history."""
    ctx = Context()
    ctx.set_variable(KEY_HISTORY, history)
    agent = _make_mock_agent(ctx)
    await mgr.save_session(sid, agent)

    loaded = await mgr.load_session(sid)
    assert loaded is not None, f"Failed to load session: {sid}"

    fresh = _make_mock_agent()
    await mgr.restore_to_agent(fresh, loaded)

    restored = fresh.get_context().get_var_value(KEY_HISTORY)
    assert isinstance(restored, list), f"Restored history is not a list: {restored!r}"
    return restored


async def _load_restored_history(mgr: SessionManager, sid: str) -> list[dict]:
    """Load an existing session from disk and restore it into a fresh agent."""
    loaded = await mgr.load_session(sid)
    assert loaded is not None, f"Failed to load session: {sid}"

    fresh = _make_mock_agent()
    await mgr.restore_to_agent(fresh, loaded)

    restored = fresh.get_context().get_var_value(KEY_HISTORY)
    assert isinstance(restored, list), f"Restored history is not a list: {restored!r}"
    return restored


# ═══════════════════════════════════════════════════════════════════════════
# MAILBOX-ONLY BEHAVIOR TESTS
# ═══════════════════════════════════════════════════════════════════════════

class TestMailboxOnlyBehavior:
    """Verify deposit_mailbox_event does NOT inject into conversation history.

    The mailbox-only contract: heartbeat results go into the session mailbox
    and are consumed as context prefix on the next chat turn, never as
    standalone assistant messages in the history.
    """

    async def test_deposit_does_not_create_consecutive_assistants(
        self, session_manager: SessionManager, memory_patch
    ):
        """Mailbox deposit must not introduce consecutive assistant messages."""
        sid = "tg_session_demo_agent__consecutive"
        history = [
            {"role": "user", "content": "What is Cybernetics?"},
            {"role": "assistant", "content": "Cybernetics studies control..."},
        ]
        ctx = Context()
        ctx.set_variable(KEY_HISTORY, history)
        agent = _make_mock_agent(ctx)
        await session_manager.save_session(sid, agent)

        await session_manager.deposit_mailbox_event(
            sid, _make_mailbox_event("Health check: normal", "hb_001"),
        )

        restored = await _load_restored_history(session_manager, sid)
        violations = _find_consecutive_role_violations(restored)

        assert len(violations) == 0, (
            "Mailbox deposit should not cause consecutive assistants. "
            f"Violations: {violations}, Snapshot: {_history_snapshot(restored)}"
        )

    async def test_deposit_does_not_inject_heartbeats_into_history(
        self, session_manager: SessionManager, memory_patch
    ):
        """Multiple mailbox deposits must not appear as heartbeat messages in history."""
        sid = "tg_session_demo_agent__multi_hb"
        history = [
            {"role": "user", "content": "Tell me about AI"},
            {"role": "assistant", "content": "AI is..."},
        ]
        ctx = Context()
        ctx.set_variable(KEY_HISTORY, history)
        agent = _make_mock_agent(ctx)
        await session_manager.save_session(sid, agent)

        for i in range(3):
            await session_manager.deposit_mailbox_event(
                sid, _make_mailbox_event(f"Report {i}", f"hb_{i}"),
            )

        restored = await _load_restored_history(session_manager, sid)
        hb_count = sum(1 for m in restored if _is_heartbeat(m))

        assert hb_count == 0, (
            f"Expected 0 heartbeats in history (mailbox-only), found {hb_count}"
        )

    async def test_deposit_does_not_fill_answer_slot(
        self, session_manager: SessionManager, memory_patch
    ):
        """Mailbox deposit must not become an answer to unanswered user question."""
        sid = "tg_session_demo_agent__answer_slot"
        history = [
            {"role": "user", "content": "What is the meaning of life?"},
        ]
        ctx = Context()
        ctx.set_variable(KEY_HISTORY, history)
        agent = _make_mock_agent(ctx)
        await session_manager.save_session(sid, agent)

        await session_manager.deposit_mailbox_event(
            sid, _make_mailbox_event("System health OK", "hb_answer"),
        )

        restored = await _load_restored_history(session_manager, sid)
        roles = _extract_roles(restored)

        # Only user message should be present; no phantom assistant reply
        assistant_count = sum(1 for r in roles if r == "assistant")
        assert assistant_count == 0, (
            f"Expected no assistant messages from mailbox deposit, "
            f"found {assistant_count}. Snapshot: {_history_snapshot(restored)}"
        )

    async def test_unanswered_question_does_not_get_heartbeat_as_answer(
        self,
        session_manager: SessionManager,
        memory_patch,
    ):
        """A heartbeat must not become the next assistant reply to a user question."""
        sid = "tg_session_demo_agent__123"
        history = [
            {"role": "user", "content": "Tell me about Cybernetics"},
            {"role": "assistant", "content": "Cybernetics studies control and communication"},
            {"role": "user", "content": "Cybernetics 具体说了什么？"},
        ]
        await _restore_history(session_manager, sid, history)
        await session_manager.deposit_mailbox_event(
            sid, _make_mailbox_event("Health check: all systems normal", "hb_001"),
        )
        restored = await _load_restored_history(session_manager, sid)

        # No heartbeat should appear in answer slot
        hb_msgs = [m for m in restored if isinstance(m, dict) and _is_heartbeat(m)]
        assert len(hb_msgs) == 0, (
            f"Heartbeat found in history. Snapshot: {_history_snapshot(restored)}"
        )

    async def test_multiple_heartbeats_do_not_create_consecutive_assistants(
        self,
        session_manager: SessionManager,
        memory_patch,
    ):
        """Restored history must preserve user/assistant alternation."""
        sid = "tg_session_demo_agent__456"
        history = [
            {"role": "user", "content": "https://x.com/odysseus0z/status/123"},
            {"role": "assistant", "content": "Harness Engineering Is Cybernetics - detailed analysis"},
            {"role": "user", "content": "Cybernetics 具体说了什么？"},
        ]
        ctx = Context()
        ctx.set_variable(KEY_HISTORY, history)
        agent = _make_mock_agent(ctx)
        await session_manager.save_session(sid, agent)

        for i, text in enumerate([
            "## 会话轨迹健康检测\n检测正常",
            "## 每日论文报告\nSkillNet: AI Skills Framework",
            "## 每日新闻简报\n国际热点 TOP 10",
        ]):
            await session_manager.deposit_mailbox_event(
                sid, _make_mailbox_event(text, f"hb_{i}"),
            )

        loaded = await session_manager.load_session(sid)
        assert loaded is not None
        fresh = _make_mock_agent()
        await session_manager.restore_to_agent(fresh, loaded)
        restored = fresh.get_context().get_var_value(KEY_HISTORY)
        assert isinstance(restored, list)

        violations = _find_consecutive_role_violations(restored)
        assert len(violations) == 0, (
            f"Consecutive assistants found: {violations}. "
            f"Snapshot: {_history_snapshot(restored)}"
        )

    async def test_real_response_survives_heartbeat_restore_cycle(
        self,
        session_manager: SessionManager,
        memory_patch,
    ):
        """Original assistant reply must survive after mailbox deposit."""
        sid = "tg_session_demo_agent__789"
        history = [
            {"role": "user", "content": "讲一个关于Cybernetics的笑话"},
            {"role": "assistant", "content": "为什么cyberneticist喜欢网球场？因为他们喜欢反馈回路！"},
        ]
        ctx = Context()
        ctx.set_variable(KEY_HISTORY, history)
        agent = _make_mock_agent(ctx)
        await session_manager.save_session(sid, agent)

        await session_manager.deposit_mailbox_event(
            sid, _make_mailbox_event("Routine health check passed", "hb_002"),
        )

        loaded = await session_manager.load_session(sid)
        assert loaded is not None
        fresh = _make_mock_agent()
        await session_manager.restore_to_agent(fresh, loaded)
        restored = fresh.get_context().get_var_value(KEY_HISTORY)
        assert isinstance(restored, list)

        content = _all_content(restored)
        assert "反馈回路" in content, (
            f"Original joke lost after mailbox deposit. "
            f"Content: {content[:200]}, Snapshot: {_history_snapshot(restored)}"
        )

    async def test_no_role_alternation_violation_after_full_cycle(
        self,
        session_manager: SessionManager,
        memory_patch,
    ):
        """Complete save→deposit→restore cycle must not violate role alternation."""
        sid = "tg_session_demo_agent__cycle"
        history = [
            {"role": "user", "content": "分析这张图片"},
            {"role": "assistant", "content": "图片显示一个控制系统框图..."},
            {"role": "user", "content": "框图里的feedback loop是什么原理？"},
        ]
        await _restore_history(session_manager, sid, history)

        await session_manager.deposit_mailbox_event(
            sid, _make_mailbox_event("Health check: all systems nominal", "hb_cycle"),
        )

        restored = await _load_restored_history(session_manager, sid)
        violations = _find_consecutive_role_violations(restored)

        assert len(violations) == 0, (
            f"Role alternation violations after full cycle: {violations}. "
            f"Snapshot: {_history_snapshot(restored)}"
        )

    async def test_cybernetics_content_visible_after_heartbeat_injection(
        self,
        session_manager: SessionManager,
        memory_patch,
    ):
        """Cybernetics content must remain visible after mailbox deposit."""
        sid = "tg_session_demo_agent__visibility"
        history = [
            {"role": "user", "content": "Cybernetics 是什么？"},
            {"role": "assistant", "content": "Cybernetics（控制论）是研究动物和机器中控制与通信的科学。"},
        ]
        ctx = Context()
        ctx.set_variable(KEY_HISTORY, history)
        agent = _make_mock_agent(ctx)
        await session_manager.save_session(sid, agent)

        await session_manager.deposit_mailbox_event(
            sid, _make_mailbox_event("Daily check complete", "hb_vis"),
        )

        loaded = await session_manager.load_session(sid)
        assert loaded is not None
        fresh = _make_mock_agent()
        await session_manager.restore_to_agent(fresh, loaded)
        restored = fresh.get_context().get_var_value(KEY_HISTORY)
        assert isinstance(restored, list)

        content = _all_content(restored)
        assert "控制论" in content, (
            f"Cybernetics content lost. Content: {content[:200]}, "
            f"Snapshot: {_history_snapshot(restored)}"
        )

    async def test_second_save_restore_cycle_preserves_content_and_structure(
        self,
        session_manager: SessionManager,
        memory_patch,
    ):
        """Multiple save/restore cycles must preserve content and structure."""
        sid = "tg_session_demo_agent__multi_cycle"
        history = [
            {"role": "user", "content": "What is AI?"},
            {"role": "assistant", "content": "AI stands for Artificial Intelligence..."},
        ]

        # First cycle
        await _restore_history(session_manager, sid, history)
        await session_manager.deposit_mailbox_event(
            sid, _make_mailbox_event("Check 1", "hb_1"),
        )

        # Second cycle
        restored1 = await _load_restored_history(session_manager, sid)
        ctx = Context()
        ctx.set_variable(KEY_HISTORY, restored1)
        agent = _make_mock_agent(ctx)
        await session_manager.save_session(sid, agent)
        await session_manager.deposit_mailbox_event(
            sid, _make_mailbox_event("Check 2", "hb_2"),
        )

        restored2 = await _load_restored_history(session_manager, sid)

        content = _all_content(restored2)
        assert "Artificial Intelligence" in content, (
            f"Content lost after second cycle. Content: {content[:200]}"
        )

        violations = _find_consecutive_role_violations(restored2)
        assert len(violations) == 0, (
            f"Role violations after second cycle: {violations}"
        )

    async def test_heartbeat_messages_do_not_outnumber_chat_messages(
        self,
        session_manager: SessionManager,
        memory_patch,
    ):
        """Background reports should not appear in restored chat context."""
        sid = "tg_session_demo_agent__pollution"
        history = []
        for i in range(5):
            history.append({"role": "user", "content": f"Question {i}"})
            history.append({"role": "assistant", "content": f"Answer {i}"})

        ctx = Context()
        ctx.set_variable(KEY_HISTORY, history)
        agent = _make_mock_agent(ctx)
        await session_manager.save_session(sid, agent)

        for i in range(15):
            await session_manager.deposit_mailbox_event(
                sid,
                _make_mailbox_event(f"Heartbeat report #{i}: " + "x" * 200, f"hb_{i}"),
            )

        loaded = await session_manager.load_session(sid)
        assert loaded is not None
        fresh = _make_mock_agent()
        await session_manager.restore_to_agent(fresh, loaded)
        restored = fresh.get_context().get_var_value(KEY_HISTORY)
        assert isinstance(restored, list)

        hb_msgs = [msg for msg in restored if isinstance(msg, dict) and _is_heartbeat(msg)]

        assert len(hb_msgs) == 0, (
            f"Heartbeat messages ({len(hb_msgs)}) found in history under mailbox-only. "
            f"Snapshot: {_history_snapshot(restored)}"
        )
