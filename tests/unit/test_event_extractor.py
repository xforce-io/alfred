"""Tests for EventExtractor — LLM-driven event extraction with mocked LLM."""

import json
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from src.everbot.core.memory.event_extractor import (
    EventExtractor,
)


@pytest.fixture
def messages() -> List[Dict[str, Any]]:
    return [
        {"role": "user", "content": "我决定把 demo_agent 切到 deepseek-chat"},
        {"role": "assistant", "content": "好的，已记录。"},
    ]


def _llm_returns(payload: dict) -> AsyncMock:
    """Build an AsyncMock that yields the payload as JSON in a code block."""
    raw = "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
    return AsyncMock(return_value=raw)


@pytest.mark.asyncio
class TestEventExtractorExtract:
    async def test_empty_messages_returns_empty_result(self):
        extractor = EventExtractor(context=object())
        result = await extractor.extract([], session_id="s1")
        assert result.new_events == []

    async def test_single_event_parsed_with_all_fields(self, messages):
        payload = {
            "new_events": [{
                "content": "用户决定把 demo_agent 切到 deepseek-chat",
                "category": "decision",
                "event_at": "2026-05-01T10:30:00+00:00",
                "importance": "high",
            }]
        }
        extractor = EventExtractor(context=object())
        with patch.object(extractor, "_call_llm", _llm_returns(payload)):
            result = await extractor.extract(messages, session_id="s1")

        assert len(result.new_events) == 1
        e = result.new_events[0]
        assert e.kind == "event"
        assert e.category == "decision"
        assert e.event_at == "2026-05-01T10:30:00+00:00"
        assert e.score == 0.8  # high → 0.8
        assert e.source_session == "s1"
        assert e.activation_count == 1
        assert "deepseek-chat" in e.content
        assert e.id  # generated, non-empty

    async def test_importance_score_mapping(self, messages):
        payload = {
            "new_events": [
                {"content": "高重要事件", "category": "decision",
                 "event_at": "2026-05-01T10:00:00+00:00", "importance": "high"},
                {"content": "中重要事件", "category": "todo",
                 "event_at": "2026-05-01T10:00:00+00:00", "importance": "medium"},
                {"content": "低重要事件", "category": "incident",
                 "event_at": "2026-05-01T10:00:00+00:00", "importance": "low"},
                {"content": "未知重要性", "category": "interaction",
                 "event_at": "2026-05-01T10:00:00+00:00", "importance": "garbage"},
            ]
        }
        extractor = EventExtractor(context=object())
        with patch.object(extractor, "_call_llm", _llm_returns(payload)):
            result = await extractor.extract(messages, session_id="s1")
        scores = {e.content: e.score for e in result.new_events}
        assert scores["高重要事件"] == 0.8
        assert scores["中重要事件"] == 0.6
        assert scores["低重要事件"] == 0.4
        assert scores["未知重要性"] == 0.6  # default to medium

    async def test_event_at_fallback_to_session_time(self, messages):
        payload = {
            "new_events": [{
                "content": "没有时间戳的事件",
                "category": "decision",
                "importance": "medium",
                # no event_at
            }]
        }
        extractor = EventExtractor(context=object())
        with patch.object(extractor, "_call_llm", _llm_returns(payload)):
            result = await extractor.extract(
                messages, session_id="s1",
                session_time="2026-05-01T10:00:00+00:00",
            )
        assert len(result.new_events) == 1
        assert result.new_events[0].event_at == "2026-05-01T10:00:00+00:00"

    async def test_unknown_category_skipped(self, messages):
        payload = {
            "new_events": [
                {"content": "正常事件", "category": "decision",
                 "event_at": "2026-05-01T10:00:00+00:00", "importance": "high"},
                {"content": "类别错误", "category": "preference",
                 "event_at": "2026-05-01T10:00:00+00:00", "importance": "high"},
                {"content": "类别为空", "category": "",
                 "event_at": "2026-05-01T10:00:00+00:00", "importance": "high"},
            ]
        }
        extractor = EventExtractor(context=object())
        with patch.object(extractor, "_call_llm", _llm_returns(payload)):
            result = await extractor.extract(messages, session_id="s1")
        assert [e.content for e in result.new_events] == ["正常事件"]

    async def test_empty_content_skipped(self, messages):
        payload = {
            "new_events": [
                {"content": "", "category": "decision",
                 "event_at": "2026-05-01T10:00:00+00:00", "importance": "high"},
                {"content": "   ", "category": "decision",
                 "event_at": "2026-05-01T10:00:00+00:00", "importance": "high"},
                {"content": "有内容", "category": "decision",
                 "event_at": "2026-05-01T10:00:00+00:00", "importance": "medium"},
            ]
        }
        extractor = EventExtractor(context=object())
        with patch.object(extractor, "_call_llm", _llm_returns(payload)):
            result = await extractor.extract(messages, session_id="s1")
        assert [e.content for e in result.new_events] == ["有内容"]

    async def test_invalid_json_returns_empty_result(self, messages):
        extractor = EventExtractor(context=object())
        bad_response = AsyncMock(return_value="this is not json at all")
        with patch.object(extractor, "_call_llm", bad_response):
            result = await extractor.extract(messages, session_id="s1")
        assert result.new_events == []

    async def test_llm_exception_returns_empty_result(self, messages):
        extractor = EventExtractor(context=object())
        boom = AsyncMock(side_effect=RuntimeError("LLM down"))
        with patch.object(extractor, "_call_llm", boom):
            result = await extractor.extract(messages, session_id="s1")
        assert result.new_events == []

    async def test_no_new_events_key_returns_empty(self, messages):
        # LLM returns valid JSON but with no new_events
        payload = {"other_key": "value"}
        extractor = EventExtractor(context=object())
        with patch.object(extractor, "_call_llm", _llm_returns(payload)):
            result = await extractor.extract(messages, session_id="s1")
        assert result.new_events == []

    async def test_session_id_propagated(self, messages):
        payload = {
            "new_events": [{
                "content": "测试 session_id",
                "category": "decision",
                "event_at": "2026-05-01T10:00:00+00:00",
                "importance": "medium",
            }]
        }
        extractor = EventExtractor(context=object())
        with patch.object(extractor, "_call_llm", _llm_returns(payload)):
            result = await extractor.extract(messages, session_id="my-session-xyz")
        assert result.new_events[0].source_session == "my-session-xyz"

    async def test_todo_due_at_preserved(self, messages):
        payload = {
            "new_events": [{
                "content": "周五交付 demo",
                "category": "todo",
                "event_at": "2026-05-01T10:00:00+00:00",
                "importance": "high",
                "due_at": "2026-05-03T18:00:00+00:00",
            }]
        }
        extractor = EventExtractor(context=object())
        with patch.object(extractor, "_call_llm", _llm_returns(payload)):
            result = await extractor.extract(messages, session_id="s1")
        assert len(result.new_events) == 1
        assert result.new_events[0].due_at == "2026-05-03T18:00:00+00:00"

    async def test_due_at_ignored_on_non_todo(self, messages):
        """due_at on a decision/incident is dropped — it has no semantic meaning."""
        payload = {
            "new_events": [{
                "content": "把 demo 切到 deepseek",
                "category": "decision",
                "event_at": "2026-05-01T10:00:00+00:00",
                "importance": "high",
                "due_at": "2026-05-03T18:00:00+00:00",  # nonsense for a decision
            }]
        }
        extractor = EventExtractor(context=object())
        with patch.object(extractor, "_call_llm", _llm_returns(payload)):
            result = await extractor.extract(messages, session_id="s1")
        assert result.new_events[0].due_at is None

    async def test_each_event_gets_unique_id(self, messages):
        payload = {
            "new_events": [
                {"content": f"事件{i}", "category": "decision",
                 "event_at": "2026-05-01T10:00:00+00:00", "importance": "medium"}
                for i in range(5)
            ]
        }
        extractor = EventExtractor(context=object())
        with patch.object(extractor, "_call_llm", _llm_returns(payload)):
            result = await extractor.extract(messages, session_id="s1")
        ids = [e.id for e in result.new_events]
        assert len(set(ids)) == 5  # all unique
