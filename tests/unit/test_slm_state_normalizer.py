"""Tests for state_normalizer: classification + ensure_registered."""

from pathlib import Path

import pytest

from src.everbot.core.slm.models import (
    CurrentPointer,
    VersionMetadata,
    VersionStatus,
)
from src.everbot.core.slm.state_normalizer import (
    FileState,
    RegistrationAction,
    StateInspector,
    ensure_registered,
)
from src.everbot.core.slm.version_manager import VersionManager


SKILL_MD_V1 = """\
---
name: s
version: "1.0.0"
---
body
"""


def _mk_ver_mgr(tmp_path: Path) -> VersionManager:
    (tmp_path / "skills").mkdir()
    (tmp_path / "eval").mkdir()
    return VersionManager(tmp_path / "skills", eval_base_dir=tmp_path / "eval")


class TestStateInspector:
    def test_all_missing(self, tmp_path: Path):
        vm = _mk_ver_mgr(tmp_path)
        state = StateInspector(vm).inspect("foo")
        assert state == FileState(
            skill_md_exists=False,
            skill_md_version=None,
            pointer=None,
            metadata=None,
            snapshot_exists=False,
        )

    def test_skill_md_only(self, tmp_path: Path):
        vm = _mk_ver_mgr(tmp_path)
        (tmp_path / "skills" / "foo").mkdir()
        (tmp_path / "skills" / "foo" / "SKILL.md").write_text(SKILL_MD_V1)
        state = StateInspector(vm).inspect("foo")
        assert state.skill_md_exists
        assert state.skill_md_version == "1.0.0"
        assert state.pointer is None
        assert state.metadata is None
        assert not state.snapshot_exists

    def test_fully_registered(self, tmp_path: Path):
        vm = _mk_ver_mgr(tmp_path)
        vm.publish("foo", "1.0.0", SKILL_MD_V1)
        state = StateInspector(vm).inspect("foo")
        assert state.skill_md_exists
        assert state.skill_md_version == "1.0.0"
        assert state.pointer is not None
        assert state.pointer.current_version == "1.0.0"
        assert state.metadata is not None
        assert state.snapshot_exists


