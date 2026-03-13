"""Tests for history policy: heartbeat isolation + token-budget compact."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.everbot.core.session.history_utils import (
    MAX_HEARTBEAT_MESSAGES,
    COMPACT_WINDOW_TOKENS,
    _HEARTBEAT_CONTEXT_MARKER,
    _is_heartbeat,
    _is_placeholder,
    _estimate_tokens,
    _normalize_heartbeat,
    evict_oldest_heartbeat,
    prepare_for_restore,
)
from src.everbot.core.session.compressor import (
    SessionCompressor,
    WINDOW_SIZE,
)
from src.everbot.core.session.persistence import SessionPersistence


# ── Test factories ────────────────────────────────────────────────────


def _user(content, **meta):
    msg = {"role": "user", "content": content}
    if meta:
        msg["metadata"] = meta
    return msg


def _assistant(content, **meta):
    msg = {"role": "assistant", "content": content}
    if meta:
        msg["metadata"] = meta
    return msg


def _heartbeat_msg(run_id, content="检测正常"):
    return _assistant(content, source="heartbeat", run_id=run_id)


def _placeholder_ack(run_id):
    return _assistant("(acknowledged)", source="system", category="placeholder", run_id=run_id)


def _placeholder_bg(run_id):
    return _user("[Background notification follows]", source="system", category="placeholder", run_id=run_id)


def _heartbeat_turn(run_id, content="检测正常"):
    """A complete heartbeat turn: ack placeholder + bg placeholder + heartbeat message."""
    return [_placeholder_ack(run_id), _placeholder_bg(run_id), _heartbeat_msg(run_id, content)]


def _legacy_heartbeat(content="结果正常"):
    """Legacy heartbeat message (content prefix identification)."""
    return _assistant(f"[此消息由心跳系统自动执行例行任务生成]\n\n{content}")


# ── 6.2 TestIsHeartbeat ──────────────────────────────────────────────


class TestIsHeartbeat:
    def test_metadata_heartbeat(self):
        msg = {"role": "assistant", "content": "ok", "metadata": {"source": "heartbeat"}}
        assert _is_heartbeat(msg) is True

    def test_legacy_prefix(self):
        msg = _legacy_heartbeat("test")
        assert _is_heartbeat(msg) is True

    def test_normal_assistant(self):
        msg = {"role": "assistant", "content": "hello"}
        assert _is_heartbeat(msg) is False

    def test_placeholder_not_heartbeat(self):
        msg = {"role": "assistant", "content": "(acknowledged)", "metadata": {"source": "system"}}
        assert _is_heartbeat(msg) is False

    def test_no_metadata(self):
        msg = {"role": "assistant", "content": "regular message"}
        assert _is_heartbeat(msg) is False


# ── 6.2b TestIsPlaceholder ───────────────────────────────────────────


class TestIsPlaceholder:
    def test_metadata_placeholder(self):
        msg = {"role": "assistant", "content": "(acknowledged)",
               "metadata": {"category": "placeholder", "source": "system"}}
        assert _is_placeholder(msg) is True

    def test_legacy_acknowledged(self):
        msg = {"role": "assistant", "content": "(acknowledged)"}
        assert _is_placeholder(msg) is True

    def test_legacy_bg_notification(self):
        msg = {"role": "user", "content": "[Background notification follows]"}
        assert _is_placeholder(msg) is True

    def test_normal_user_message(self):
        msg = {"role": "user", "content": "hello"}
        assert _is_placeholder(msg) is False

    def test_partial_match_not_placeholder(self):
        msg = {"role": "assistant", "content": "(acknowledged) and more"}
        assert _is_placeholder(msg) is False


# ── 6.4b TestEstimateTokens ──────────────────────────────────────────


class TestEstimateTokens:
    def test_simple_string_content(self):
        msgs = [{"role": "user", "content": "a" * 300}]
        tokens = _estimate_tokens(msgs)
        # (300 + 12) // 3 = 104
        assert tokens == 104

    def test_list_content_with_text(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "a" * 300}]}]
        tokens = _estimate_tokens(msgs)
        assert tokens == 104

    def test_empty_messages(self):
        assert _estimate_tokens([]) == 0

    def test_tool_calls_counted(self):
        msg_plain = {"role": "assistant", "content": "a" * 100}
        msg_tool = {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_abc123",
                "function": {"name": "bash", "arguments": '{"cmd":"ls -la"}'},
            }],
        }
        _estimate_tokens([msg_plain])
        tokens_tool = _estimate_tokens([msg_tool])
        # tool msg has function name + arguments + id counted
        assert tokens_tool > 0
        # Both have overhead, but tool has extra from tool_calls
        assert tokens_tool > _estimate_tokens([{"role": "assistant", "content": ""}])

    def test_tool_response_counted(self):
        msg = {"role": "tool", "content": "result data", "tool_call_id": "call_abc123"}
        tokens = _estimate_tokens([msg])
        # content + tool_call_id + overhead all counted
        assert tokens > 0

    def test_tool_heavy_vs_chat_only(self):
        chat_msgs = [{"role": "user", "content": "a" * 100} for _ in range(10)]
        tool_msgs = [
            {
                "role": "assistant",
                "content": "a" * 100,
                "tool_calls": [{
                    "id": f"call_{i}",
                    "function": {"name": "execute", "arguments": "b" * 500},
                }],
            }
            for i in range(10)
        ]
        chat_tokens = _estimate_tokens(chat_msgs)
        tool_tokens = _estimate_tokens(tool_msgs)
        assert tool_tokens > chat_tokens * 3

    def test_message_overhead(self):
        msg = {"role": "assistant", "content": ""}
        tokens = _estimate_tokens([msg])
        assert tokens > 0  # structural overhead


# ── 6.3 TestEvictOldestHeartbeat ─────────────────────────────────────


class TestEvictOldestHeartbeat:
    def test_under_limit_no_change(self):
        hb = [_heartbeat_msg(f"hb_{i}") for i in range(5)]
        result = evict_oldest_heartbeat(hb)
        assert result == hb

    def test_evict_oldest_fifo(self):
        hb = [_heartbeat_msg(f"hb_{i}") for i in range(25)]
        result = evict_oldest_heartbeat(hb)
        assert len(result) == MAX_HEARTBEAT_MESSAGES
        # Kept the newest 20
        assert result[0] == hb[5]
        assert result[-1] == hb[24]

    def test_placeholder_evicted_with_heartbeat_new_format(self):
        history = []
        for i in range(25):
            history.extend(_heartbeat_turn(f"hb_{i}"))
        result = evict_oldest_heartbeat(history)
        # 20 heartbeats * 3 messages each = 60
        hb_count = sum(1 for m in result if _is_heartbeat(m))
        assert hb_count == MAX_HEARTBEAT_MESSAGES
        # No orphan placeholders (each placeholder should have its heartbeat)
        ph_count = sum(1 for m in result if _is_placeholder(m))
        assert ph_count == MAX_HEARTBEAT_MESSAGES * 2

    def test_placeholder_evicted_with_heartbeat_legacy(self):
        history = []
        for i in range(25):
            # Legacy format: bare placeholders + legacy heartbeat
            history.append({"role": "assistant", "content": "(acknowledged)"})
            history.append({"role": "user", "content": "[Background notification follows]"})
            history.append(_legacy_heartbeat(f"result_{i}"))
        result = evict_oldest_heartbeat(history)
        hb_count = sum(1 for m in result if _is_heartbeat(m))
        assert hb_count == MAX_HEARTBEAT_MESSAGES
        # Legacy placeholders should also be evicted
        total = len(result)
        assert total == MAX_HEARTBEAT_MESSAGES * 3  # 20 turns of 3 msgs each

    def test_placeholder_evicted_mixed_format(self):
        history = []
        # 13 new-format turns
        for i in range(13):
            history.extend(_heartbeat_turn(f"hb_{i}"))
        # 12 legacy turns
        for i in range(12):
            history.append({"role": "assistant", "content": "(acknowledged)"})
            history.append({"role": "user", "content": "[Background notification follows]"})
            history.append(_legacy_heartbeat(f"legacy_{i}"))
        result = evict_oldest_heartbeat(history)
        hb_count = sum(1 for m in result if _is_heartbeat(m))
        assert hb_count == MAX_HEARTBEAT_MESSAGES

    def test_chat_messages_preserved(self):
        history = []
        chat_msgs = [_user(f"chat_{i}") for i in range(10)]
        hb_msgs = [_heartbeat_msg(f"hb_{i}") for i in range(25)]
        history = chat_msgs + hb_msgs
        result = evict_oldest_heartbeat(history)
        # All chat preserved
        result_chat = [m for m in result if not _is_heartbeat(m)]
        assert len(result_chat) == 10
        assert result_chat == chat_msgs

    def test_legacy_heartbeat_evicted(self):
        history = [_legacy_heartbeat(f"r_{i}") for i in range(25)]
        result = evict_oldest_heartbeat(history)
        assert len(result) == MAX_HEARTBEAT_MESSAGES

    def test_empty_history(self):
        assert evict_oldest_heartbeat([]) == []

    def test_no_heartbeat(self):
        history = [_user(f"msg_{i}") for i in range(10)]
        result = evict_oldest_heartbeat(history)
        assert result == history

    def test_interleaved_order_preserved(self):
        history = []
        for i in range(25):
            history.append(_user(f"chat_{i}"))
            history.append(_heartbeat_msg(f"hb_{i}"))
        result = evict_oldest_heartbeat(history)
        hb_count = sum(1 for m in result if _is_heartbeat(m))
        assert hb_count == MAX_HEARTBEAT_MESSAGES
        # Relative order preserved
        chat_in_result = [m for m in result if not _is_heartbeat(m)]
        assert len(chat_in_result) == 25  # all chat preserved
        # Check order: each chat should come before any later chat
        for i in range(len(chat_in_result) - 1):
            idx_a = result.index(chat_in_result[i])
            idx_b = result.index(chat_in_result[i + 1])
            assert idx_a < idx_b


# ── 6.4 TestSaveCompact ─────────────────────────────────────────────


class TestTokenBudgetWindowEdge:
    """Edge cases for token-budget window in compressor."""

    @pytest.mark.asyncio
    async def test_last_message_exceeds_budget_still_kept(self):
        """When the last message alone exceeds token_budget, it must still be kept verbatim."""
        ctx = MagicMock()
        compressor = SessionCompressor(ctx)
        # Small messages + one huge trailing message
        msgs = [{"role": "user", "content": f"msg-{i}"} for i in range(10)]
        msgs.append({"role": "assistant", "content": "x" * 200_000})  # ~66K tokens alone

        with patch.object(compressor, "_generate_summary", new_callable=AsyncMock) as mock_gen:
            mock_gen.return_value = "Summary"
            compressed, result = await compressor.maybe_compress(
                msgs, token_budget=COMPACT_WINDOW_TOKENS,
            )

        assert compressed is True
        # The huge last message must be in to_keep (verbatim), not summarized
        assert any(len(m.get("content", "")) > 100_000 for m in result)

    @pytest.mark.asyncio
    async def test_single_message_exceeds_budget_no_compress(self):
        """A single message that exceeds token_budget: nothing to compress, kept as-is."""
        ctx = MagicMock()
        compressor = SessionCompressor(ctx)
        msgs = [{"role": "assistant", "content": "x" * 200_000}]

        compressed, result = await compressor.maybe_compress(
            msgs, token_budget=COMPACT_WINDOW_TOKENS,
        )
        # to_compress is empty → no compression
        assert compressed is False
        assert result is msgs


class TestCompressHistory:
    """Tests for the shared compress_history() entry point used by both save paths."""

    @pytest.mark.asyncio
    async def test_chat_below_budget_no_compact(self):
        """Heartbeat tokens should NOT count toward compact decision."""
        ctx = MagicMock()
        compressor = SessionCompressor(ctx)
        # Chat: ~10K tokens (30K chars)
        chat_msgs = [{"role": "user", "content": "a" * 3000} for _ in range(10)]
        # Heartbeat: ~5K tokens (15K chars) — should be excluded from budget
        hb_msgs = [_heartbeat_msg(f"hb_{i}", "b" * 1500) for i in range(10)]
        history = chat_msgs + hb_msgs

        with patch.object(compressor, "_generate_summary", new_callable=AsyncMock) as mock_gen:
            result = await compressor.compress_history(history)
            mock_gen.assert_not_called()

        assert result is history  # unchanged

    @pytest.mark.asyncio
    async def test_chat_above_budget_triggers_compact(self):
        """When chat tokens > COMPACT_TOKEN_BUDGET, compact is triggered."""
        ctx = MagicMock()
        compressor = SessionCompressor(ctx)
        # Chat: ~50K tokens (150K chars)
        chat_msgs = [{"role": "user", "content": "a" * 3000} for _ in range(50)]
        hb_msgs = [_heartbeat_msg(f"hb_{i}") for i in range(3)]
        history = chat_msgs + hb_msgs

        with patch.object(compressor, "_generate_summary", new_callable=AsyncMock) as mock_gen:
            mock_gen.return_value = "Compressed summary"
            result = await compressor.compress_history(history)

        mock_gen.assert_called_once()
        assert len(result) < len(history)

    @pytest.mark.asyncio
    async def test_heartbeat_preserved_after_compact(self):
        """After compact, heartbeat messages should still be in result."""
        ctx = MagicMock()
        compressor = SessionCompressor(ctx)
        chat_msgs = [{"role": "user", "content": "a" * 3000} for _ in range(50)]
        hb_msgs = [_heartbeat_msg(f"hb_{i}") for i in range(5)]
        history = chat_msgs + hb_msgs

        with patch.object(compressor, "_generate_summary", new_callable=AsyncMock) as mock_gen:
            mock_gen.return_value = "Summary"
            result = await compressor.compress_history(history)

        hb_in_result = [m for m in result if _is_heartbeat(m)]
        assert len(hb_in_result) == 5

    @pytest.mark.asyncio
    async def test_heartbeat_appended_to_tail(self):
        """Heartbeat messages should appear after compacted chat."""
        ctx = MagicMock()
        compressor = SessionCompressor(ctx)
        chat_msgs = [{"role": "user", "content": "a" * 3000} for _ in range(50)]
        hb_msgs = [_heartbeat_msg(f"hb_{i}") for i in range(3)]
        history = chat_msgs + hb_msgs

        with patch.object(compressor, "_generate_summary", new_callable=AsyncMock) as mock_gen:
            mock_gen.return_value = "Summary"
            result = await compressor.compress_history(history)

        last_chat_idx = max(i for i, m in enumerate(result) if not _is_heartbeat(m))
        for i, m in enumerate(result):
            if _is_heartbeat(m):
                assert i > last_chat_idx

    @pytest.mark.asyncio
    async def test_no_heartbeat_same_as_before(self):
        """Without heartbeat, legacy maybe_compress path still works."""
        ctx = MagicMock()
        compressor = SessionCompressor(ctx)
        # 100 messages > COMPRESS_THRESHOLD
        msgs = []
        for i in range(50):
            msgs.append({"role": "user", "content": f"msg-{i}"})
            msgs.append({"role": "assistant", "content": f"reply-{i}"})

        with patch.object(compressor, "_generate_summary", new_callable=AsyncMock) as mock_gen:
            mock_gen.return_value = "Summary"
            compressed, result = await compressor.maybe_compress(msgs)

        assert compressed is True
        assert len(result) == 2 + WINDOW_SIZE


# ── 6.5 TestRestoreFilter ───────────────────────────────────────────


class TestRestoreFilter:
    """Tests for prepare_for_restore: placeholders stripped, heartbeat content preserved."""

    def test_heartbeat_content_preserved(self):
        history = [_user("hi"), *_heartbeat_turn("hb_1"), _user("bye")]
        result = prepare_for_restore(history)
        # user("hi") + heartbeat_result(normalized) + user("bye") = 3
        assert len(result) == 3
        assert result[1]["role"] == "assistant"
        assert result[1]["content"] == _HEARTBEAT_CONTEXT_MARKER + "检测正常"
        # metadata.source must be preserved so heartbeat stays identifiable
        assert _is_heartbeat(result[1])

    def test_placeholder_removed_heartbeat_kept(self):
        history = _heartbeat_turn("hb_1")
        result = prepare_for_restore(history)
        # 2 placeholders removed, 1 heartbeat result kept
        assert len(result) == 1
        assert result[0]["content"] == _HEARTBEAT_CONTEXT_MARKER + "检测正常"
        assert _is_heartbeat(result[0])

    def test_legacy_placeholder_removed(self):
        history = [
            {"role": "assistant", "content": "(acknowledged)"},
            {"role": "user", "content": "[Background notification follows]"},
        ]
        result = prepare_for_restore(history)
        assert len(result) == 0

    def test_legacy_heartbeat_normalized(self):
        history = [_legacy_heartbeat("test")]
        result = prepare_for_restore(history)
        assert len(result) == 1
        assert result[0]["content"] == _HEARTBEAT_CONTEXT_MARKER + "test"
        assert _is_heartbeat(result[0])

    def test_chat_preserved_with_heartbeat(self):
        history = [_user("hi"), _assistant("hello"), *_heartbeat_turn("hb_1"), _user("q")]
        result = prepare_for_restore(history)
        # user("hi") + assistant("hello") + heartbeat(normalized) + user("q") = 4
        assert len(result) == 4
        assert result[0]["content"] == "hi"
        assert result[1]["content"] == "hello"
        assert result[2]["content"] == _HEARTBEAT_CONTEXT_MARKER + "检测正常"
        assert result[3]["content"] == "q"

    def test_preserves_valid_structure(self):
        """After prepare_for_restore, all messages are valid dicts with role."""
        history = [
            _user("hi"),
            _assistant("hello"),
            *_heartbeat_turn("hb_1"),
            _user("follow-up"),
            _assistant("sure"),
        ]
        result = prepare_for_restore(history)
        assert len(result) == 5
        assert all(isinstance(m, dict) and "role" in m for m in result)
        assert result[0]["content"] == "hi"
        assert result[-1]["content"] == "sure"

    def test_normalize_preserves_traceability(self):
        """run_id, source, and injected_at survive normalization."""
        hb = _heartbeat_msg("run_42", "paper report")
        normalized = _normalize_heartbeat(hb)
        assert normalized["metadata"]["run_id"] == "run_42"
        assert normalized["metadata"]["source"] == "heartbeat"

    def test_normalize_strips_legacy_prefix(self):
        legacy = _legacy_heartbeat("actual content")
        normalized = _normalize_heartbeat(legacy)
        assert normalized["content"] == _HEARTBEAT_CONTEXT_MARKER + "actual content"
        assert "metadata" not in normalized or normalized.get("metadata") is None

    def test_old_filter_still_works(self):
        """_filter_heartbeat_messages still strips all heartbeat messages (backward compat)."""
        history = [_user("hi"), *_heartbeat_turn("hb_1"), _user("bye")]
        result = SessionPersistence._filter_heartbeat_messages(history)
        assert len(result) == 2
        assert all(not _is_heartbeat(m) for m in result)


# ── 6.6 TestEndToEnd ────────────────────────────────────────────────


class TestInjectionPathConsistency:
    """Both heartbeat injection paths should produce metadata-only format (no content prefix)."""

    @pytest.mark.asyncio
    async def test_cron_delivery_no_prefix(self):
        """CronDelivery.inject_to_history should NOT add content prefix."""
        from src.everbot.core.runtime.cron_delivery import CronDelivery

        cd = CronDelivery(
            session_manager=MagicMock(),
            primary_session_id="test",
            heartbeat_session_id="hb_test",
            agent_name="agent",
        )
        captured = {}

        async def capture_inject(session_id, message, **kwargs):
            captured.update(message)
            return True

        cd.session_manager.inject_history_message = AsyncMock(side_effect=capture_inject)

        await cd.inject_to_history("检测正常", "run_123")
        # Content should NOT have the legacy prefix
        assert not captured["content"].startswith("[此消息由心跳系统自动执行例行任务生成]")
        assert captured["content"] == "检测正常"
        assert captured["metadata"]["source"] == "heartbeat"

    def test_heartbeat_runner_no_prefix(self):
        """HeartbeatRunner._inject_result_to_primary_history should NOT add content prefix."""
        # Read the source to verify no prefix in the method
        import inspect
        from src.everbot.core.runtime.heartbeat import HeartbeatRunner

        source = inspect.getsource(HeartbeatRunner._inject_result_to_primary_history)
        assert "[此消息由心跳系统自动执行例行任务生成]" not in source


class TestEndToEnd:
    def test_inject_accumulation_then_evict(self):
        """Simulate 30 heartbeat injections; eviction caps at 20."""
        history = []
        for i in range(30):
            history.extend(_heartbeat_turn(f"hb_{i}"))
            history = evict_oldest_heartbeat(history)

        hb_count = sum(1 for m in history if _is_heartbeat(m))
        assert hb_count == MAX_HEARTBEAT_MESSAGES

    def test_metadata_roundtrip(self):
        """Metadata survives serialization roundtrip."""
        import json

        hb = _heartbeat_msg("hb_test", "data")
        serialized = json.dumps(hb)
        loaded = json.loads(serialized)
        assert _is_heartbeat(loaded) is True
        assert loaded["metadata"]["run_id"] == "hb_test"

    def test_backward_compatibility(self):
        """Mixed legacy + new format: all correctly handled across full pipeline."""
        history = []
        # 15 new-format
        for i in range(15):
            history.extend(_heartbeat_turn(f"hb_{i}"))
        # 10 legacy
        for i in range(10):
            history.append(_legacy_heartbeat(f"legacy_{i}"))
        # 5 chat
        for i in range(5):
            history.append(_user(f"chat_{i}"))

        # inject eviction
        evicted = evict_oldest_heartbeat(history)
        hb_count = sum(1 for m in evicted if _is_heartbeat(m))
        assert hb_count == MAX_HEARTBEAT_MESSAGES

        # restore: placeholders stripped, heartbeat content preserved with metadata
        restored = prepare_for_restore(evicted)
        assert all(not _is_placeholder(m) for m in restored)
        # Chat preserved
        chat_msgs = [m for m in restored if m.get("role") == "user"]
        assert len(chat_msgs) == 5
        # Heartbeat content preserved (normalized) — count matches post-eviction cap
        hb_restored = [m for m in restored if m.get("role") == "assistant"]
        assert len(hb_restored) == MAX_HEARTBEAT_MESSAGES


# ── 7. extract_recent_heartbeat ──────────────────────────────────────


class TestExtractRecentHeartbeat:
    """Tests for cross-session heartbeat context extraction."""

    def test_extracts_heartbeat_messages(self):
        from src.everbot.core.session.history_utils import extract_recent_heartbeat

        history = [
            _user("hi"),
            _assistant("hello"),
            _heartbeat_msg("hb_1", "report 1"),
            _user("thanks"),
            _heartbeat_msg("hb_2", "report 2"),
        ]
        result = extract_recent_heartbeat(history)
        assert len(result) == 2
        assert result[0]["content"] == _HEARTBEAT_CONTEXT_MARKER + "report 1"
        assert result[1]["content"] == _HEARTBEAT_CONTEXT_MARKER + "report 2"
        # metadata.source preserved for identification across round-trips
        for m in result:
            meta = m.get("metadata") or {}
            assert meta.get("source") == "heartbeat"

    def test_respects_max_count(self):
        from src.everbot.core.session.history_utils import extract_recent_heartbeat

        history = [_heartbeat_msg(f"hb_{i}", f"report {i}") for i in range(10)]
        result = extract_recent_heartbeat(history, max_count=3)
        assert len(result) == 3
        assert result[0]["content"] == _HEARTBEAT_CONTEXT_MARKER + "report 7"
        assert result[2]["content"] == _HEARTBEAT_CONTEXT_MARKER + "report 9"

    def test_empty_history(self):
        from src.everbot.core.session.history_utils import extract_recent_heartbeat

        assert extract_recent_heartbeat([]) == []

    def test_no_heartbeat_messages(self):
        from src.everbot.core.session.history_utils import extract_recent_heartbeat

        history = [_user("hi"), _assistant("hello")]
        assert extract_recent_heartbeat(history) == []

    def test_normalizes_legacy_prefix(self):
        from src.everbot.core.session.history_utils import extract_recent_heartbeat

        history = [_legacy_heartbeat("legacy content")]
        result = extract_recent_heartbeat(history)
        assert len(result) == 1
        assert result[0]["content"] == _HEARTBEAT_CONTEXT_MARKER + "legacy content"


class TestRestoreWithHeartbeatContext:
    """Tests verifying heartbeat_context is ignored (mailbox-only architecture).

    After the mailbox-only migration, heartbeat results are delivered via
    session mailbox (deposit_mailbox_event) and consumed as "## Background
    Updates" prefix on the next user turn. The heartbeat_context parameter
    in restore_to_agent is deprecated and ignored.
    """

    @pytest.mark.asyncio
    async def test_heartbeat_context_ignored_history_unchanged(self):
        """Passing heartbeat_context should not alter restored history."""
        persistence = SessionPersistence(__import__("tempfile").mkdtemp())
        agent = MagicMock()
        agent.snapshot.import_portable_session = MagicMock(return_value={})

        from src.everbot.core.session.session_data import SessionData
        session_data = SessionData(
            session_id="tg_session_alice__123",
            agent_name="alice",
            model_name="test",
            session_type="channel",
            history_messages=[
                _user("Ki Editor 是什么"),
                _assistant("Ki Editor 是一个基于 AST 操作的编辑器..."),
                _user("是给人用的还是给机器用的"),  # unanswered
            ],
            variables={},
            created_at="2026-01-01",
            updated_at="2026-01-01",
        )
        heartbeat_ctx = [
            {"role": "assistant", "content": "## Session Health Check - Complete"},
        ]

        await persistence.restore_to_agent(agent, session_data, heartbeat_context=heartbeat_ctx)

        call_args = agent.snapshot.import_portable_session.call_args
        history = call_args[0][0]["history_messages"]

        # heartbeat_context is ignored — history should only contain the original 3 messages
        assert len(history) == 3
        assert history[-1].get("content") == "是给人用的还是给机器用的"
        assert not any(
            "Health Check" in (m.get("content") or "") for m in history
        ), "heartbeat_context should be ignored under mailbox-only architecture"

    @pytest.mark.asyncio
    async def test_no_heartbeat_context(self):
        """Without heartbeat_context, restore behaves as before."""
        persistence = SessionPersistence(__import__("tempfile").mkdtemp())
        agent = MagicMock()
        agent.snapshot.import_portable_session = MagicMock(return_value={})

        from src.everbot.core.session.session_data import SessionData
        session_data = SessionData(
            session_id="tg_session_alice__123",
            agent_name="alice",
            model_name="test",
            session_type="channel",
            history_messages=[_user("hi"), _assistant("hello")],
            variables={},
            created_at="2026-01-01",
            updated_at="2026-01-01",
        )

        await persistence.restore_to_agent(agent, session_data)

        call_args = agent.snapshot.import_portable_session.call_args
        history = call_args[0][0]["history_messages"]
        assert len(history) == 2
