"""Tests for SLM VersionManager."""

import tempfile
from pathlib import Path

from src.everbot.core.slm.models import (
    CurrentPointer,
    EvalReport,
    JudgeResult,
    VersionMetadata,
    VersionStatus,
)
from src.everbot.core.slm.version_manager import VersionManager, _read_frontmatter_version


SKILL_CONTENT_V1 = """\
---
name: test-skill
version: "1.0"
description: Test skill
---
This is version 1.0
"""

SKILL_CONTENT_V2 = """\
---
name: test-skill
version: "2.0"
description: Test skill updated
---
This is version 2.0
"""


class TestReadFrontmatterVersion:
    def test_with_version(self, tmp_path):
        p = tmp_path / "SKILL.md"
        p.write_text(SKILL_CONTENT_V1)
        assert _read_frontmatter_version(p) == "1.0"

    def test_without_version(self, tmp_path):
        p = tmp_path / "SKILL.md"
        p.write_text("---\nname: foo\n---\ncontent")
        assert _read_frontmatter_version(p) == "baseline"

    def test_no_frontmatter(self, tmp_path):
        p = tmp_path / "SKILL.md"
        p.write_text("just content")
        assert _read_frontmatter_version(p) == "baseline"

    def test_nonexistent(self, tmp_path):
        assert _read_frontmatter_version(tmp_path / "nope.md") == "baseline"


class TestVersionManager:
    def _make_mgr(self, tmp_path) -> VersionManager:
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        return VersionManager(skills_dir)

    def test_publish_creates_structure(self, tmp_path):
        mgr = self._make_mgr(tmp_path)
        mgr.publish("test-skill", "1.0", SKILL_CONTENT_V1)

        # SKILL.md written
        skill_md = tmp_path / "skills" / "test-skill" / "SKILL.md"
        assert skill_md.exists()
        assert "version: \"1.0\"" in skill_md.read_text()

        # Snapshot created
        snapshot = tmp_path / "skills" / "test-skill" / ".eval" / "versions" / "v1.0" / "skill.md"
        assert snapshot.exists()

        # Metadata created
        meta = mgr.get_metadata("test-skill", "1.0")
        assert meta is not None
        assert meta.status == VersionStatus.TESTING
        assert meta.verification_phase == "dense"

        # Pointer created
        ptr = mgr.get_pointer("test-skill")
        assert ptr is not None
        assert ptr.current_version == "1.0"
        assert ptr.repo_baseline is True  # first version, no prior stable

    def test_publish_second_version_promotes_stable(self, tmp_path):
        mgr = self._make_mgr(tmp_path)
        mgr.publish("test-skill", "1.0", SKILL_CONTENT_V1)
        mgr.publish("test-skill", "2.0", SKILL_CONTENT_V2)

        ptr = mgr.get_pointer("test-skill")
        assert ptr.current_version == "2.0"
        assert ptr.stable_version == "1.0"
        assert ptr.repo_baseline is False

    def test_rollback_to_stable(self, tmp_path):
        mgr = self._make_mgr(tmp_path)
        mgr.publish("test-skill", "1.0", SKILL_CONTENT_V1)
        mgr.publish("test-skill", "2.0", SKILL_CONTENT_V2)

        rolled = mgr.rollback("test-skill", reason="critical issues")
        assert rolled == "1.0"

        # SKILL.md should be v1.0 content
        skill_md = tmp_path / "skills" / "test-skill" / "SKILL.md"
        assert "version: \"1.0\"" in skill_md.read_text()

        # Pointer updated
        ptr = mgr.get_pointer("test-skill")
        assert ptr.current_version == "1.0"

        # v2.0 suspended
        meta = mgr.get_metadata("test-skill", "2.0")
        assert meta.status == VersionStatus.SUSPENDED
        assert "critical issues" in meta.suspended_reason

    def test_rollback_to_repo_baseline(self, tmp_path):
        mgr = self._make_mgr(tmp_path)
        mgr.publish("test-skill", "1.0", SKILL_CONTENT_V1)

        rolled = mgr.rollback("test-skill", reason="bad first version")
        assert rolled == "baseline"

        # SKILL.md should be deleted
        skill_md = tmp_path / "skills" / "test-skill" / "SKILL.md"
        assert not skill_md.exists()

    def test_list_versions(self, tmp_path):
        mgr = self._make_mgr(tmp_path)
        mgr.publish("test-skill", "1.0", SKILL_CONTENT_V1)
        mgr.publish("test-skill", "2.0", SKILL_CONTENT_V2)
        assert mgr.list_versions("test-skill") == ["1.0", "2.0"]
        assert mgr.list_versions("nonexistent") == []

    def test_activate(self, tmp_path):
        mgr = self._make_mgr(tmp_path)
        mgr.publish("test-skill", "1.0", SKILL_CONTENT_V1)
        mgr.activate("test-skill", "1.0")

        meta = mgr.get_metadata("test-skill", "1.0")
        assert meta.status == VersionStatus.ACTIVE
        assert meta.verification_phase == "full"

        ptr = mgr.get_pointer("test-skill")
        assert ptr.stable_version == "1.0"
        assert ptr.repo_baseline is False

    def test_save_eval_report(self, tmp_path):
        mgr = self._make_mgr(tmp_path)
        mgr.publish("test-skill", "1.0", SKILL_CONTENT_V1)

        report = EvalReport.build(
            "test-skill", "1.0",
            [JudgeResult(0, False, 0.9, "ok")],
        )
        mgr.save_eval_report("test-skill", "1.0", report)

        loaded = mgr.get_eval_report("test-skill", "1.0")
        assert loaded is not None
        assert loaded.mean_satisfaction == 0.9

        meta = mgr.get_metadata("test-skill", "1.0")
        assert meta.eval_summary["satisfaction_score"] == 0.9

    def test_check_consistency_ok(self, tmp_path):
        mgr = self._make_mgr(tmp_path)
        mgr.publish("test-skill", "1.0", SKILL_CONTENT_V1)
        assert mgr.check_consistency("test-skill") is True

    def test_check_consistency_fixes_mismatch(self, tmp_path):
        mgr = self._make_mgr(tmp_path)
        mgr.publish("test-skill", "1.0", SKILL_CONTENT_V1)

        # Manually tamper pointer
        ptr_path = tmp_path / "skills" / "test-skill" / ".eval" / "current.json"
        ptr_path.write_text('{"current_version": "9.9", "stable_version": "1.0", "repo_baseline": false}')

        assert mgr.check_consistency("test-skill") is False
        ptr = mgr.get_pointer("test-skill")
        assert ptr.current_version == "1.0"  # fixed to match SKILL.md

    def test_get_active_version(self, tmp_path):
        mgr = self._make_mgr(tmp_path)
        assert mgr.get_active_version("nonexistent") == "baseline"
        mgr.publish("test-skill", "1.0", SKILL_CONTENT_V1)
        assert mgr.get_active_version("test-skill") == "1.0"
