"""Tests for stale per-agent skill override drift detection (#132).

Covers spec v1 acceptance criteria:
  (a) stale override (content differs, override not newer) -> detected + warned
  (b) intentional override (content differs, override mtime newer) -> not stale
  (c) no override -> not stale
plus boundary/degradation cases:
  - identical content (e.g. symlink to repo) -> not stale even if older
  - no repo baseline -> not stale (nothing to compare against)
  - only .py/.md files count toward the content hash
  - check_skill_override_drift emits the spec-mandated warning line
  - list_stale_skill_overrides returns only the stale skills, sorted
"""

import logging
import os
from pathlib import Path

import pytest

from src.everbot.infra.user_data import UserDataManager


def _write(path: Path, content: str, mtime: float | None = None) -> None:
    """Write a file (creating parents) and optionally pin its mtime."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


# Stable, ordered timestamps so "older / newer" is unambiguous.
OLD = 1_700_000_000.0
NEW = 1_700_100_000.0


@pytest.fixture
def udm(tmp_path: Path, monkeypatch) -> UserDataManager:
    """A UserDataManager whose repo baseline resolves to a hermetic tmp repo."""
    monkeypatch.setenv("ALFRED_REPO_ROOT", str(tmp_path / "repo"))
    (tmp_path / "repo" / "skills").mkdir(parents=True)
    return UserDataManager(alfred_home=tmp_path)


def _override_dir(udm: UserDataManager, agent: str, skill: str) -> Path:
    return udm.get_agent_writable_skills_dir(agent) / skill


def _repo_dir(udm: UserDataManager, skill: str) -> Path:
    assert udm.repo_skills_dir is not None
    return udm.repo_skills_dir / skill


class TestIsSkillOverrideStale:
    def test_stale_when_content_differs_and_override_not_newer(self, udm):
        # (a) repo got a fix; agent override is older and still has old content.
        _write(_repo_dir(udm, "tw") / "fetch.py", "fixed\n", mtime=NEW)
        _write(_override_dir(udm, "demo", "tw") / "fetch.py", "old\n", mtime=OLD)

        result = udm.is_skill_override_stale("demo", "tw")

        assert result is not None
        agent_hash, repo_hash = result
        assert agent_hash != repo_hash
        assert len(agent_hash) >= 7 and len(repo_hash) >= 7

    def test_not_stale_when_override_is_newer(self, udm):
        # (b) intentional customization: differs but was edited after the repo.
        _write(_repo_dir(udm, "tw") / "fetch.py", "baseline\n", mtime=OLD)
        _write(_override_dir(udm, "demo", "tw") / "fetch.py", "customized\n", mtime=NEW)

        assert udm.is_skill_override_stale("demo", "tw") is None

    def test_not_stale_when_no_override(self, udm):
        # (c) repo only, no per-agent copy.
        _write(_repo_dir(udm, "tw") / "fetch.py", "baseline\n", mtime=NEW)

        assert udm.is_skill_override_stale("demo", "tw") is None

    def test_not_stale_when_content_identical_even_if_older(self, udm):
        # symlink-to-repo / byte-identical copy must never warn.
        _write(_repo_dir(udm, "tw") / "fetch.py", "same\n", mtime=NEW)
        _write(_override_dir(udm, "demo", "tw") / "fetch.py", "same\n", mtime=OLD)

        assert udm.is_skill_override_stale("demo", "tw") is None

    def test_symlink_override_to_repo_not_stale(self, udm):
        # contrarian-signals pattern: override dir is a symlink to the repo dir.
        _write(_repo_dir(udm, "cs") / "run.py", "fixed\n", mtime=NEW)
        link = _override_dir(udm, "demo", "cs")
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(_repo_dir(udm, "cs"))

        assert udm.is_skill_override_stale("demo", "cs") is None

    def test_not_stale_when_no_repo_baseline(self, udm):
        # override exists but repo has no such skill -> nothing to compare.
        _write(_override_dir(udm, "demo", "ghost") / "fetch.py", "x\n", mtime=OLD)

        assert udm.is_skill_override_stale("demo", "ghost") is None

    def test_not_stale_when_repo_dir_missing_entirely(self, tmp_path, monkeypatch):
        # repo_skills_dir resolves to None -> degrade gracefully, never crash.
        u = UserDataManager(alfred_home=tmp_path)
        monkeypatch.setattr(type(u), "repo_skills_dir", property(lambda self: None))
        _write(_override_dir(u, "demo", "tw") / "fetch.py", "x\n", mtime=OLD)

        assert u.is_skill_override_stale("demo", "tw") is None

    def test_only_py_and_md_files_count_toward_hash(self, udm):
        # A differing non-(.py/.md) file must NOT trigger drift; .py/.md identical.
        _write(_repo_dir(udm, "tw") / "fetch.py", "same\n", mtime=NEW)
        _write(_repo_dir(udm, "tw") / "data.json", '{"v":1}\n', mtime=NEW)
        _write(_override_dir(udm, "demo", "tw") / "fetch.py", "same\n", mtime=OLD)
        _write(_override_dir(udm, "demo", "tw") / "data.json", '{"v":2}\n', mtime=OLD)

        assert udm.is_skill_override_stale("demo", "tw") is None

    def test_markdown_change_triggers_drift(self, udm):
        # analyze.md missing the #124 requirement is exactly the real-world case.
        _write(_repo_dir(udm, "tw") / "analyze.md", "must cite source\n", mtime=NEW)
        _write(_override_dir(udm, "demo", "tw") / "analyze.md", "old prompt\n", mtime=OLD)

        assert udm.is_skill_override_stale("demo", "tw") is not None

    def test_nested_subdir_files_counted(self, udm):
        # scripts/ live under the skill dir; drift there must be caught.
        _write(_repo_dir(udm, "tw") / "scripts" / "fetch.py", "fixed\n", mtime=NEW)
        _write(_override_dir(udm, "demo", "tw") / "scripts" / "fetch.py", "old\n", mtime=OLD)

        assert udm.is_skill_override_stale("demo", "tw") is not None


class TestCheckSkillOverrideDrift:
    def test_emits_structured_warning_when_stale(self, udm, caplog):
        _write(_repo_dir(udm, "tw") / "fetch.py", "fixed\n", mtime=NEW)
        _write(_override_dir(udm, "demo", "tw") / "fetch.py", "old\n", mtime=OLD)

        with caplog.at_level(logging.WARNING, logger="src.everbot.infra.user_data"):
            result = udm.check_skill_override_drift("demo", "tw")

        assert result is not None
        msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any(
            "tw" in m and "per-agent override is stale" in m and "repo baseline" in m
            for m in msgs
        ), msgs

    def test_silent_when_not_stale(self, udm, caplog):
        _write(_repo_dir(udm, "tw") / "fetch.py", "same\n", mtime=NEW)
        _write(_override_dir(udm, "demo", "tw") / "fetch.py", "same\n", mtime=OLD)

        with caplog.at_level(logging.WARNING, logger="src.everbot.infra.user_data"):
            result = udm.check_skill_override_drift("demo", "tw")

        assert result is None
        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


class TestListStaleSkillOverrides:
    def test_returns_only_stale_skills_sorted(self, udm):
        # stale: differs + older.  fresh: differs + newer.  same: identical.
        _write(_repo_dir(udm, "invest") / "a.py", "fix\n", mtime=NEW)
        _write(_override_dir(udm, "demo", "invest") / "a.py", "old\n", mtime=OLD)

        _write(_repo_dir(udm, "web") / "a.py", "base\n", mtime=OLD)
        _write(_override_dir(udm, "demo", "web") / "a.py", "custom\n", mtime=NEW)

        _write(_repo_dir(udm, "twitter-watch") / "a.py", "fix\n", mtime=NEW)
        _write(_override_dir(udm, "demo", "twitter-watch") / "a.py", "old\n", mtime=OLD)

        assert udm.list_stale_skill_overrides("demo") == ["invest", "twitter-watch"]

    def test_empty_when_agent_has_no_overrides(self, udm):
        assert udm.list_stale_skill_overrides("nobody") == []
