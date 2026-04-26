"""End-to-end: symlinked skill evolves into workspace, repo untouched.

Reproduces the production layout: ~/.alfred/skills/<id> is a symlink to
<repo>/skills/<id>/. With the layered architecture, SLM's writable dir
is the agent workspace (real dir, separate from the symlink layer).
Evolved versions land in workspace; loader picks them up because layer 0
> layer 1; rollback unlinks workspace and loader returns to the symlinked
baseline. The repo file is read-only throughout.
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
You are a baseline test skill.
"""

EVOLVED_SKILL_MD = """\
---
name: e2e-skill
version: "1.0.0-evolve-202604261000"
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
        evaluated_at="2026-04-26T00:00:00",
        segment_count=3, critical_issue_count=3,
        critical_issue_rate=1.0, mean_satisfaction=0.2,
        results=results,
    )


def _mk_context(tmp_path: Path, workspace: Path):
    from types import SimpleNamespace
    llm = SimpleNamespace(complete=AsyncMock(return_value=""))
    mailbox = SimpleNamespace(deposit=AsyncMock(return_value=None))
    return SimpleNamespace(
        llm=llm,
        mailbox=mailbox,
        workspace_path=workspace,
        agent_name="e2e-agent",
        skill_logs_dir=tmp_path / "logs",
        skill_eval_dir=tmp_path / "eval",
    )


@pytest.mark.asyncio
async def test_symlinked_skill_evolves_into_workspace(tmp_path: Path):
    # ── Setup: production-like layout ────────────────────────────
    repo = tmp_path / "repo_skills"
    user_global = tmp_path / "user_skills"
    workspace = tmp_path / "agents" / "e2e-agent"
    workspace_skills = workspace / "skills"
    eval_dir = tmp_path / "eval"
    logs_dir = tmp_path / "logs"
    sessions_dir = tmp_path / "sessions"
    for d in (user_global, workspace_skills, eval_dir, logs_dir, sessions_dir):
        d.mkdir(parents=True)
    # repo has the real skill file
    (repo / "e2e-skill").mkdir(parents=True)
    repo_md = repo / "e2e-skill" / "SKILL.md"
    repo_md.write_text(SKILL_MD)
    # user-global is a symlink to the repo dir
    (user_global / "e2e-skill").symlink_to(repo / "e2e-skill")

    # Sanity: loader-equivalent priority order
    read_dirs = [workspace_skills, user_global, repo]
    vm = VersionManager(workspace_skills, eval_base_dir=eval_dir,
                        read_skill_dirs=read_dirs)

    # Prime segments so _evaluate_one will run
    seg_logger = SegmentLogger(logs_dir)
    for i in range(3):
        seg_logger.append(EvaluationSegment(
            skill_id="e2e-skill", skill_version="1.0.0",
            triggered_at=f"2026-04-26T0{i}:00:00",
            context_before=f"q{i}", skill_output=f"bad{i}",
            context_after="", session_id=f"s{i}",
        ))

    # ── Pre-conditions ───────────────────────────────────────────
    assert not (workspace_skills / "e2e-skill" / "SKILL.md").exists()
    assert (user_global / "e2e-skill" / "SKILL.md").exists()  # via symlink
    repo_md_original = repo_md.read_text()

    # ── Act: trigger evaluate → unhealthy → rollback → evolve → publish ──
    from src.everbot.core.jobs.skill_evaluate import _evaluate_one

    context = _mk_context(tmp_path, workspace)
    context.llm.complete = AsyncMock(return_value=EVOLVED_SKILL_MD)
    unhealthy = _unhealthy_report("e2e-skill", "1.0.0")

    with patch("src.everbot.core.jobs.skill_evaluate.evaluate_skill",
               new=AsyncMock(return_value=unhealthy)), \
         patch("src.everbot.infra.user_data.get_user_data_manager") as mock_udm:
        # Mock UDM so _evaluate_one's repo_skills lookup returns a real dir
        from unittest.mock import MagicMock
        udm = MagicMock()
        udm.skills_dir = user_global
        udm.repo_skills_dir = repo
        udm.skill_logs_dir = logs_dir
        udm.get_agent_writable_skills_dir.return_value = workspace_skills
        udm.get_agent_read_skill_dirs.return_value = read_dirs
        mock_udm.return_value = udm
        await _evaluate_one(context, seg_logger, vm, "e2e-skill", sessions_dir)

    # ── Assertions: writable evolved, repo untouched ─────────────
    pointer = vm.get_pointer("e2e-skill")
    assert pointer is not None
    assert "evolve" in pointer.current_version, \
        f"expected evolve version, got {pointer.current_version}"

    # The new evolved SKILL.md is in workspace
    workspace_md = workspace_skills / "e2e-skill" / "SKILL.md"
    assert workspace_md.exists(), "evolve must write to workspace skills dir"
    assert "improved" in workspace_md.read_text()

    # The repo (= what the symlink points to) is unchanged
    assert repo_md.read_text() == repo_md_original, \
        "REPO MUST NOT be modified by SLM publish"
    assert repo_md.is_file() and not repo_md.is_symlink()

    # Snapshot of original baseline preserved for future rollback
    baseline_snap = eval_dir / "e2e-skill" / "versions" / "v1.0.0" / "skill.md"
    assert baseline_snap.exists()
