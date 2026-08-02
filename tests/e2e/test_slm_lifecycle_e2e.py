"""E2E: full SLM lifecycle through the real skill_evaluate job entry.

Endpoint is not "wrote an eval report" — it is live skill replacement / iteration:

  baseline SKILL.md
    → accumulate samples
    → Skill Evaluate (unhealthy)
    → rollback stable target + evolve + publish TESTING candidate
    → live SKILL.md replaced by evolved content
    → accumulate samples on evolved version
    → Skill Evaluate (healthy + promotable)
    → activate: TESTING → ACTIVE, stable pointer advances
    → third evaluate is skipped/no_changes

Judge scoring is deterministic (mocked ``evaluate_skill``). Evolve content comes
from a deliberately faulty ``context.llm.complete`` stub that keeps the baseline
frontmatter version, proving production code owns target-version normalization.
No network.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.everbot.core.jobs.skill_evaluate import run as run_skill_evaluate
from src.everbot.core.slm.models import (
    EvalReport,
    EvaluationSegment,
    JudgeResult,
    VersionStatus,
)
from src.everbot.core.slm.segment_logger import SegmentLogger
from src.everbot.core.slm.state_normalizer import (
    RegistrationAction,
    ensure_registered,
)
from src.everbot.core.slm.version_manager import VersionManager


SKILL_ID = "lifecycle-skill"
BASE_VERSION = "1.0.0"

BASELINE_SKILL_MD = f"""\
---
name: {SKILL_ID}
version: "{BASE_VERSION}"
---
# Lifecycle Skill

You are the baseline skill body used by the lifecycle e2e.
"""

# Deliberately stale: the LLM ignores the prompt's target version (#191).
EVOLVED_SKILL_MD = f"""\
---
name: {SKILL_ID}
version: "{BASE_VERSION}"
---
# Lifecycle Skill

