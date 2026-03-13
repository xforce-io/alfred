"""Integration test: heartbeat messages must remain visible to LLM after restore.

Reproduces the bug where:
1. Telegram channel injects heartbeat messages with metadata.source="heartbeat_delivery"
2. After prepare_for_restore normalizes (strips prefix), _is_heartbeat fails to identify
   them via metadata (expects "heartbeat", gets "heartbeat_delivery")
3. On subsequent save, compress_history treats them as regular chat messages
4. LLM loses context about system-delivered reports, leading to answers like
   "推文里没有提到 skillnet" when the heartbeat report clearly mentions it

Key assertions:
- Heartbeat messages are identifiable across save→restore cycles
- Heartbeat content is preserved (not compressed away) after multiple cycles
- LLM context (KEY_HISTORY) contains the heartbeat content after restore
"""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from dolphin.core.context.context import Context
from dolphin.core.common.constants import KEY_HISTORY

from src.everbot.core.session.session import SessionManager
from src.everbot.core.session.history_utils import (
    _is_heartbeat,
    _normalize_heartbeat,
    _HEARTBEAT_PREFIX,
)


# ── Helpers ──────────────────────────────────────────────────────────

_memory_patch = patch(
    "src.everbot.core.memory.manager.MemoryManager.process_session_end",
    new_callable=AsyncMock,
    return_value=None,
)


def _make_mock_agent(context: Context, name: str = "demo_agent"):
    from dolphin.sdk.agent.dolphin_agent_snapshot import DolphinAgentSnapshot

    class MockAgent:
        def __init__(self, ctx):
            self.executor = type("obj", (object,), {"context": ctx})
            self.name = name
            self.snapshot = DolphinAgentSnapshot(self)

        def get_context(self):
            return self.executor.context

    return MockAgent(context)


# ── Heartbeat message fixtures (matching real telegram_channel.py injection) ──

def _make_heartbeat_delivery_msg(content: str, run_id: str) -> dict:
    """Create a heartbeat message as telegram_channel.py would inject it.

    Note: telegram_channel.py sets source=source_type which is "heartbeat_delivery",
    NOT "heartbeat".  This is the root cause of the identification bug.
    """
    return {
        "role": "assistant",
        "content": f"{_HEARTBEAT_PREFIX}\n\n{content}",
        "metadata": {
            "source": "heartbeat_delivery",  # <-- as telegram_channel.py does
            "run_id": run_id,
            "injected_at": "2026-03-09T01:17:19.763033+00:00",
        },
    }


def _make_heartbeat_msg(content: str, run_id: str) -> dict:
    """Create a heartbeat message as heartbeat.py injects into primary session.

    Primary session injection uses source="heartbeat" (correct).
    """
    return {
        "role": "assistant",
        "content": content,
        "metadata": {
            "source": "heartbeat",
            "run_id": run_id,
            "injected_at": "2026-03-09T01:17:19.763033+00:00",
        },
    }


SKILLNET_REPORT = (
    "**✅ 任务执行成功 - 每日论文报告已生成**\n\n"
    "| 项目 | 结果 |\n|------|------|\n"
    "| 论文数量 | 10篇 |\n| 数据来源 | HuggingFace |\n\n"
    "**🔥 今日热点主题:**\n"
    "- **SkillNet**: Create, Evaluate, and Connect AI Skills | 64 👍\n"
    "- **Interactive Benchmarks**: 主动信息获取框架评估模型智能与推理能力\n"
)


# ── Bug 1: _is_heartbeat fails on "heartbeat_delivery" source ────────


class TestHeartbeatIdentification:
    """Verify _is_heartbeat correctly identifies heartbeat messages."""

    def test_primary_session_heartbeat_identified_by_metadata(self):
        """Primary session heartbeats (source="heartbeat") should be identified."""
        msg = _make_heartbeat_msg("some report", "job_abc123")
        assert _is_heartbeat(msg), (
            "_is_heartbeat should identify messages with metadata.source='heartbeat'"
        )

    def test_channel_session_heartbeat_identified_by_prefix(self):
        """Channel session heartbeats have prefix, so first-time identification works."""
        msg = _make_heartbeat_delivery_msg("some report", "job_abc123")
        assert _is_heartbeat(msg), (
            "_is_heartbeat should identify messages via content prefix "
            "even when metadata.source='heartbeat_delivery'"
        )

    def test_channel_heartbeat_after_normalize_loses_identity(self):
        """BUG: After _normalize_heartbeat strips prefix, _is_heartbeat fails.

        This is the core bug: _normalize_heartbeat strips the content prefix
        but metadata.source remains "heartbeat_delivery" (not "heartbeat"),
        so _is_heartbeat returns False on subsequent cycles.
        """
        msg = _make_heartbeat_delivery_msg(SKILLNET_REPORT, "job_paper_report")
        normalized = _normalize_heartbeat(msg)

        # Prefix should be stripped
        assert not normalized["content"].startswith(_HEARTBEAT_PREFIX), (
            "_normalize_heartbeat should strip the prefix"
        )

        # BUG: After normalization, _is_heartbeat should still identify it,
        # but it fails because source="heartbeat_delivery" != "heartbeat"
        # and prefix is now gone.
        is_still_heartbeat = _is_heartbeat(normalized)
        assert is_still_heartbeat, (
            "CRITICAL BUG: After _normalize_heartbeat, message is no longer "
            "identifiable as heartbeat. metadata.source='heartbeat_delivery' "
            "doesn't match _is_heartbeat check for 'heartbeat', and prefix "
            "has been stripped. This causes compress_history to treat it as "
            "a regular chat message on subsequent save cycles."
        )


