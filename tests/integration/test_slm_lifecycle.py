"""E2E integration test for SLM: full lifecycle from segment logging to rollback.

Flow tested:
1. Publish v1.0 skill
2. Log inline evaluation segments
3. LLM Judge scores → evaluation report
4. Publish v2.0
5. Log segments with critical issues for v2.0
6. Evaluate v2.0 → report shows problems
7. Rollback to v1.0
8. Verify consistency after rollback
9. Rollback to repo baseline (delete override)
"""

import pytest
from pathlib import Path

from src.everbot.core.slm.models import (
    EvalReport,
    EvaluationSegment,
    JudgeResult,
    VersionStatus,
)
from src.everbot.core.slm.segment_logger import SegmentLogger
from src.everbot.core.slm.judge import evaluate_skill
from src.everbot.core.slm.version_manager import VersionManager


SKILL_V1 = """\
---
name: coding-master
version: "1.0"
description: Code review and generation
---
You are a coding assistant. Help users write clean code.
"""

SKILL_V2 = """\
---
name: coding-master
version: "2.0"
description: Code review and generation (improved)
---
You are an advanced coding assistant with deeper analysis.
"""


class MockLLM:
    """Configurable mock: returns different scores based on call order."""

    def __init__(self, responses: list[str]):
        self._responses = responses
        self._index = 0

    async def complete(self, prompt: str, system: str = "") -> str:
        resp = self._responses[self._index % len(self._responses)]
        self._index += 1
        return resp


def _good_judge_response(satisfaction: float = 0.9) -> str:
    return f'{{"has_critical_issue": false, "satisfaction": {satisfaction}, "reason": "user accepted smoothly"}}'


def _bad_judge_response() -> str:
    return '{"has_critical_issue": true, "satisfaction": 0.1, "reason": "skill broke the code, user had to redo"}'


def _log_segment(
    seg_logger: SegmentLogger,
    *,
    skill_id: str,
    version: str,
    session_id: str,
    triggered_at: str,
    context_before: str = "user: help",
    skill_output: str = "assistant: done",
    context_after: str = "user: ok",
):
    """Append an inline EvaluationSegment to the logger."""
    segment = EvaluationSegment(
        skill_id=skill_id,
        skill_version=version,
        triggered_at=triggered_at,
        context_before=context_before,
        skill_output=skill_output,
        context_after=context_after,
        session_id=session_id,
    )
    seg_logger.append(segment)


