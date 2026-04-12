"""Tests for skill_evaluate job degradation behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.everbot.core.jobs.llm_errors import LLMTransientError
from src.everbot.core.jobs.skill_evaluate import _evaluate_one, run
from src.everbot.core.slm.models import (
    CurrentPointer,
    EvalReport,
    EvaluationSegment,
    JudgeResult,
    VersionMetadata,
    VersionStatus,
)
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


def _make_healthy_report(skill_id: str, version: str, n: int = 3) -> EvalReport:
    return EvalReport(
        skill_id=skill_id,
        skill_version=version,
        evaluated_at="2026-04-12T00:00:00+00:00",
        segment_count=n,
        critical_issue_count=0,
        critical_issue_rate=0.0,
        mean_satisfaction=0.85,
        results=[
            JudgeResult(segment_index=i, has_critical_issue=False, satisfaction=0.85, reason="ok")
            for i in range(n)
        ],
    )


def _make_unhealthy_report(skill_id: str, version: str, n: int = 4) -> EvalReport:
    results = [
        JudgeResult(segment_index=i, has_critical_issue=(i % 2 == 0), satisfaction=0.3, reason="bad output")
        for i in range(n)
    ]
    critical = sum(1 for r in results if r.has_critical_issue)
    return EvalReport(
        skill_id=skill_id,
        skill_version=version,
        evaluated_at="2026-04-12T00:00:00+00:00",
        segment_count=n,
        critical_issue_count=critical,
        critical_issue_rate=critical / n,
        mean_satisfaction=0.3,
        results=results,
    )


def _setup_skill_with_version(
    skills_dir, logs_dir, eval_dir, skill_id, version, status=VersionStatus.TESTING,
    *, with_stable_base: bool = False,
):
    """Create a skill with a specific version and status.

    If with_stable_base=True, publishes a "0.1" base version first so that
    rollback has a stable version to fall back to (instead of repo baseline).
    """
    ver_mgr = VersionManager(skills_dir, eval_base_dir=eval_dir)
    if with_stable_base:
        base_content = f"---\nname: {skill_id}\nversion: \"0.1\"\n---\nBase content"
        ver_mgr.publish(skill_id, "0.1", base_content)
        # Activate the base so it becomes stable when the next version is published
        ver_mgr.activate(skill_id, "0.1")
    content = f"---\nname: {skill_id}\nversion: \"{version}\"\n---\nSkill content"
    ver_mgr.publish(skill_id, version, content)
    # Set the desired status
    meta = ver_mgr.get_metadata(skill_id, version)
    meta.status = status
    ver_dir = eval_dir / skill_id / "versions" / f"v{version}"
    (ver_dir / "metadata.json").write_text(meta.to_json(), encoding="utf-8")
    # Write segments
    seg_logger = SegmentLogger(logs_dir)
    for i in range(3):
        seg_logger.append(EvaluationSegment(
            skill_id=skill_id,
            skill_version=version,
            triggered_at=f"2026-04-12T0{i}:00:00+00:00",
            context_before="user: do something",
            skill_output=f"output {i}",
            context_after="user: ok",
            session_id=f"sess-{i}",
        ))
    return ver_mgr, seg_logger


@pytest.mark.asyncio
async def test_testing_healthy_activates(tmp_path: Path):
    """Testing version + healthy report -> activate + evolve_count cleared."""
    skills_dir = tmp_path / "skills"
    logs_dir = tmp_path / "skill_logs"
    eval_dir = tmp_path / "skill_eval"
    skills_dir.mkdir()

    ver_mgr, seg_logger = _setup_skill_with_version(
        skills_dir, logs_dir, eval_dir, "my-skill", "1.0-evolve-202604", VersionStatus.TESTING,
    )
    pointer = ver_mgr.get_pointer("my-skill")
    pointer.consecutive_evolve_count = 1
    ver_mgr._current_json("my-skill").write_text(pointer.to_json(), encoding="utf-8")

    context = MagicMock()
    context.llm = MagicMock()
    context.mailbox = AsyncMock()
    context.mailbox.deposit = AsyncMock(return_value=True)

    healthy_report = _make_healthy_report("my-skill", "1.0-evolve-202604")

    with patch("src.everbot.core.jobs.skill_evaluate.evaluate_skill", new=AsyncMock(return_value=healthy_report)):
        result = await _evaluate_one(context, seg_logger, ver_mgr, "my-skill", tmp_path / "sessions")

    meta = ver_mgr.get_metadata("my-skill", "1.0-evolve-202604")
    assert meta.status == VersionStatus.ACTIVE
    pointer = ver_mgr.get_pointer("my-skill")
    assert pointer.consecutive_evolve_count == 0
    context.mailbox.deposit.assert_awaited_once()


@pytest.mark.asyncio
async def test_unhealthy_triggers_rollback_and_evolve(tmp_path: Path):
    """Unhealthy report -> rollback to stable + LLM evolve + publish testing."""
    skills_dir = tmp_path / "skills"
    logs_dir = tmp_path / "skill_logs"
    eval_dir = tmp_path / "skill_eval"
    skills_dir.mkdir()

    ver_mgr, seg_logger = _setup_skill_with_version(
        skills_dir, logs_dir, eval_dir, "bad-skill", "1.0", VersionStatus.ACTIVE,
        with_stable_base=True,
    )

    context = MagicMock()
    context.llm = AsyncMock()
    context.llm.complete = AsyncMock(return_value=(
        "---\nname: bad-skill\nversion: \"1.0-evolve-fix\"\n---\nImproved content"
    ))
    context.mailbox = AsyncMock()
    context.mailbox.deposit = AsyncMock(return_value=True)

    unhealthy_report = _make_unhealthy_report("bad-skill", "1.0")

    with patch("src.everbot.core.jobs.skill_evaluate.evaluate_skill", new=AsyncMock(return_value=unhealthy_report)):
        result = await _evaluate_one(context, seg_logger, ver_mgr, "bad-skill", tmp_path / "sessions")

    pointer = ver_mgr.get_pointer("bad-skill")
    assert "evolve" in pointer.current_version
    meta = ver_mgr.get_metadata("bad-skill", pointer.current_version)
    assert meta.status == VersionStatus.TESTING
    assert pointer.consecutive_evolve_count == 1
    context.mailbox.deposit.assert_awaited()


@pytest.mark.asyncio
async def test_evolve_count_exceeded_suspends(tmp_path: Path):
    """Consecutive evolve > MAX -> suspend skill."""
    skills_dir = tmp_path / "skills"
    logs_dir = tmp_path / "skill_logs"
    eval_dir = tmp_path / "skill_eval"
    skills_dir.mkdir()

    ver_mgr, seg_logger = _setup_skill_with_version(
        skills_dir, logs_dir, eval_dir, "stuck-skill", "1.0", VersionStatus.ACTIVE,
        with_stable_base=True,
    )
    pointer = ver_mgr.get_pointer("stuck-skill")
    pointer.consecutive_evolve_count = 3
    ver_mgr._current_json("stuck-skill").write_text(pointer.to_json(), encoding="utf-8")

    context = MagicMock()
    context.llm = MagicMock()
    context.mailbox = AsyncMock()
    context.mailbox.deposit = AsyncMock(return_value=True)

    unhealthy_report = _make_unhealthy_report("stuck-skill", "1.0")

    with patch("src.everbot.core.jobs.skill_evaluate.evaluate_skill", new=AsyncMock(return_value=unhealthy_report)):
        result = await _evaluate_one(context, seg_logger, ver_mgr, "stuck-skill", tmp_path / "sessions")

    meta = ver_mgr.get_metadata("stuck-skill", "1.0")
    assert meta.status == VersionStatus.SUSPENDED


@pytest.mark.asyncio
async def test_evolve_llm_failure_still_rolls_back(tmp_path: Path):
    """If LLM evolve fails, rollback still happens but no new version published."""
    skills_dir = tmp_path / "skills"
    logs_dir = tmp_path / "skill_logs"
    eval_dir = tmp_path / "skill_eval"
    skills_dir.mkdir()

    ver_mgr, seg_logger = _setup_skill_with_version(
        skills_dir, logs_dir, eval_dir, "fail-skill", "2.0", VersionStatus.ACTIVE,
        with_stable_base=True,
    )

    context = MagicMock()
    context.llm = AsyncMock()
    context.llm.complete = AsyncMock(return_value="invalid garbage no frontmatter")
    context.mailbox = AsyncMock()
    context.mailbox.deposit = AsyncMock(return_value=True)

    unhealthy_report = _make_unhealthy_report("fail-skill", "2.0")

    with patch("src.everbot.core.jobs.skill_evaluate.evaluate_skill", new=AsyncMock(return_value=unhealthy_report)):
        result = await _evaluate_one(context, seg_logger, ver_mgr, "fail-skill", tmp_path / "sessions")

    pointer = ver_mgr.get_pointer("fail-skill")
    assert pointer.current_version != "2.0"
    assert "evolve" not in pointer.current_version