# ── Bug 2: heartbeat content lost after save→restore→save cycle ──────


def test_heartbeat_content_survives_save_restore_cycle():
    """Heartbeat messages must remain identifiable after a full save→restore→save cycle.

    Simulates:
    1. Channel session with chat history + heartbeat reports (as Telegram injects them)
    2. Save session (compress_history runs)
    3. Restore to a new agent (prepare_for_restore normalizes heartbeat)
    4. Save again (compress_history runs on normalized messages)
    5. Verify heartbeat content (SkillNet report) is still present
    """
    async def _run():
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir)
            manager = SessionManager(session_dir)
            session_id = "tg_session_demo_agent__8576399597"

            # Build realistic history: chat turns + heartbeat reports
            history = []
            # 30 chat turns (60 messages)
            for i in range(30):
                history.append({"role": "user", "content": f"user message {i}"})
                history.append({"role": "assistant", "content": f"assistant reply {i}"})

            # Last user message + assistant reply (the tweet discussion)
            history.append({"role": "user", "content": "https://x.com/odysseus0z/status/2030416758138634583"})
            history.append({
                "role": "assistant",
                "content": "我无法直接访问这条 X/Twitter 链接。",
            })

            # Heartbeat-delivered reports (as telegram_channel.py injects them)
            history.append(_make_heartbeat_delivery_msg(
                "## 会话轨迹健康检测结果\n检测正常", "job_health_check"
            ))
            history.append(_make_heartbeat_delivery_msg(
                SKILLNET_REPORT, "job_paper_report"
            ))
            history.append(_make_heartbeat_delivery_msg(
                "## 每日新闻简报\n国际热点新闻", "job_news"
            ))

            # Cycle 1: Save
            context = Context()
            context.set_variable(KEY_HISTORY, history)
            agent = _make_mock_agent(context)
            await manager.save_session(session_id, agent)

            # Cycle 1: Load + Restore to new agent
            loaded = await manager.load_session(session_id)
            assert loaded is not None

            fresh_context = Context()
            fresh_agent = _make_mock_agent(fresh_context)
            await manager.restore_to_agent(fresh_agent, loaded)

            # Check: SkillNet content must be in the restored history
            restored_history = fresh_context.get_var_value(KEY_HISTORY)
            all_content = " ".join(
                m.get("content", "") for m in restored_history
                if isinstance(m, dict) and isinstance(m.get("content"), str)
            )
            assert "SkillNet" in all_content, (
                "After first restore, SkillNet report content is missing from LLM context"
            )

            # Cycle 2: Save again (this is where the bug manifests)
            await manager.save_session(session_id, fresh_agent)

            # Cycle 2: Load + Restore again
            loaded2 = await manager.load_session(session_id)
            fresh_context2 = Context()
            fresh_agent2 = _make_mock_agent(fresh_context2)
            await manager.restore_to_agent(fresh_agent2, loaded2)

            # Check: SkillNet content must STILL be present after second cycle
            restored_history2 = fresh_context2.get_var_value(KEY_HISTORY)
            all_content2 = " ".join(
                m.get("content", "") for m in restored_history2
                if isinstance(m, dict) and isinstance(m.get("content"), str)
            )
            assert "SkillNet" in all_content2, (
                "CRITICAL BUG: After second save→restore cycle, SkillNet report "
                "content has been lost. This is because _normalize_heartbeat "
                "stripped the prefix and metadata.source='heartbeat_delivery' "
                "is not recognized by _is_heartbeat, so compress_history "
                "treats the heartbeat message as regular chat and may compress it away."
            )

    with _memory_patch:
        asyncio.run(_run())


def test_heartbeat_not_misclassified_as_chat_after_normalize():
    """After normalize + save, heartbeat messages must not be counted as chat_msgs
    in compress_history, which would inflate token count and potentially trigger
    unwanted compression.
    """
    async def _run():
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir)
            manager = SessionManager(session_dir)
            session_id = "tg_session_demo_agent__8576399597"

            # Build history with heartbeat messages
            history = [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ]
            # Add heartbeat messages as telegram_channel.py would
            for i in range(5):
                history.append(_make_heartbeat_delivery_msg(
                    f"Report {i}: " + "x" * 500, f"job_{i}"
                ))

            # Save (this triggers compress_history which separates chat from heartbeat)
            context = Context()
            context.set_variable(KEY_HISTORY, history)
            agent = _make_mock_agent(context)
            await manager.save_session(session_id, agent)

            # Load, restore, then save again
            loaded = await manager.load_session(session_id)
            fresh_context = Context()
            fresh_agent = _make_mock_agent(fresh_context)
            await manager.restore_to_agent(fresh_agent, loaded)
            await manager.save_session(session_id, fresh_agent)

            # Load after second save - heartbeat messages should still be identifiable
            loaded2 = await manager.load_session(session_id)
            heartbeat_count = sum(
                1 for m in loaded2.history_messages if _is_heartbeat(m)
            )
            assert heartbeat_count >= 5, (
                f"After save→restore→save cycle, only {heartbeat_count}/5 heartbeat "
                f"messages are still identifiable by _is_heartbeat. "
                f"This means they will be misclassified as chat messages in "
                f"compress_history, leading to potential content loss."
            )

    with _memory_patch:
        asyncio.run(_run())


