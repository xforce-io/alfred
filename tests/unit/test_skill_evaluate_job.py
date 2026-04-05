"""Tests for skill_evaluate job degradation behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.everbot.core.jobs.llm_errors import LLMTransientError
from src.everbot.core.jobs.skill_evaluate import _evaluate_one, run
from src.everbot.core.slm.segment_logger import SegmentLogger
from src.everbot.core.slm.version_manager import VersionManager


def _write_skill_md(base: Path, skill_name: str) -> None:
    skill_dir = base / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill_name}\nversion: baseline\n---\n",
        encoding="utf-8",
    )


def _append_segment(logs_dir: Path, skill_id: str) -> None:
    logger = SegmentLogger(logs_dir)
    from src.everbot.core.slm.models import EvaluationSegment

    logger.append(EvaluationSegment(
        skill_id=skill_id,
        skill_version="baseline",
        triggered_at="2026-04-05T08:00:00+00:00",
        context_before="user: run evaluation",
        skill_output="assistant: final output",
        context_after="user: ok",
        session_id=f"{skill_id}-session",
    ))


@pytest.mark.asyncio
async def test_evaluate_one_converts_timeout_to_transient_error(tmp_path: Path):
    skills_dir = tmp_path / "skills"
    logs_dir = tmp_path / "skill_logs"
    eval_dir = tmp_path / "skill_eval"
    _write_skill_md(skills_dir, "web-search")
    _append_segment(logs_dir, "web-search")

    seg_logger = SegmentLogger(logs_dir)
    ver_mgr = VersionManager(skills_dir, eval_base_dir=eval_dir)
    context = MagicMock()
    context.llm = MagicMock()

    async def slow_evaluate(*args, **kwargs):
        await asyncio.sleep(0.01)
        return MagicMock()

    with patch("src.everbot.core.jobs.skill_evaluate._SKILL_EVALUATION_TIMEOUT_SECONDS", 0), patch(
        "src.everbot.core.jobs.skill_evaluate.evaluate_skill",
        new=slow_evaluate,
    ):
        with pytest.raises(LLMTransientError, match="Request timed out during skill evaluation"):
            await _evaluate_one(context, seg_logger, ver_mgr, "web-search", tmp_path / "sessions")


@pytest.mark.asyncio
async def test_run_skips_unavailable_skill_and_continues(tmp_path: Path):
    agent_dir = tmp_path / "agents" / "demo"
    logs_dir = agent_dir / "skill_logs"
    eval_dir = agent_dir / "skill_eval"
    skills_dir = tmp_path / "skills"
    _write_skill_md(skills_dir, "alpha")
    _write_skill_md(skills_dir, "beta")
    _append_segment(logs_dir, "alpha")
    _append_segment(logs_dir, "beta")

    context = MagicMock()
    context.skill_logs_dir = logs_dir
    context.skill_eval_dir = eval_dir
    context.llm = MagicMock()

    async def fake_evaluate_one(_context, _seg_logger, _ver_mgr, skill_id, _sessions_dir):
        if skill_id == "alpha":
            raise LLMTransientError("Request timed out.")
        return "ok"

    fake_udm = MagicMock()
    fake_udm.skill_logs_dir = logs_dir
    fake_udm.skills_dir = skills_dir
    fake_udm.sessions_dir = tmp_path / "sessions"

    with patch("src.everbot.infra.user_data.get_user_data_manager", return_value=fake_udm), patch(
        "src.everbot.core.jobs.skill_evaluate._evaluate_one",
        new=AsyncMock(side_effect=fake_evaluate_one),
    ):
        summary = await run(context)

    assert summary == "Evaluated 1/2 skills, skipped 1 due to LLM unavailability"


@pytest.mark.asyncio
async def test_evaluate_one_falls_back_to_most_common_version(tmp_path: Path):
    """When no SLM pointer exists, use the most common version in the log."""
    skills_dir = tmp_path / "skills"
    logs_dir = tmp_path / "skill_logs"
    eval_dir = tmp_path / "skill_eval"
    _write_skill_md(skills_dir, "gray-rhino")

    seg_logger = SegmentLogger(logs_dir)
    from src.everbot.core.slm.models import EvaluationSegment

    for i in range(3):
        seg_logger.append(EvaluationSegment(
            skill_id="gray-rhino",
            skill_version="2.0.0",
            triggered_at=f"2026-04-05T0{i}:00:00+00:00",
            context_before="user: run",
            skill_output=f"output {i}",
            context_after="user: ok",
            session_id=f"sess-{i}",
        ))

    ver_mgr = VersionManager(skills_dir, eval_base_dir=eval_dir)
    # No pointer exists → should fall back to "2.0.0"
    assert ver_mgr.get_pointer("gray-rhino") is None

    context = MagicMock()
    context.llm = MagicMock()

    from src.everbot.core.slm.models import EvalReport, JudgeResult

    fake_report = EvalReport(
        skill_id="gray-rhino",
        skill_version="2.0.0",
        evaluated_at="2026-04-05T00:00:00+00:00",
        segment_count=3,
        critical_issue_count=0,
        critical_issue_rate=0.0,
        mean_satisfaction=0.8,
        results=[
            JudgeResult(segment_index=i, has_critical_issue=False, satisfaction=0.8, reason="ok")
            for i in range(3)
        ],
    )

    with patch("src.everbot.core.jobs.skill_evaluate.evaluate_skill", new=AsyncMock(return_value=fake_report)):
        result = await _evaluate_one(context, seg_logger, ver_mgr, "gray-rhino", tmp_path / "sessions")

    assert result is not None
    assert "v2.0.0" in result