class TestSLMLifecycle:
    """Full lifecycle: publish → log → evaluate → upgrade → detect problems → rollback."""

    @pytest.mark.asyncio
    async def test_full_lifecycle(self, tmp_path: Path):
        skills_dir = tmp_path / "skills"
        logs_dir = tmp_path / "skill_logs"
        skills_dir.mkdir()
        logs_dir.mkdir()

        ver_mgr = VersionManager(skills_dir)
        seg_logger = SegmentLogger(logs_dir)

        # ── Step 1: Publish v1.0 ──
        ver_mgr.publish("coding-master", "1.0", SKILL_V1)

        skill_md = skills_dir / "coding-master" / "SKILL.md"
        assert skill_md.exists()
        assert 'version: "1.0"' in skill_md.read_text()
        assert ver_mgr.get_active_version("coding-master") == "1.0"

        ptr = ver_mgr.get_pointer("coding-master")
        assert ptr.current_version == "1.0"
        assert ptr.repo_baseline is True

        # ── Step 2: Log good segments for v1.0 ──
        for i in range(5):
            _log_segment(
                seg_logger,
                skill_id="coding-master", version="1.0",
                session_id=f"sess-{i}",
                triggered_at=f"2026-03-17T1{i}:00:00Z",
                context_before=f"user: help me with task {i}",
                skill_output=f"here is the solution for task {i}",
                context_after="user: that works, thanks",
            )

        assert seg_logger.count("coding-master") == 5

        # ── Step 3: Evaluate v1.0 ──
        v1_segments = seg_logger.load_by_version("coding-master", "1.0")
        assert len(v1_segments) == 5

        llm_v1 = MockLLM([_good_judge_response(0.85)])
        report_v1 = await evaluate_skill(llm_v1, "coding-master", "1.0", v1_segments)

        assert report_v1.segment_count == 5
        assert report_v1.critical_issue_rate == 0.0
        assert report_v1.mean_satisfaction == 0.85
        assert report_v1.is_healthy

        ver_mgr.save_eval_report("coding-master", "1.0", report_v1)
        ver_mgr.activate("coding-master", "1.0")

        meta_v1 = ver_mgr.get_metadata("coding-master", "1.0")
        assert meta_v1.status == VersionStatus.ACTIVE
        assert meta_v1.eval_summary["satisfaction_score"] == 0.85

        # ── Step 4: Publish v2.0 ──
        ver_mgr.publish("coding-master", "2.0", SKILL_V2)

        assert ver_mgr.get_active_version("coding-master") == "2.0"
        ptr = ver_mgr.get_pointer("coding-master")
        assert ptr.current_version == "2.0"
        assert ptr.stable_version == "1.0"
        assert ptr.repo_baseline is False

        # ── Step 5: Log problematic segments for v2.0 ──
        for i in range(5):
            _log_segment(
                seg_logger,
                skill_id="coding-master", version="2.0",
                session_id=f"sess-v2-{i}",
                triggered_at=f"2026-03-18T1{i}:00:00Z",
                context_before=f"user: review this code block {i}",
                skill_output=f"analysis of block {i} with advanced reasoning",
                context_after="user: that broke everything, redo this" if i < 2 else "user: ok",
            )

        # ── Step 6: Evaluate v2.0 — 2/5 are critical ──
        v2_segments = seg_logger.load_by_version("coding-master", "2.0")
        assert len(v2_segments) == 5

        llm_v2 = MockLLM([
            _bad_judge_response(),
            _bad_judge_response(),
            _good_judge_response(0.7),
            _good_judge_response(0.8),
            _good_judge_response(0.75),
        ])
        report_v2 = await evaluate_skill(llm_v2, "coding-master", "2.0", v2_segments)

        assert report_v2.segment_count == 5
        assert report_v2.critical_issue_count == 2
        assert abs(report_v2.critical_issue_rate - 0.4) < 0.01
        assert not report_v2.is_healthy

        ver_mgr.save_eval_report("coding-master", "2.0", report_v2)

        # ── Step 7: Rollback to v1.0 ──
        rolled_to = ver_mgr.rollback("coding-master", reason="40% critical issue rate")
        assert rolled_to == "1.0"

        assert 'version: "1.0"' in skill_md.read_text()
        assert ver_mgr.get_active_version("coding-master") == "1.0"

        meta_v2 = ver_mgr.get_metadata("coding-master", "2.0")
        assert meta_v2.status == VersionStatus.SUSPENDED
        assert "40% critical" in meta_v2.suspended_reason

        ptr = ver_mgr.get_pointer("coding-master")
        assert ptr.current_version == "1.0"

        # ── Step 8: Consistency check passes ──
        assert ver_mgr.check_consistency("coding-master") is True

        # ── Step 9: Rollback to repo baseline (delete override) ──
        ptr = ver_mgr.get_pointer("coding-master")
        ptr.repo_baseline = True
        ptr.stable_version = ""
        (skills_dir / "coding-master" / ".eval" / "current.json").write_text(
            ptr.to_json(), encoding="utf-8"
        )

        rolled_to = ver_mgr.rollback("coding-master", reason="revert to repo original")
        assert rolled_to == "baseline"
        assert not skill_md.exists()

        # ── Step 10: Version history preserved ──
        versions = ver_mgr.list_versions("coding-master")
        assert "1.0" in versions
        assert "2.0" in versions

        loaded_report = ver_mgr.get_eval_report("coding-master", "1.0")
        assert loaded_report is not None
        assert loaded_report.mean_satisfaction == 0.85

        # Segments still accessible
        all_segments = seg_logger.load("coding-master")
        assert len(all_segments) == 10  # 5 v1.0 + 5 v2.0