# ── Bug 3: LLM context lacks framing for heartbeat messages ─────────


class TestHeartbeatContextFraming:
    """Verify heartbeat messages have adequate framing for LLM context."""

    def test_normalized_heartbeat_retains_context_marker(self):
        """After normalization, heartbeat messages should retain some indicator
        that they are system-delivered reports, not the assistant's own responses.

        Without this, the LLM confuses heartbeat reports with its own prior
        responses and fails to reference content from them when the user asks
        "介绍下上面提到的 skillnet".
        """
        msg = _make_heartbeat_delivery_msg(SKILLNET_REPORT, "job_paper_report")
        normalized = _normalize_heartbeat(msg)

        content = normalized.get("content", "")
        # The content should have SOME indication it's a system-delivered report
        # Either the original prefix, a shortened marker, or metadata the LLM can see
        has_any_marker = (
            content.startswith(_HEARTBEAT_PREFIX)
            or "系统" in content[:50]
            or "后台" in content[:50]
            or "推送" in content[:50]
            or "报告" in content[:20]
        )
        assert has_any_marker, (
            "After normalization, heartbeat message has NO contextual marker. "
            "The LLM will not be able to distinguish this from its own prior "
            "responses, leading to context confusion when the user references "
            "content from the report."
        )


# ── End-to-end: realistic scenario from the bug report ──────────────


def test_e2e_user_can_reference_heartbeat_report_content():
    """End-to-end simulation of the bug scenario:

    1. Agent has a chat about a tweet URL (failed to access)
    2. Heartbeat delivers a paper report mentioning SkillNet
    3. User asks "介绍下上面提到的 skillnet"
    4. The LLM context MUST contain the SkillNet content from the heartbeat report

    This doesn't test the LLM response quality, but ensures the content is
    available in the context that would be sent to the LLM.
    """
    async def _run():
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir)
            manager = SessionManager(session_dir)
            session_id = "tg_session_demo_agent__8576399597"

            # Build the exact session history from the bug report
            history = [
                {"role": "user", "content": "https://x.com/odysseus0z/status/2030416758138634583?s=20"},
                {"role": "assistant", "content": (
                    "我无法直接访问这条 X/Twitter 链接。原因是：\n"
                    "1. **登录墙** — X 现在强制要求登录才能查看推文内容\n"
                    "2. **JavaScript 渲染** — 推文内容需要浏览器执行 JS 才能加载"
                )},
                # Heartbeat-delivered reports (as Telegram injects them)
                _make_heartbeat_delivery_msg(
                    "## 会话轨迹健康检测结果\n检测正常",
                    "job_health_check",
                ),
                _make_heartbeat_delivery_msg(
                    SKILLNET_REPORT,
                    "job_paper_report",
                ),
                _make_heartbeat_delivery_msg(
                    "## 每日新闻简报\n国际热点新闻 TOP 10",
                    "job_news",
                ),
            ]

            # Save the session
            context = Context()
            context.set_variable(KEY_HISTORY, history)
            agent = _make_mock_agent(context)
            await manager.save_session(session_id, agent)

            # Simulate: user opens chat, session is restored
            loaded = await manager.load_session(session_id)
            fresh_context = Context()
            fresh_agent = _make_mock_agent(fresh_context)
            await manager.restore_to_agent(fresh_agent, loaded)

            # Now the user sends: "介绍下上面提到的 skillnet"
            # At this point, the LLM context should contain the SkillNet report
            restored = fresh_context.get_var_value(KEY_HISTORY)
            assert isinstance(restored, list), "KEY_HISTORY should be a list"

            # Collect all assistant message content that the LLM will see
            llm_visible_content = []
            for msg in restored:
                if isinstance(msg, dict) and msg.get("role") == "assistant":
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        llm_visible_content.append(content)

            full_text = "\n".join(llm_visible_content)
            assert "SkillNet" in full_text, (
                "The SkillNet report from the heartbeat delivery is NOT visible "
                "in the LLM context after session restore. The LLM will not be "
                "able to answer 'introduce the SkillNet mentioned above'.\n\n"
                f"Assistant messages in context ({len(llm_visible_content)}):\n"
                + "\n---\n".join(c[:100] for c in llm_visible_content)
            )

    with _memory_patch:
        asyncio.run(_run())
