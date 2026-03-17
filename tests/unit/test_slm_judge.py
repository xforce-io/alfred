"""Tests for SLM LLM Judge."""

import pytest

from src.everbot.core.slm.judge import evaluate_skill, judge_segment, judge_segments
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


class TestJudgeSegment:
    @pytest.mark.asyncio
    async def test_basic_scoring(self):
        llm = MockLLM('{"has_critical_issue": false, "satisfaction": 0.9, "reason": "user happy"}')
        result = await judge_segment(llm, _make_segment())
        assert result.has_critical_issue is False
        assert result.satisfaction == 0.9
        assert result.reason == "user happy"

    @pytest.mark.asyncio
    async def test_critical_issue(self):
        llm = MockLLM('{"has_critical_issue": true, "satisfaction": 0.1, "reason": "broke code"}')
        result = await judge_segment(llm, _make_segment())
        assert result.has_critical_issue is True
        assert result.satisfaction == 0.1

    @pytest.mark.asyncio
    async def test_markdown_wrapped_json(self):
        llm = MockLLM('```json\n{"has_critical_issue": false, "satisfaction": 0.7, "reason": "ok"}\n```')
        result = await judge_segment(llm, _make_segment())
        assert result.satisfaction == 0.7

    @pytest.mark.asyncio
    async def test_satisfaction_clamped(self):
        llm = MockLLM('{"has_critical_issue": false, "satisfaction": 1.5, "reason": "over"}')
        result = await judge_segment(llm, _make_segment())
        assert result.satisfaction == 1.0


class TestJudgeSegments:
    @pytest.mark.asyncio
    async def test_indexes_set_correctly(self):
        llm = MockLLM('{"has_critical_issue": false, "satisfaction": 0.8, "reason": "ok"}')
        segments = [_make_segment(session_id=f"s{i}") for i in range(3)]
        results = await judge_segments(llm, segments)
        assert len(results) == 3
        assert [r.segment_index for r in results] == [0, 1, 2]
        assert llm.call_count == 3

    @pytest.mark.asyncio
    async def test_error_handling(self):
        """Failed LLM calls should produce neutral results, not crash."""

        class FailingLLM:
            async def complete(self, prompt: str, system: str = "") -> str:
                raise RuntimeError("LLM unavailable")

        results = await judge_segments(FailingLLM(), [_make_segment()])
        assert len(results) == 1
        assert results[0].satisfaction == 0.5
        assert "error" in results[0].reason.lower()


class TestEvaluateSkill:
    @pytest.mark.asyncio
    async def test_produces_report(self):
        llm = MockLLM('{"has_critical_issue": false, "satisfaction": 0.85, "reason": "good"}')
        segments = [_make_segment(session_id=f"s{i}") for i in range(3)]
        report = await evaluate_skill(llm, "test-skill", "1.0", segments)
        assert report.skill_id == "test-skill"
        assert report.segment_count == 3
        assert report.critical_issue_rate == 0.0
        assert report.mean_satisfaction == 0.85
