"""Tests for SLM LLM Judge."""

import json

import pytest

from src.everbot.core.slm.judge import evaluate_skill, judge_segments
from src.everbot.core.slm.models import EvaluationSegment, JudgeResult


def _make_segment(**kw):
    defaults = dict(
        skill_id="test-skill",
        skill_version="1.0",
        triggered_at="2026-03-17T10:00:00Z",
        context_before="user: fix the bug",
        skill_output="here is the fix",
        context_after="user: that worked",
        session_id="s1",
    )
    defaults.update(kw)
    return EvaluationSegment(**defaults)


class MockLLM:
    """Mock LLM that returns configurable JSON responses."""

    def __init__(self, response: str):
        self._response = response
        self.call_count = 0

    async def complete(self, prompt: str, system: str = "") -> str:
        self.call_count += 1
        return self._response


class TestJudgeSegments:
    @pytest.mark.asyncio
    async def test_batch_single_segment(self):
        llm = MockLLM('[{"has_critical_issue": false, "satisfaction": 0.9, "reason": "user happy"}]')
        results = await judge_segments(llm, [_make_segment()])
        assert len(results) == 1
        assert results[0].satisfaction == 0.9
        assert results[0].reason == "user happy"
        assert llm.call_count == 1

    @pytest.mark.asyncio
    async def test_batch_multiple_segments(self):
        response = json.dumps([
            {"has_critical_issue": False, "satisfaction": 0.8, "reason": "ok"},
            {"has_critical_issue": True, "satisfaction": 0.2, "reason": "broke it"},
            {"has_critical_issue": False, "satisfaction": 0.95, "reason": "great"},
        ])
        llm = MockLLM(response)
        segments = [_make_segment(session_id=f"s{i}") for i in range(3)]
        results = await judge_segments(llm, segments)
        assert len(results) == 3
        assert [r.segment_index for r in results] == [0, 1, 2]
        assert results[1].has_critical_issue is True
        assert results[1].satisfaction == 0.2
        assert llm.call_count == 1  # single call for all segments

    @pytest.mark.asyncio
    async def test_markdown_wrapped_json(self):
        llm = MockLLM('```json\n[{"has_critical_issue": false, "satisfaction": 0.7, "reason": "ok"}]\n```')
        results = await judge_segments(llm, [_make_segment()])
        assert results[0].satisfaction == 0.7

    @pytest.mark.asyncio
    async def test_satisfaction_clamped(self):
        llm = MockLLM('[{"has_critical_issue": false, "satisfaction": 1.5, "reason": "over"}]')
        results = await judge_segments(llm, [_make_segment()])
        assert results[0].satisfaction == 1.0

    @pytest.mark.asyncio
    async def test_single_object_fallback(self):
        """LLM returns a single object instead of array — should still work."""
        llm = MockLLM('{"has_critical_issue": false, "satisfaction": 0.8, "reason": "ok"}')
        results = await judge_segments(llm, [_make_segment()])
        assert len(results) == 1
        assert results[0].satisfaction == 0.8

    @pytest.mark.asyncio
    async def test_missing_segments_padded(self):
        """LLM returns fewer results than segments — pad with neutral scores."""
        llm = MockLLM('[{"has_critical_issue": false, "satisfaction": 0.9, "reason": "ok"}]')
        segments = [_make_segment(session_id=f"s{i}") for i in range(3)]
        results = await judge_segments(llm, segments)
        assert len(results) == 3
        assert results[0].satisfaction == 0.9
        assert results[1].satisfaction == 0.5  # padded
        assert results[2].satisfaction == 0.5  # padded

    @pytest.mark.asyncio
    async def test_llm_error_propagates(self):
        """LLM errors should propagate, not be swallowed with default scores."""
        from src.everbot.core.jobs.llm_errors import LLMTransientError

        class FailingLLM:
            async def complete(self, prompt: str, system: str = "") -> str:
                raise LLMTransientError("Connection refused")

        with pytest.raises(LLMTransientError, match="Connection refused"):
            await judge_segments(FailingLLM(), [_make_segment()])

    @pytest.mark.asyncio
    async def test_parse_error_returns_neutral_scores(self):
        """Non-LLM errors (like JSON parse failures) should return neutral scores."""
        llm = MockLLM("this is not valid json at all")
        results = await judge_segments(llm, [_make_segment()])
        assert len(results) == 1
        assert results[0].satisfaction == 0.5
        assert "error" in results[0].reason.lower()

    @pytest.mark.asyncio
    async def test_empty_segments(self):
        llm = MockLLM("[]")
        results = await judge_segments(llm, [])
        assert results == []
        assert llm.call_count == 0


class TestEvaluateSkill:
    @pytest.mark.asyncio
    async def test_produces_report(self):
        response = json.dumps([
            {"has_critical_issue": False, "satisfaction": 0.85, "reason": "good"},
            {"has_critical_issue": False, "satisfaction": 0.85, "reason": "good"},
            {"has_critical_issue": False, "satisfaction": 0.85, "reason": "good"},
        ])
        llm = MockLLM(response)
        segments = [_make_segment(session_id=f"s{i}") for i in range(3)]
        report = await evaluate_skill(llm, "test-skill", "1.0", segments)
        assert report.skill_id == "test-skill"
        assert report.segment_count == 3
        assert report.critical_issue_rate == 0.0
        assert report.mean_satisfaction == 0.85
        assert llm.call_count == 1
