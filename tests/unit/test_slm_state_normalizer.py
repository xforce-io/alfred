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
