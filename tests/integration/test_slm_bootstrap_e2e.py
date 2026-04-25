"""End-to-end: unpublished SKILL.md → log → evaluate → evolve → publish.

Before this test existed, the evaluate→evolve pipeline had never been
exercised end-to-end without a prior ver_mgr.publish() call. Every unit
test pre-published its fixture skills, masking the real-world path.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.everbot.core.slm.models import (
    EvalReport,
    EvaluationSegment,
    JudgeResult,
    VersionStatus,
)
from src.everbot.core.slm.segment_logger import SegmentLogger
from src.everbot.core.slm.version_manager import VersionManager


SKILL_MD = """\
---
name: e2e-skill
version: "1.0.0"
---
You are a test skill.
"""

EVOLVED_SKILL_MD = """\
---
name: e2e-skill
version: "1.0.0-evolve-202604241200"
---
You are an improved test skill.
"""


def _unhealthy_report(skill_id: str, version: str) -> EvalReport:
    results = [
        JudgeResult(segment_index=i, has_critical_issue=True,
                    satisfaction=0.2, reason="bad")
        for i in range(3)
    ]
    return EvalReport(
        skill_id=skill_id, skill_version=version,
        evaluated_at="2026-04-24T00:00:00",
        segment_count=3, critical_issue_count=3,
        critical_issue_rate=1.0, mean_satisfaction=0.2,
        results=results,
    )


def _mk_context(tmp_path: Path):
    from types import SimpleNamespace
    llm = SimpleNamespace(complete=AsyncMock(return_value=""))
    mailbox = SimpleNamespace(deposit=AsyncMock(return_value=None))
    return SimpleNamespace(
        llm=llm,
        mailbox=mailbox,
        skill_logs_dir=tmp_path / "logs",
        skill_eval_dir=tmp_path / "eval",
    )


@pytest.mark.asyncio
async def test_unpublished_skill_evolves_end_to_end(tmp_path: Path):
    # --- Setup: drop SKILL.md into skills_dir, NO publish() ever called ---
    skills_dir = tmp_path / "skills"
    logs_dir = tmp_path / "logs"
    eval_dir = tmp_path / "eval"
    sessions_dir = tmp_path / "sessions"
    for d in (skills_dir, logs_dir, eval_dir, sessions_dir):
        d.mkdir()
    (skills_dir / "e2e-skill").mkdir()
    (skills_dir / "e2e-skill" / "SKILL.md").write_text(SKILL_MD)

    seg_logger = SegmentLogger(logs_dir)
    for i in range(3):
        seg_logger.append(EvaluationSegment(
            skill_id="e2e-skill", skill_version="1.0.0",
            triggered_at=f"2026-04-24T0{i}:00:00",
            context_before=f"query {i}", skill_output=f"bad output {i}",
            context_after="thumbs down", session_id=f"s{i}",
        ))
    vm = VersionManager(skills_dir, eval_base_dir=eval_dir)

    # --- Pre-condition: NO SLM materials exist ---
    assert vm.get_pointer("e2e-skill") is None
    assert not (eval_dir / "e2e-skill" / "current.json").exists()

    # --- Act: run _evaluate_one with mocked LLM returning unhealthy report,
    #         and mocked evolve LLM returning a valid evolved SKILL.md ---
    from src.everbot.core.jobs.skill_evaluate import _evaluate_one

    context = _mk_context(tmp_path)
    context.llm.complete = AsyncMock(return_value=EVOLVED_SKILL_MD)
    unhealthy = _unhealthy_report("e2e-skill", "1.0.0")

    # Patch get_user_data_manager so _evaluate_one's repo_skills lookup
    # resolves to our tmp skills_dir (no real ~/.alfred needed).
    # The function is imported lazily inside _evaluate_one, so we patch at
    # its canonical source location.
    fake_udm = type("FakeUDM", (), {"repo_skills_dir": None})()
    with patch(
        "src.everbot.infra.user_data.get_user_data_manager",
        return_value=fake_udm,
    ), patch(
        "src.everbot.core.jobs.skill_evaluate.evaluate_skill",
        new=AsyncMock(return_value=unhealthy),
    ):
        await _evaluate_one(context, seg_logger, vm, "e2e-skill", sessions_dir)

    # --- Assert: skill was bootstrapped, then evolved ---
    pointer = vm.get_pointer("e2e-skill")
    assert pointer is not None, "ensure_registered should have bootstrapped pointer"
    assert "evolve" in pointer.current_version, \
        f"expected evolve version after unhealthy eval, got {pointer.current_version}"

    evolve_meta = vm.get_metadata("e2e-skill", pointer.current_version)
    assert evolve_meta is not None
    assert evolve_meta.status == VersionStatus.TESTING, \
        f"new evolve version should be TESTING, got {evolve_meta.status}"

    # Original version's snapshot still exists (needed for future rollback)
    baseline_snap = eval_dir / "e2e-skill" / "versions" / "v1.0.0" / "skill.md"
    assert baseline_snap.exists(), "baseline snapshot must be preserved for rollback"