class TestEnsureRegisteredBootstrap:
    def test_fresh_skill_with_repo_baseline(self, tmp_path: Path):
        vm = _mk_ver_mgr(tmp_path)
        # simulate a repo baseline: same skill_id exists in repo_skills_dir
        repo = tmp_path / "repo_skills"
        (repo / "foo").mkdir(parents=True)
        (repo / "foo" / "SKILL.md").write_text(SKILL_MD_V1)
        # and also exists in user skills_dir (normal layered setup)
        (tmp_path / "skills" / "foo").mkdir(parents=True)
        (tmp_path / "skills" / "foo" / "SKILL.md").write_text(SKILL_MD_V1)

        result = ensure_registered(vm, "foo", repo_skills_dir=repo)

        assert result.action == RegistrationAction.BOOTSTRAPPED
        pointer = vm.get_pointer("foo")
        assert pointer is not None
        assert pointer.current_version == "1.0.0"
        assert pointer.stable_version == "1.0.0"
        assert pointer.repo_baseline is True
        meta = vm.get_metadata("foo", "1.0.0")
        assert meta is not None
        assert meta.status == VersionStatus.ACTIVE
        snapshot = (tmp_path / "eval" / "foo" / "versions" / "v1.0.0" / "skill.md")
        assert snapshot.exists()

    def test_fresh_skill_user_installed(self, tmp_path: Path):
        vm = _mk_ver_mgr(tmp_path)
        (tmp_path / "skills" / "bar").mkdir(parents=True)
        (tmp_path / "skills" / "bar" / "SKILL.md").write_text(SKILL_MD_V1)
        # NO repo entry for bar

        result = ensure_registered(vm, "bar", repo_skills_dir=tmp_path / "repo_skills")

        assert result.action == RegistrationAction.BOOTSTRAPPED
        pointer = vm.get_pointer("bar")
        assert pointer is not None
        assert pointer.current_version == "1.0.0"
        assert pointer.repo_baseline is False
        meta = vm.get_metadata("bar", "1.0.0")
        assert meta is not None
        assert meta.status == VersionStatus.ACTIVE
        snapshot = tmp_path / "eval" / "bar" / "versions" / "v1.0.0" / "skill.md"
        assert snapshot.exists()

    def test_skill_md_missing_is_noop(self, tmp_path: Path):
        vm = _mk_ver_mgr(tmp_path)
        result = ensure_registered(vm, "ghost", repo_skills_dir=None)
        assert result.action == RegistrationAction.SKILL_MISSING
        assert vm.get_pointer("ghost") is None

    def test_already_registered_is_noop(self, tmp_path: Path):
        vm = _mk_ver_mgr(tmp_path)
        vm.publish("foo", "1.0.0", SKILL_MD_V1)
        vm.activate("foo", "1.0.0")
        result = ensure_registered(vm, "foo", repo_skills_dir=None)
        assert result.action == RegistrationAction.NOOP

    def test_bootstrap_populates_eval_summary_from_existing_report(self, tmp_path: Path):
        """D3-A: if eval_report.json exists before bootstrap, metadata.eval_summary
        must reflect its critical_rate and satisfaction."""
        from src.everbot.core.slm.models import EvalReport
        vm = _mk_ver_mgr(tmp_path)
        (tmp_path / "skills" / "baz").mkdir(parents=True)
        (tmp_path / "skills" / "baz" / "SKILL.md").write_text(SKILL_MD_V1)
        # Pre-seed an unhealthy eval_report (mirrors the paper-discovery
        # production state the migration needs to handle).
        report = EvalReport(
            skill_id="baz", skill_version="1.0.0",
            evaluated_at="2026-04-24T00:00:00",
            segment_count=8, critical_issue_count=4,
            critical_issue_rate=0.5, mean_satisfaction=0.51,
            results=[],
        )
        vm.save_eval_report("baz", "1.0.0", report)

        result = ensure_registered(vm, "baz", repo_skills_dir=None)

        assert result.action == RegistrationAction.BOOTSTRAPPED
        meta = vm.get_metadata("baz", "1.0.0")
        assert meta is not None
        assert meta.eval_summary is not None
        assert meta.eval_summary["critical_issue_rate"] == 0.5
        assert meta.eval_summary["satisfaction_score"] == 0.51

    def test_bootstrap_leaves_eval_summary_none_when_no_report(self, tmp_path: Path):
        vm = _mk_ver_mgr(tmp_path)
        (tmp_path / "skills" / "qux").mkdir(parents=True)
        (tmp_path / "skills" / "qux" / "SKILL.md").write_text(SKILL_MD_V1)

        result = ensure_registered(vm, "qux", repo_skills_dir=None)

        assert result.action == RegistrationAction.BOOTSTRAPPED
        meta = vm.get_metadata("qux", "1.0.0")
        assert meta is not None
        assert meta.eval_summary is None


