"""Tests for SLM data models."""

from src.everbot.core.slm.models import (
    CurrentPointer,
    EvalReport,
    EvaluationSegment,
    JudgeResult,
    VersionMetadata,
    VersionStatus,
)




class TestEvaluationSegment:
    def test_roundtrip(self):
        seg = EvaluationSegment(
            skill_id="example-skill",
            skill_version="1.0",
            triggered_at="2026-03-17T10:00:00Z",
            context_before="user: help me fix this bug",
            skill_output="here is the fix...",
            context_after="user: that worked, thanks",
            session_id="sess-123",
        )
        json_str = seg.to_json()
        restored = EvaluationSegment.from_json(json_str)
        assert restored.skill_id == "example-skill"
        assert restored.skill_version == "1.0"
        assert restored.session_id == "sess-123"

    def test_from_dict_defaults(self):
        seg = EvaluationSegment.from_dict({})
        assert seg.skill_id == ""
        assert seg.skill_version == "baseline"


class TestJudgeResult:
    def test_to_dict_omits_none_override(self):
        result = JudgeResult(
            segment_index=0,
            has_critical_issue=False,
            satisfaction=0.8,
            reason="looks good",
        )
        d = result.to_dict()
        assert "human_override" not in d

    def test_to_dict_includes_override(self):
        result = JudgeResult(
            segment_index=0,
            has_critical_issue=True,
            satisfaction=0.2,
            reason="error",
            human_override="accepted",
        )
        d = result.to_dict()
        assert d["human_override"] == "accepted"


class TestEvalReport:
    def test_build_basic(self):
        results = [
            JudgeResult(0, False, 0.9, "good"),
            JudgeResult(1, False, 0.8, "ok"),
            JudgeResult(2, True, 0.1, "broken"),
        ]
        report = EvalReport.build("test-skill", "1.0", results)
        assert report.segment_count == 3
        assert report.critical_issue_count == 1
        assert abs(report.critical_issue_rate - 1 / 3) < 0.01
        assert abs(report.mean_satisfaction - 0.6) < 0.01

    def test_build_excludes_human_overridden_from_critical_rate(self):
        results = [
            JudgeResult(0, True, 0.2, "error", human_override="accepted"),
            JudgeResult(1, False, 0.9, "good"),
        ]
        report = EvalReport.build("test-skill", "1.0", results)
        # human-overridden critical issue excluded from auto denominator
        assert report.critical_issue_count == 0
        assert report.critical_issue_rate == 0.0
        # but satisfaction includes all
        assert report.mean_satisfaction == 0.55

    def test_roundtrip(self):
        results = [JudgeResult(0, False, 0.85, "fine")]
        report = EvalReport.build("s", "1.0", results)
        json_str = report.to_json()
        restored = EvalReport.from_json(json_str)
        assert restored.skill_id == "s"
        assert len(restored.results) == 1
        assert restored.results[0].satisfaction == 0.85

    def test_is_healthy(self):
        healthy = EvalReport.build("s", "1.0", [JudgeResult(0, False, 0.8, "ok")])
        assert healthy.is_healthy

        unhealthy = EvalReport.build("s", "1.0", [JudgeResult(0, True, 0.1, "bad")])
        assert not unhealthy.is_healthy


class TestVersionMetadata:
    def test_roundtrip(self):
        meta = VersionMetadata(
            version="2.0",
            created_at="2026-03-17T00:00:00Z",
            status=VersionStatus.ACTIVE,
            eval_summary={"critical_issue_rate": 0.02, "satisfaction_score": 0.81},
        )
        json_str = meta.to_json()
        restored = VersionMetadata.from_json(json_str)
        assert restored.version == "2.0"
        assert restored.status == VersionStatus.ACTIVE
        assert restored.eval_summary["satisfaction_score"] == 0.81

    def test_minimal(self):
        meta = VersionMetadata(version="1.0", created_at="now")
        d = meta.to_dict()
        assert "eval_summary" not in d
        assert "suspended_reason" not in d

    def test_legacy_metadata_with_verification_phase_is_loadable(self):
        """Old metadata.json files in production may carry the now-removed
        `verification_phase` field. Loading should ignore it gracefully,
        not error."""
        legacy_json = (
            '{"version": "1.0", "created_at": "now", "status": "active", '
            '"verification_phase": "full"}'
        )
        meta = VersionMetadata.from_json(legacy_json)
        assert meta.version == "1.0"
        assert meta.status == VersionStatus.ACTIVE


class TestCurrentPointer:
    def test_roundtrip(self):
        ptr = CurrentPointer("2.0", "1.1", repo_baseline=False)
        restored = CurrentPointer.from_json(ptr.to_json())
        assert restored.current_version == "2.0"
        assert restored.stable_version == "1.1"
        assert restored.repo_baseline is False

    def test_defaults(self):
        ptr = CurrentPointer.from_dict({})
        assert ptr.repo_baseline is True


class TestCurrentPointerEvolveCount:
    def test_default_evolve_count(self):
        pointer = CurrentPointer(current_version="1.0", stable_version="0.9")
        assert pointer.consecutive_evolve_count == 0

    def test_roundtrip_with_evolve_count(self):
        pointer = CurrentPointer(
            current_version="1.0",
            stable_version="0.9",
            consecutive_evolve_count=2,
        )
        json_str = pointer.to_json()
        restored = CurrentPointer.from_json(json_str)
        assert restored.consecutive_evolve_count == 2

    def test_backward_compat_missing_field(self):
        """Old current.json without consecutive_evolve_count loads as 0."""
        data = {"current_version": "1.0", "stable_version": "0.9", "repo_baseline": False}
        pointer = CurrentPointer.from_dict(data)
        assert pointer.consecutive_evolve_count == 0


# ── is_promotable threshold tests ──────────────────────────────────

class TestIsPromotable:
    def _report(self, *, segments: int, sat: float, crit_rate: float):
        from src.everbot.core.slm.models import EvalReport
        return EvalReport(
            skill_id="x", skill_version="1.0", evaluated_at="2026-04-26T00:00:00",
            segment_count=segments,
            critical_issue_count=int(round(segments * crit_rate)),
            critical_issue_rate=crit_rate,
            mean_satisfaction=sat,
            results=[],
        )

    def test_promotable_meets_all_three_guards(self):
        r = self._report(segments=3, sat=0.7, crit_rate=0.0)
        assert r.is_promotable is True

    def test_segments_below_minimum_blocks(self):
        # Even with perfect scores, < 3 segments not enough.
        r = self._report(segments=2, sat=1.0, crit_rate=0.0)
        assert r.is_promotable is False
        assert r.is_healthy is True  # but still healthy enough not to rollback

    def test_one_critical_blocks_promotion(self):
        # 1/3 critical → rate 0.333. Even though sat is high.
        r = self._report(segments=3, sat=0.9, crit_rate=0.333)
        assert r.is_promotable is False
        assert r.is_healthy is False  # would also trigger rollback

    def test_low_satisfaction_blocks_promotion(self):
        # 0.6 sat = healthy, but < 0.7 promotion bar.
        r = self._report(segments=10, sat=0.6, crit_rate=0.0)
        assert r.is_promotable is False
        assert r.is_healthy is True  # OK for not-rolling-back

    def test_promotable_implies_healthy(self):
        # Logical sanity: any promotable report should also be healthy.
        for segs, sat in [(3, 0.7), (5, 0.85), (10, 1.0)]:
            r = self._report(segments=segs, sat=sat, crit_rate=0.0)
            assert r.is_promotable is True
            assert r.is_healthy is True