You are an improved skill body produced by the evolve step.
"""


async def _evolve_complete(prompt: str, system: str = "") -> str:
    """Mimic an LLM that improves content but forgets the version update."""
    assert "Update the `version` field" in prompt
    return EVOLVED_SKILL_MD


def _write_baseline_skill(skills_dir: Path) -> Path:
    skill_dir = skills_dir / SKILL_ID
    skill_dir.mkdir(parents=True)
    path = skill_dir / "SKILL.md"
    path.write_text(BASELINE_SKILL_MD, encoding="utf-8")
    return path


def _append_segments(
    logs_dir: Path,
    *,
    version: str,
    count: int,
    session_prefix: str,
    output_prefix: str = "output",
) -> None:
    logger = SegmentLogger(logs_dir)
    for i in range(count):
        logger.append(
            EvaluationSegment(
                skill_id=SKILL_ID,
                skill_version=version,
                triggered_at=f"2026-08-02T{10 + i:02d}:00:00+00:00",
                context_before=f"user: request {session_prefix}-{i}",
                skill_output=f"{output_prefix} {i}",
                context_after="user: noted",
                session_id=f"{session_prefix}-{i}",
            )
        )


def _report(
    *,
    version: str,
    segment_count: int,
    unhealthy: bool,
) -> EvalReport:
    if unhealthy:
        results = [
            JudgeResult(
                segment_index=i,
                has_critical_issue=True,
                satisfaction=0.2,
                reason="critical regression in lifecycle e2e",
            )
            for i in range(segment_count)
        ]
        critical = segment_count
        mean = 0.2
    else:
        results = [
            JudgeResult(
                segment_index=i,
                has_critical_issue=False,
                satisfaction=0.9,
                reason="accepted in lifecycle e2e",
            )
            for i in range(segment_count)
        ]
        critical = 0
        mean = 0.9
    return EvalReport(
        skill_id=SKILL_ID,
        skill_version=version,
        evaluated_at="2026-08-02T12:00:00+00:00",
        segment_count=segment_count,
        critical_issue_count=critical,
        critical_issue_rate=(critical / segment_count) if segment_count else 0.0,
        mean_satisfaction=mean,
        results=results,
    )


def _mk_context(*, logs_dir: Path, eval_dir: Path) -> SimpleNamespace:
    mailbox = SimpleNamespace(deposit=AsyncMock(return_value=None))
    llm = SimpleNamespace(complete=AsyncMock(side_effect=_evolve_complete))
    return SimpleNamespace(
        agent_name="lifecycle-agent",
        llm=llm,
        mailbox=mailbox,
        skill_logs_dir=logs_dir,
        skill_eval_dir=eval_dir,
    )


def _mk_udm(
    *,
    workspace_skills: Path,
    logs_dir: Path,
    sessions_dir: Path,
    repo_skills: Path,
) -> MagicMock:
    udm = MagicMock()
    udm.skill_logs_dir = logs_dir
    udm.skills_dir = workspace_skills
    # Keep repo_skills empty so bootstrap does NOT mark repo_baseline=True
    # (repo_baseline rollback would unlink the live SKILL.md).
    udm.repo_skills_dir = repo_skills
    udm.sessions_dir = sessions_dir
    udm.get_agent_writable_skills_dir.return_value = workspace_skills
    udm.get_agent_read_skill_dirs.return_value = [workspace_skills]
    udm.check_skill_override_drift.return_value = None
    return udm


def _mailbox_summaries(context: SimpleNamespace) -> List[str]:
    out: List[str] = []
    for call in context.mailbox.deposit.await_args_list:
        if "summary" in call.kwargs:
            out.append(str(call.kwargs["summary"]))
        elif call.args:
            out.append(str(call.args[0]))
    return out


@pytest.mark.asyncio
async def test_full_skill_lifecycle_evaluate_evolve_activate(tmp_path: Path):
    """baseline → unhealthy evolve/replace → healthy activate → no_changes."""
    workspace_skills = tmp_path / "agents" / "lifecycle-agent" / "skills"
    logs_dir = tmp_path / "skill_logs"
    eval_dir = tmp_path / "skill_eval"
    sessions_dir = tmp_path / "sessions"
    repo_skills = tmp_path / "repo_skills"
    for d in (workspace_skills, logs_dir, eval_dir, sessions_dir, repo_skills):
        d.mkdir(parents=True)

    skill_md = _write_baseline_skill(workspace_skills)
    baseline_body = skill_md.read_text(encoding="utf-8")
    assert "baseline skill body" in baseline_body

    # Phase 0 — enough baseline samples for a decisive unhealthy report.
    _append_segments(
        logs_dir,
        version=BASE_VERSION,
        count=3,
        session_prefix="base",
        output_prefix="bad-baseline",
    )

    context = _mk_context(logs_dir=logs_dir, eval_dir=eval_dir)
    udm = _mk_udm(
        workspace_skills=workspace_skills,
        logs_dir=logs_dir,
        sessions_dir=sessions_dir,
        repo_skills=repo_skills,
    )

    judge_calls: list[tuple[str, int]] = []

    async def _fake_evaluate_skill(_llm, skill_id, version, segments):
        assert skill_id == SKILL_ID
        judge_calls.append((version, len(segments)))
        # baseline → unhealthy → evolve; evolved → healthy + promotable → activate
        unhealthy = "evolve" not in version
        return _report(
            version=version,
            segment_count=len(segments),
            unhealthy=unhealthy,
        )

    # ── Phase A: unhealthy baseline → rollback + evolve + publish ──
    with patch("src.everbot.infra.user_data.get_user_data_manager", return_value=udm), patch(
        "src.everbot.core.jobs.skill_evaluate.evaluate_skill",
        new=AsyncMock(side_effect=_fake_evaluate_skill),
    ):
        first = await run_skill_evaluate(context)

    assert first.status == "completed"
    assert first.reason == "evaluated"
    assert first.detail["evaluated_count"] >= 1
    assert judge_calls and judge_calls[0][0] == BASE_VERSION

    ver_mgr = VersionManager(
        workspace_skills,
        eval_base_dir=eval_dir,
        read_skill_dirs=[workspace_skills],
    )
    pointer = ver_mgr.get_pointer(SKILL_ID)
    assert pointer is not None
    evolve_version = pointer.current_version
    assert "evolve" in evolve_version, f"expected evolve candidate, got {evolve_version}"
    assert pointer.stable_version == BASE_VERSION
    assert pointer.consecutive_evolve_count == 1

    # Live skill file must be replaced by the evolved body (the real endpoint).
    live_after_evolve = skill_md.read_text(encoding="utf-8")
    assert "improved skill body" in live_after_evolve
    assert "baseline skill body" not in live_after_evolve
    assert f'version: "{evolve_version}"' in live_after_evolve

    evolve_snap = eval_dir / SKILL_ID / "versions" / f"v{evolve_version}" / "skill.md"
    assert evolve_snap.exists()
    assert f'version: "{evolve_version}"' in evolve_snap.read_text(encoding="utf-8")

    registration = ensure_registered(ver_mgr, SKILL_ID, repo_skills_dir=None)
    assert registration.action == RegistrationAction.NOOP

    evolve_meta = ver_mgr.get_metadata(SKILL_ID, evolve_version)
    assert evolve_meta is not None
    assert evolve_meta.status == VersionStatus.TESTING

    baseline_meta = ver_mgr.get_metadata(SKILL_ID, BASE_VERSION)
    assert baseline_meta is not None
    assert baseline_meta.status == VersionStatus.ACTIVE  # stable target stays active

    baseline_report = ver_mgr.get_eval_report(SKILL_ID, BASE_VERSION)
    assert baseline_report is not None
    assert baseline_report.segment_count == 3
    assert not baseline_report.is_healthy

    baseline_snap = eval_dir / SKILL_ID / "versions" / f"v{BASE_VERSION}" / "skill.md"
    assert baseline_snap.exists()
    assert "baseline skill body" in baseline_snap.read_text(encoding="utf-8")

    summaries_a = _mailbox_summaries(context)
    assert any("不达标" in s or "改进" in s for s in summaries_a), summaries_a

    # ── Phase B: healthy samples on evolved version → activate ──
    _append_segments(
        logs_dir,
        version=evolve_version,
        count=3,
        session_prefix="evolved",
        output_prefix="good-evolved",
    )
    context.mailbox.deposit.reset_mock()

    with patch("src.everbot.infra.user_data.get_user_data_manager", return_value=udm), patch(
        "src.everbot.core.jobs.skill_evaluate.evaluate_skill",
        new=AsyncMock(side_effect=_fake_evaluate_skill),
    ):
        second = await run_skill_evaluate(context)

    assert second.status == "completed"
    assert second.reason == "evaluated"
    assert second.detail["evaluated_count"] == 3
    assert any(v == evolve_version for v, _ in judge_calls)

    pointer = ver_mgr.get_pointer(SKILL_ID)
    assert pointer.current_version == evolve_version
    assert pointer.stable_version == evolve_version, "activate must advance stable"
    assert pointer.consecutive_evolve_count == 0
    assert pointer.repo_baseline is False

    evolve_meta = ver_mgr.get_metadata(SKILL_ID, evolve_version)
    assert evolve_meta.status == VersionStatus.ACTIVE

    # Live file remains the evolved body after activation (no further rewrite).
    live_after_activate = skill_md.read_text(encoding="utf-8")
    assert "improved skill body" in live_after_activate
    assert f'version: "{evolve_version}"' in live_after_activate

    evolve_report = ver_mgr.get_eval_report(SKILL_ID, evolve_version)
    assert evolve_report is not None
    assert evolve_report.segment_count == 3
    assert evolve_report.is_healthy
    assert evolve_report.is_promotable

    summaries_b = _mailbox_summaries(context)
    assert any("验证通过" in s or "已生效" in s for s in summaries_b), summaries_b

    # ── Phase C: no new samples → skipped/no_changes, no further mutation ──
    context.mailbox.deposit.reset_mock()
    live_before_third = skill_md.read_text(encoding="utf-8")
    pointer_before = ver_mgr.get_pointer(SKILL_ID)
    assert pointer_before is not None
    stable_before = pointer_before.stable_version
    current_before = pointer_before.current_version

    with patch("src.everbot.infra.user_data.get_user_data_manager", return_value=udm), patch(
        "src.everbot.core.jobs.skill_evaluate.evaluate_skill",
        new=AsyncMock(side_effect=_fake_evaluate_skill),
    ) as judge:
        third = await run_skill_evaluate(context)

    assert third.status == "skipped"
    assert third.reason == "no_changes"
    assert third.detail["evaluated_count"] == 0
    judge.assert_not_awaited()
    context.mailbox.deposit.assert_not_awaited()

    pointer = ver_mgr.get_pointer(SKILL_ID)
    assert pointer.current_version == current_before
    assert pointer.stable_version == stable_before
    assert skill_md.read_text(encoding="utf-8") == live_before_third