class TestEnsureRegisteredRepair:
    def test_missing_metadata_is_repaired(self, tmp_path: Path):
        vm = _mk_ver_mgr(tmp_path)
        vm.publish("foo", "1.0.0", SKILL_MD_V1)
        # Delete metadata.json, leave pointer + snapshot
        meta_path = tmp_path / "eval" / "foo" / "versions" / "v1.0.0" / "metadata.json"
        meta_path.unlink()

        result = ensure_registered(vm, "foo", repo_skills_dir=None)

        assert result.action == RegistrationAction.REPAIRED_METADATA
        meta = vm.get_metadata("foo", "1.0.0")
        assert meta is not None
        assert meta.status == VersionStatus.ACTIVE

    def test_missing_snapshot_is_repaired(self, tmp_path: Path):
        vm = _mk_ver_mgr(tmp_path)
        vm.publish("foo", "1.0.0", SKILL_MD_V1)
        snap_path = tmp_path / "eval" / "foo" / "versions" / "v1.0.0" / "skill.md"
        snap_path.unlink()

        result = ensure_registered(vm, "foo", repo_skills_dir=None)

        assert result.action == RegistrationAction.REPAIRED_SNAPSHOT
        assert snap_path.exists()
        assert snap_path.read_text() == SKILL_MD_V1

    def test_version_conflict_is_not_auto_fixed(self, tmp_path: Path):
        """If SKILL.md version differs from pointer.current_version,
        ensure_registered must NOT auto-resolve — it must flag CONFLICT_DETECTED
        and leave disk state untouched (per policy D1-A)."""
        vm = _mk_ver_mgr(tmp_path)
        vm.publish("foo", "1.0.0", SKILL_MD_V1)
        # Corrupt: user edits SKILL.md to 1.1.0 and deletes snapshot
        (tmp_path / "skills" / "foo" / "SKILL.md").write_text(
            SKILL_MD_V1.replace('"1.0.0"', '"1.1.0"')
        )
        snap_path = tmp_path / "eval" / "foo" / "versions" / "v1.0.0" / "skill.md"
        snap_path.unlink()

        result = ensure_registered(vm, "foo", repo_skills_dir=None)

        assert result.action == RegistrationAction.CONFLICT_DETECTED
        # Snapshot NOT reconstructed from mismatched SKILL.md
        assert not snap_path.exists()
        # Pointer untouched — still points at 1.0.0
        pointer = vm.get_pointer("foo")
        assert pointer.current_version == "1.0.0"

    def test_both_missing_reports_repaired_metadata(self, tmp_path: Path):
        """When both snapshot and metadata are missing, action == REPAIRED_METADATA
        (metadata repair is the final write, so it wins for reporting)."""
        vm = _mk_ver_mgr(tmp_path)
        vm.publish("foo", "1.0.0", SKILL_MD_V1)
        meta_path = tmp_path / "eval" / "foo" / "versions" / "v1.0.0" / "metadata.json"
        snap_path = tmp_path / "eval" / "foo" / "versions" / "v1.0.0" / "skill.md"
        meta_path.unlink()
        snap_path.unlink()

        result = ensure_registered(vm, "foo", repo_skills_dir=None)

        assert result.action == RegistrationAction.REPAIRED_METADATA
        assert snap_path.exists()
        meta = vm.get_metadata("foo", "1.0.0")
        assert meta is not None
        assert meta.status == VersionStatus.ACTIVE


class TestStateInspectorLayered:
    def test_skill_md_exists_when_only_lower_layer_has_it(self, tmp_path: Path):
        """The 'exists' question is about the loader's perspective: any
        layer counts. Writable layer empty + lower layer has it = exists."""
        writable = tmp_path / "writable"
        readable = tmp_path / "readable"
        writable.mkdir()
        (readable / "foo").mkdir(parents=True)
        (readable / "foo" / "SKILL.md").write_text(
            '---\nname: foo\nversion: "1.0.0"\n---\nbody\n'
        )

        vm = VersionManager(
            writable, eval_base_dir=tmp_path / "eval",
            read_skill_dirs=[writable, readable],
        )
        state = StateInspector(vm).inspect("foo")
        assert state.skill_md_exists is True
        assert state.skill_md_version == "1.0.0"

    def test_skill_md_missing_when_no_layer_has_it(self, tmp_path: Path):
        writable = tmp_path / "writable"
        writable.mkdir()
        vm = VersionManager(
            writable, eval_base_dir=tmp_path / "eval",
            read_skill_dirs=[writable],
        )
        state = StateInspector(vm).inspect("ghost")
        assert state.skill_md_exists is False
        assert state.skill_md_version is None