class TestSLMSuccessfulUpgrade:
    """Happy path: v1.0 → v2.0 upgrade succeeds, v2.0 becomes new stable."""

    @pytest.mark.asyncio
    async def test_successful_upgrade(self, tmp_path: Path):
        skills_dir = tmp_path / "skills"
        logs_dir = tmp_path / "skill_logs"
        skills_dir.mkdir()
        logs_dir.mkdir()

        ver_mgr = VersionManager(skills_dir)
        seg_logger = SegmentLogger(logs_dir)
        skill_md = skills_dir / "coding-master" / "SKILL.md"

        # ── Step 1: Publish and activate v1.0 ──
        ver_mgr.publish("coding-master", "1.0", SKILL_V1)
        for i in range(10):
            _log_segment(
                seg_logger,
                skill_id="coding-master", version="1.0",
                session_id=f"v1-{i}",
                triggered_at=f"2026-03-17T{10+i}:00:00Z",
                context_before=f"user: task {i}",
                skill_output=f"solution {i}",
                context_after="user: ok",
            )

        v1_segments = seg_logger.load_by_version("coding-master", "1.0")
        llm_v1 = MockLLM([_good_judge_response(0.75)])
        report_v1 = await evaluate_skill(llm_v1, "coding-master", "1.0", v1_segments)
        ver_mgr.save_eval_report("coding-master", "1.0", report_v1)
        ver_mgr.activate("coding-master", "1.0")

        assert report_v1.mean_satisfaction == 0.75
        assert ver_mgr.get_metadata("coding-master", "1.0").status == VersionStatus.ACTIVE

        # ── Step 2: Publish v2.0 (enters testing) ──
        ver_mgr.publish("coding-master", "2.0", SKILL_V2)

        ptr = ver_mgr.get_pointer("coding-master")
        assert ptr.current_version == "2.0"
        assert ptr.stable_version == "1.0"
        assert ver_mgr.get_metadata("coding-master", "2.0").status == VersionStatus.TESTING

        # ── Step 3: Log segments for v2.0 — all good, higher satisfaction ──
        for i in range(20):
            _log_segment(
                seg_logger,
                skill_id="coding-master", version="2.0",
                session_id=f"v2-{i}",
                triggered_at=f"2026-03-18T{10+i}:00:00Z",
                context_before=f"user: complex task {i}",
                skill_output=f"deep analysis and solution for {i}",
                context_after="user: excellent, exactly what I needed",
            )

        # ── Step 4: Evaluate v2.0 — better than v1.0 ──
        v2_segments = seg_logger.load_by_version("coding-master", "2.0")
        assert len(v2_segments) == 20

        llm_v2 = MockLLM([_good_judge_response(0.92)])
        report_v2 = await evaluate_skill(llm_v2, "coding-master", "2.0", v2_segments)

        assert report_v2.segment_count == 20
        assert report_v2.critical_issue_rate == 0.0
        assert report_v2.mean_satisfaction == 0.92
        assert report_v2.is_healthy

        assert report_v2.mean_satisfaction > report_v1.mean_satisfaction

        ver_mgr.save_eval_report("coding-master", "2.0", report_v2)

        # ── Step 5: Activate v2.0 — becomes new stable ──
        ver_mgr.activate("coding-master", "2.0")

        meta_v2 = ver_mgr.get_metadata("coding-master", "2.0")
        assert meta_v2.status == VersionStatus.ACTIVE
        assert meta_v2.verification_phase == "full"
        assert meta_v2.eval_summary["satisfaction_score"] == 0.92

        ptr = ver_mgr.get_pointer("coding-master")
        assert ptr.current_version == "2.0"
        assert ptr.stable_version == "2.0"
        assert ptr.repo_baseline is False

        # ── Step 6: SKILL.md still points to v2.0 ──
        assert 'version: "2.0"' in skill_md.read_text()

        # ── Step 7: Both versions' reports are preserved and comparable ──
        r1 = ver_mgr.get_eval_report("coding-master", "1.0")
        r2 = ver_mgr.get_eval_report("coding-master", "2.0")
        assert r1.mean_satisfaction == 0.75
        assert r2.mean_satisfaction == 0.92
        assert r2.segment_count > r1.segment_count

        assert ver_mgr.get_metadata("coding-master", "1.0").status == VersionStatus.ACTIVE


class TestSLMHumanOverrideFlow:
    """Test that human-overridden critical issues are excluded from auto-rollback denominator."""

    @pytest.mark.asyncio
    async def test_human_override_exclusion(self, tmp_path: Path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        ver_mgr = VersionManager(skills_dir)
        ver_mgr.publish("test-skill", "1.0", "---\nname: test\nversion: '1.0'\n---\ncontent")

        results = [
            JudgeResult(0, True, 0.2, "looks bad", human_override="accepted"),
            JudgeResult(1, False, 0.9, "fine"),
            JudgeResult(2, False, 0.85, "ok"),
        ]
        report = EvalReport.build("test-skill", "1.0", results)

        assert report.critical_issue_count == 0
        assert report.critical_issue_rate == 0.0
        assert abs(report.mean_satisfaction - 0.65) < 0.01
        assert report.is_healthy

        ver_mgr.save_eval_report("test-skill", "1.0", report)
        loaded = ver_mgr.get_eval_report("test-skill", "1.0")
        assert loaded.critical_issue_rate == 0.0
