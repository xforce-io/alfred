"""Unit tests for session history compressor."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.everbot.core.session.compressor import (
    COMPRESS_THRESHOLD,
    SUMMARY_TAG,
    WINDOW_SIZE,
    SessionCompressor,
    extract_existing_summary,
    inject_summary,
    is_summary_message,
    _format_messages_for_prompt,
)


# ── Pure helper tests ────────────────────────────────────────────────


class TestExtractExistingSummary:
    def test_no_summary(self):
        msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
        summary, remaining = extract_existing_summary(msgs)
        assert summary == ""
        assert remaining == msgs

    def test_with_summary(self):
        msgs = [
            {"role": "user", "content": f"{SUMMARY_TAG}\n请回顾以下之前对话的摘要，以便继续对话。"},
            {"role": "assistant", "content": "这是摘要内容"},
            {"role": "user", "content": "新消息"},
        ]
        summary, remaining = extract_existing_summary(msgs)
        assert summary == "这是摘要内容"
        assert len(remaining) == 1
        assert remaining[0]["content"] == "新消息"

    def test_empty_list(self):
        summary, remaining = extract_existing_summary([])
        assert summary == ""
        assert remaining == []

    def test_single_message(self):
        msgs = [{"role": "user", "content": "hi"}]
        summary, remaining = extract_existing_summary(msgs)
        assert summary == ""
        assert remaining == msgs


class TestInjectSummary:
    def test_inject(self):
        msgs = [{"role": "user", "content": "hello"}]
        result = inject_summary("摘要内容", msgs)
        assert len(result) == 3
        assert result[0]["role"] == "user"
        assert SUMMARY_TAG in result[0]["content"]
        assert result[1]["role"] == "assistant"
        assert result[1]["content"] == "摘要内容"
        assert result[2] == msgs[0]

    def test_inject_empty_messages(self):
        result = inject_summary("摘要", [])
        assert len(result) == 2
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"


class TestIsSummaryMessage:
    def test_positive(self):
        msg = {"role": "user", "content": f"{SUMMARY_TAG}\n回顾摘要"}
        assert is_summary_message(msg) is True

    def test_negative_role(self):
        msg = {"role": "assistant", "content": f"{SUMMARY_TAG}\nfoo"}
        assert is_summary_message(msg) is False

    def test_negative_no_tag(self):
        msg = {"role": "user", "content": "normal message"}
        assert is_summary_message(msg) is False


class TestFormatMessages:
    def test_basic(self):
        msgs = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！"},
        ]
        text = _format_messages_for_prompt(msgs)
        assert "用户: 你好" in text
        assert "助手: 你好！" in text

    def test_skips_tool_messages(self):
        msgs = [
            {"role": "user", "content": "搜索"},
            {"role": "assistant", "content": "ok", "tool_calls": [{"id": "1"}]},
            {"role": "tool", "content": "result", "tool_call_id": "1"},
            {"role": "assistant", "content": "结果"},
        ]
        text = _format_messages_for_prompt(msgs)
        assert "tool" not in text.lower()
        assert "result" not in text

    def test_truncation(self):
        msgs = [{"role": "user", "content": "a" * 5000} for _ in range(10)]
        text = _format_messages_for_prompt(msgs, max_chars=8000)
        assert "省略" in text


# ── Threshold / compression logic tests ──────────────────────────────


def _make_history(n: int) -> list:
    """Generate n user+assistant message pairs (2n messages total)."""
    msgs = []
    for i in range(n):
        msgs.append({"role": "user", "content": f"msg-{i}"})
        msgs.append({"role": "assistant", "content": f"reply-{i}"})
    return msgs


class TestMaybeCompress:
    @pytest.mark.asyncio
    async def test_below_threshold_no_compress(self):
        ctx = MagicMock()
        compressor = SessionCompressor(ctx)
        history = _make_history(20)  # 40 messages < 80
        compressed, result = await compressor.maybe_compress(history)
        assert compressed is False
        assert result is history

    @pytest.mark.asyncio
    async def test_above_threshold_triggers_compression(self):
        ctx = MagicMock()
        compressor = SessionCompressor(ctx)
        history = _make_history(50)  # 100 messages > 80

        with patch.object(compressor, "_generate_summary", new_callable=AsyncMock) as mock_gen:
            mock_gen.return_value = "这是压缩后的摘要"
            compressed, result = await compressor.maybe_compress(history)

        assert compressed is True
        # Should have summary pair + WINDOW_SIZE messages
        assert len(result) == 2 + WINDOW_SIZE
        assert SUMMARY_TAG in result[0]["content"]
        assert result[1]["content"] == "这是压缩后的摘要"
        # Verify the kept messages are the most recent ones
        assert result[-1] == history[-1]

    @pytest.mark.asyncio
    async def test_existing_summary_updated(self):
        ctx = MagicMock()
        compressor = SessionCompressor(ctx)

        # Build history with existing summary + enough messages to trigger compression
        summary_pair = [
            {"role": "user", "content": f"{SUMMARY_TAG}\n请回顾以下之前对话的摘要，以便继续对话。"},
            {"role": "assistant", "content": "旧摘要"},
        ]
        new_msgs = _make_history(50)  # 100 messages
        history = summary_pair + new_msgs  # 102 total > 80

        with patch.object(compressor, "_generate_summary", new_callable=AsyncMock) as mock_gen:
            mock_gen.return_value = "更新后的摘要"
            compressed, result = await compressor.maybe_compress(history)

        assert compressed is True
        # _generate_summary should receive the old summary text
        call_args = mock_gen.call_args
        assert call_args[0][0] == "旧摘要"  # old_summary parameter

    @pytest.mark.asyncio
    async def test_llm_failure_returns_original(self):
        ctx = MagicMock()
        compressor = SessionCompressor(ctx)
        history = _make_history(50)

        with patch.object(compressor, "_generate_summary", new_callable=AsyncMock) as mock_gen:
            mock_gen.side_effect = RuntimeError("LLM error")
            compressed, result = await compressor.maybe_compress(history)

        assert compressed is False
        assert result is history

    @pytest.mark.asyncio
    async def test_empty_summary_returns_original(self):
        ctx = MagicMock()
        compressor = SessionCompressor(ctx)
        history = _make_history(50)

        with patch.object(compressor, "_generate_summary", new_callable=AsyncMock) as mock_gen:
            mock_gen.return_value = ""
            compressed, result = await compressor.maybe_compress(history)

        assert compressed is False
        assert result is history
