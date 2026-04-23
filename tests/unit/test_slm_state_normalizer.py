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
        assert pointer.repo_baseline is False

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
