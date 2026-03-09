"""Spec tests for heartbeat history injection behavior.

This module intentionally contains two layers of tests:

1. Current-behavior tests
   These document the existing inject→placeholder→restore chain and prove
   why the current design is structurally unsafe.
2. Target-behavior tests
   These express the mailbox-only contract that should hold after the
   architectural fix lands.

The current codebase is expected to pass the first group and fail the second
group until heartbeat results stop being injected into session history.
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


def _make_heartbeat_msg(content: str, run_id: str) -> dict:
    """Build a persisted heartbeat-like assistant message."""
    return {
        "role": "assistant",
        "content": content,
        "metadata": {
            "source": "heartbeat",
            "run_id": run_id,
            "injected_at": datetime.now(timezone.utc).isoformat(),
        },
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


def _make_mailbox_event(content: str, run_id: str) -> dict:
    """Build a mailbox event matching what heartbeat delivery deposits."""
    from src.everbot.core.models.system_event import build_system_event

    return build_system_event(
        event_type="heartbeat_result",
        source_session_id="heartbeat_session_demo_agent",
        summary=content[:300],
        detail=content,
        dedupe_key=f"heartbeat:demo_agent:{run_id}",
    )


async def _save_restore_cycle(
    mgr: SessionManager,
    sid: str,
    history: list[dict],
    heartbeat_reports: list[tuple[str, str]],
    cycles: int = 1,
) -> tuple[list[dict], list[dict]]:
    """Persist history, deposit heartbeats to mailbox, then run N restore→save cycles."""
    ctx = Context()
    ctx.set_variable(KEY_HISTORY, history)
    agent = _make_mock_agent(ctx)
    await mgr.save_session(sid, agent)

    for content, run_id in heartbeat_reports:
        await mgr.deposit_mailbox_event(sid, _make_mailbox_event(content, run_id))

    restored: list[dict] | None = None
    for _ in range(cycles):
        loaded = await mgr.load_session(sid)
        assert loaded is not None, f"Failed to load session during cycle: {sid}"
        fresh = _make_mock_agent()
        await mgr.restore_to_agent(fresh, loaded)
        restored = fresh.get_context().get_var_value(KEY_HISTORY)
        assert isinstance(restored, list), f"Restored history is not a list: {restored!r}"
        await mgr.save_session(sid, fresh)

    final = await mgr.load_session(sid)
    assert final is not None, f"Failed to reload final session: {sid}"
    return restored or [], final.history_messages


def _assert_no_heartbeat_in_answer_slot(messages: list[dict], question_substring: str) -> None:
    """Assert that the message after a user question is not a heartbeat.

    If the question is the last message (no answer yet), the assertion passes —
    an absent answer is not a heartbeat stealing the slot.
    """
    q_idx = next(
        i
        for i, msg in enumerate(messages)
        if question_substring in str(msg.get("content") or "")
    )
    next_msg = messages[q_idx + 1] if q_idx + 1 < len(messages) else None
    if next_msg is None:
        return  # no answer yet — acceptable
    assert not _is_heartbeat(next_msg), (
        f"Heartbeat occupies the answer slot after '{question_substring}'. "
        f"Snapshot: {_history_snapshot(messages)}"
    )


# ── Current Behavior: documents the existing bug chain ───────────────


class TestCurrentBehaviorOfHistoryInjection:
    """Document the current placeholder stripping and heartbeat retention behavior."""

    def test_prepare_for_restore_creates_consecutive_assistants(self):
        """Placeholder stripping leaves two adjacent assistant messages."""
        history = [
            {"role": "user", "content": "What is cybernetics?"},
            {"role": "assistant", "content": "Cybernetics is the study of control..."},
            {"role": "assistant", "content": "(acknowledged)",
             "metadata": {"source": "system", "category": "placeholder", "run_id": "hb_1"}},
            {"role": "user", "content": "[Background notification follows]",
             "metadata": {"source": "system", "category": "placeholder", "run_id": "hb_1"}},
            _make_heartbeat_msg("Health check: all normal", "hb_1"),
        ]

        result = prepare_for_restore(history)
        violations = _find_consecutive_role_violations(result)

        assert violations, (
            "Expected consecutive same-role messages after placeholder stripping. "
            f"Snapshot: {_history_snapshot(result)}"
        )

    def test_prepare_for_restore_keeps_multiple_consecutive_heartbeats(self):
        """Multiple heartbeat injections become multiple adjacent assistants."""
        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
            _make_heartbeat_msg("Report 1", "hb_1"),
            _make_heartbeat_msg("Report 2", "hb_2"),
        ]

        result = prepare_for_restore(history)
        consecutive_count = sum(
            1
            for i in range(1, len(_extract_roles(result)))
            if _extract_roles(result)[i] == _extract_roles(result)[i - 1] == "assistant"
        )
        assert consecutive_count >= 2, (
            "Expected multiple adjacent heartbeat assistants after restore prep. "
            f"Snapshot: {_history_snapshot(result)}"
        )

    def test_prepare_for_restore_places_heartbeat_into_answer_slot(self):
        """An unanswered user question is followed by a heartbeat assistant."""
        history = [
            {"role": "user", "content": "是给人用的还是给机器用的"},
            {"role": "assistant", "content": "(acknowledged)",
             "metadata": {"source": "system", "category": "placeholder", "run_id": "hb_1"}},
            {"role": "user", "content": "[Background notification follows]",
             "metadata": {"source": "system", "category": "placeholder", "run_id": "hb_1"}},
            _make_heartbeat_msg("Health check: normal", "hb_1"),
        ]

        result = prepare_for_restore(history)

        assert len(result) == 2
        assert result[0]["role"] == "user"
        assert result[0]["content"] == "是给人用的还是给机器用的"
        assert result[1]["role"] == "assistant"
        assert _is_heartbeat(result[1]), (
            "Heartbeat should occupy the answer slot in the current behavior test. "
            f"Snapshot: {_history_snapshot(result)}"
        )


# ── Target Behavior: mailbox-only contract after the fix ─────────────


class TestMailboxOnlyTargetBehavior:
    """Describe the expected behavior after heartbeat history injection is removed."""

    @pytest.mark.asyncio
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
        # Mailbox-only: heartbeat result goes to mailbox, not history
        await session_manager.deposit_mailbox_event(
            sid, _make_mailbox_event("Health check: all systems normal", "hb_001"),
        )
        restored = await _load_restored_history(session_manager, sid)

        _assert_no_heartbeat_in_answer_slot(restored, "具体说了什么")

    @pytest.mark.asyncio
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
        assert not violations, (
            f"Consecutive same-role messages after restore: {violations}. "
            f"Snapshot: {_history_snapshot(restored)}"
        )

    @pytest.mark.asyncio
    async def test_real_response_survives_heartbeat_restore_cycle(
        self,
        session_manager: SessionManager,
        memory_patch,
    ):
        """A valid assistant answer must survive inject→restore→save."""
        sid = "tg_session_demo_agent__789"
        history = [
            {"role": "user", "content": "https://x.com/odysseus0z/status/123"},
            {
                "role": "assistant",
                "content": (
                    "Harness Engineering Is Cybernetics — 作者 George 阐述了控制论与"
                    "工具工程的关系。核心观点：1) 控制论的反馈环路在工程实践中无处不在；"
                    "2) 工具本身就是控制系统的延伸；3) 软件工程的本质是信息流的控制。"
                ),
            },
        ]
        _, final_history = await _save_restore_cycle(
            session_manager,
            sid,
            history,
            [(f"Routine check #{i}: OK", f"hb_{i}") for i in range(3)],
            cycles=1,
        )

        assert "控制论" in _all_content(final_history), (
            "The original assistant response disappeared after a heartbeat cycle. "
            f"Snapshot: {_history_snapshot(final_history)}"
        )

    @pytest.mark.asyncio
    async def test_no_role_alternation_violation_after_full_cycle(
        self,
        session_manager: SessionManager,
        memory_patch,
    ):
        """Persisted history must not contain adjacent assistant messages."""
        sid = "tg_session_demo_agent__role_check"
        _, final_history = await _save_restore_cycle(
            session_manager,
            sid,
            [
                {"role": "user", "content": "question"},
                {"role": "assistant", "content": "answer"},
            ],
            [(f"Report #{i}", f"hb_{i}") for i in range(3)],
            cycles=1,
        )

        violations = _find_consecutive_role_violations(final_history)
        assert not violations, (
            f"Role alternation violation persisted after full cycle: {violations}. "
            f"Snapshot: {_history_snapshot(final_history)}"
        )

    @pytest.mark.asyncio
    async def test_cybernetics_content_visible_after_heartbeat_injection(
        self,
        session_manager: SessionManager,
        memory_patch,
    ):
        """A follow-up question must still see the original Cybernetics answer."""
        sid = "tg_session_demo_agent__8576399597"
        history = [
            {"role": "user", "content": "https://x.com/odysseus0z/status/2030416758138634583"},
            {
                "role": "assistant",
                "content": (
                    "推文核心内容 作者: George (@odysseus0z) "
                    "主题: Harness Engineering Is Cybernetics（工具工程即控制论）\n\n"
                    "核心论点:\n"
                    "1. 控制论（Cybernetics）是关于反馈环路和系统控制的学科\n"
                    "2. 工具工程本质上就是在构建控制系统\n"
                    "3. 每个工具都是人类意图的放大器和控制器"
                ),
            },
            {"role": "user", "content": "Cybernetics 具体说了什么？"},
        ]
        heartbeat_reports = [
            ("## 会话轨迹健康检测结果\n检测正常", "job_health_check"),
            (
                "## 每日论文报告\n"
                "| 项目 | 结果 |\n|------|------|\n"
                "| 论文数量 | 10篇 |\n\n"
                "**热点主题:**\n"
                "- **SkillNet**: Create, Evaluate, and Connect AI Skills\n",
                "job_paper_report",
            ),
            ("## 每日新闻简报\n国际热点新闻 TOP 10", "job_news"),
        ]

        ctx = Context()
        ctx.set_variable(KEY_HISTORY, history)
        agent = _make_mock_agent(ctx)
        await session_manager.save_session(sid, agent)
        for content, run_id in heartbeat_reports:
            await session_manager.deposit_mailbox_event(
                sid, _make_mailbox_event(content, run_id),
            )

        loaded = await session_manager.load_session(sid)
        assert loaded is not None
        fresh = _make_mock_agent()
        await session_manager.restore_to_agent(fresh, loaded)
        restored = fresh.get_context().get_var_value(KEY_HISTORY)
        assert isinstance(restored, list)

        all_content = _all_content(restored)
        assert "Cybernetics" in all_content, (
            f"Cybernetics content lost from restored context. Snapshot: {_history_snapshot(restored)}"
        )
        assert "控制论" in all_content, (
            f"Cybernetics analysis lost from restored context. Snapshot: {_history_snapshot(restored)}"
        )
        # With mailbox-only, heartbeats never enter history, so no answer-slot theft
        _assert_no_heartbeat_in_answer_slot(restored, "具体说了什么")

        violations = _find_consecutive_role_violations(restored)
        assert not violations, (
            f"Consecutive same-role messages after restore: {violations}. "
            f"Snapshot: {_history_snapshot(restored)}"
        )

    @pytest.mark.asyncio
    async def test_second_save_restore_cycle_preserves_content_and_structure(
        self,
        session_manager: SessionManager,
        memory_patch,
    ):
        """Two full cycles must not lose content or persist role violations."""
        sid = "tg_session_demo_agent__cycle2"
        _, final_history = await _save_restore_cycle(
            session_manager,
            sid,
            [
                {"role": "user", "content": "Tell me about Cybernetics"},
                {"role": "assistant", "content": "Harness Engineering Is Cybernetics - 详细分析"},
            ],
            [(f"Report #{i}: data", f"hb_{i}") for i in range(3)],
            cycles=2,
        )

        all_content = _all_content(final_history)
        assert "Harness" in all_content, (
            f"Original content lost after two cycles. Snapshot: {_history_snapshot(final_history)}"
        )
        assert "Cybernetics" in all_content, (
            f"Cybernetics content lost after two cycles. Snapshot: {_history_snapshot(final_history)}"
        )

        violations = _find_consecutive_role_violations(final_history)
        assert not violations, (
            f"Role alternation violation persisted after two cycles: {violations}. "
            f"Snapshot: {_history_snapshot(final_history)}"
        )

    @pytest.mark.asyncio
    async def test_heartbeat_messages_do_not_outnumber_chat_messages(
        self,
        session_manager: SessionManager,
        memory_patch,
    ):
        """Background reports should not dominate restored chat context."""
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

        # With mailbox-only, heartbeats never enter history as messages
        hb_msgs = [msg for msg in restored if isinstance(msg, dict) and _is_heartbeat(msg)]

        assert len(hb_msgs) == 0, (
            f"Heartbeat messages ({len(hb_msgs)}) found in history under mailbox-only. "
            f"Snapshot: {_history_snapshot(restored)}"
        )
