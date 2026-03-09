#!/usr/bin/env python3
"""Unit tests for coding-master v3 tools.py — core functions only."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

# Add scripts dir to path
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import tools


# ══════════════════════════════════════════════════════════
#  Fixtures
# ══════════════════════════════════════════════════════════


@pytest.fixture
def tmp_dir(tmp_path):
    """Create a temp dir and return it."""
    return tmp_path


@pytest.fixture
def git_repo(tmp_path):
    """Create a bare git repo for testing."""
    repo = tmp_path / "test-repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True)
    # Initial commit
    (repo / "README.md").write_text("# Test")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)
    return repo


# ══════════════════════════════════════════════════════════
#  _atomic_json_update
# ══════════════════════════════════════════════════════════


class TestAtomicJsonUpdate:
    def test_create_file(self, tmp_dir):
        path = tmp_dir / "test.json"
        result = tools._atomic_json_update(path, lambda d: (d.update({"x": 1}), {"ok": True})[1])
        assert result["ok"]
        assert json.loads(path.read_text()) == {"x": 1}

    def test_empty_file(self, tmp_dir):
        path = tmp_dir / "test.json"
        path.touch()
        result = tools._atomic_json_update(path, lambda d: (d.update({"x": 1}), {"ok": True})[1])
        assert result["ok"]
        assert json.loads(path.read_text()) == {"x": 1}

    def test_read_existing(self, tmp_dir):
        path = tmp_dir / "test.json"
        path.write_text('{"a": 1}')
        captured = {}
        def updater(d):
            captured.update(d)
            d["b"] = 2
            return {"ok": True}
        tools._atomic_json_update(path, updater)
        assert captured == {"a": 1}
        assert json.loads(path.read_text()) == {"a": 1, "b": 2}

    def test_rollback_on_failure(self, tmp_dir):
        path = tmp_dir / "test.json"
        path.write_text('{"a": 1}')
        def updater(d):
            d["a"] = 999  # modify
            return {"ok": False, "error": "nope"}
        result = tools._atomic_json_update(path, updater)
        assert not result["ok"]
        assert json.loads(path.read_text()) == {"a": 1}  # restored

    def test_no_write_on_failure_no_change(self, tmp_dir):
        path = tmp_dir / "test.json"
        path.write_text('{"a": 1}')
        mtime_before = path.stat().st_mtime_ns
        def updater(d):
            return {"ok": False, "error": "nope"}
        tools._atomic_json_update(path, updater)
        # File content unchanged
        assert json.loads(path.read_text()) == {"a": 1}

    def test_corrupt_json(self, tmp_dir):
        path = tmp_dir / "test.json"
        path.write_text("{broken")
        result = tools._atomic_json_update(path, lambda d: (d.update({"x": 1}), {"ok": True})[1])
        assert result["ok"]
        assert json.loads(path.read_text()) == {"x": 1}


# ══════════════════════════════════════════════════════════
#  _parse_plan_md
# ══════════════════════════════════════════════════════════


SAMPLE_PLAN = """\
# Feature Plan

## Origin Task
Refactor inspector module

## Features

### Feature 1: Extract Scanner Interface
**Depends on**: —

#### Task
Extract scan logic into SessionScanner class.

#### Acceptance Criteria
- [ ] SessionScanner class exists
- [ ] All tests pass

---

### Feature 2: Incremental Scan
**Depends on**: Feature 1

#### Task
Implement incremental scan based on watermark.

#### Acceptance Criteria
- [ ] Only processes new messages
- [ ] Performance < 100ms

---

### Feature 3: Split ReportGenerator
**Depends on**: Feature 1

#### Task
Split report generation into separate class.

#### Acceptance Criteria
- [ ] ReportGenerator exists

---

### Feature 4: Integration Tests
**Depends on**: Feature 2, Feature 3

#### Task
Add integration tests.

#### Acceptance Criteria
- [ ] 3+ test scenarios
"""


class TestParsePlanMd:
    def test_standard(self, tmp_dir):
        path = tmp_dir / "PLAN.md"
        path.write_text(SAMPLE_PLAN)
        plan = tools._parse_plan_md(path)
        assert len(plan) == 4
        assert plan["1"]["title"] == "Extract Scanner Interface"
        assert plan["1"]["depends_on"] == []
        assert plan["2"]["depends_on"] == ["1"]
        assert plan["4"]["depends_on"] == ["2", "3"]
        assert "SessionScanner" in plan["1"]["task"]
        assert "SessionScanner" in plan["1"]["criteria"]

    def test_missing_file(self, tmp_dir):
        assert tools._parse_plan_md(tmp_dir / "nope.md") == {}

    def test_empty_file(self, tmp_dir):
        path = tmp_dir / "PLAN.md"
        path.write_text("")
        assert tools._parse_plan_md(path) == {}

    def test_single_feature(self, tmp_dir):
        path = tmp_dir / "PLAN.md"
        path.write_text("""\
### Feature 1: Simple Task
**Depends on**: —

#### Task
Do something.

#### Acceptance Criteria
- [ ] It works
""")
        plan = tools._parse_plan_md(path)
        assert len(plan) == 1
        assert plan["1"]["depends_on"] == []

    def test_partial_format_error(self, tmp_dir):
        path = tmp_dir / "PLAN.md"
        path.write_text("""\
### Feature 1: Good
**Depends on**: —

#### Task
Task 1

#### Acceptance Criteria
- [ ] AC 1

---

### Feature 2: Missing Task
**Depends on**: Feature 1

#### Acceptance Criteria
- [ ] AC 2
""")
        plan = tools._parse_plan_md(path)
        assert len(plan) == 2
        assert plan["1"]["task"] == "Task 1"
        assert plan["2"]["task"] == ""  # missing but parsed


# ══════════════════════════════════════════════════════════
#  _topo_sort
# ══════════════════════════════════════════════════════════


class TestTopoSort:
    def test_no_deps(self):
        plan = {"1": {"depends_on": []}, "2": {"depends_on": []}}
        result = tools._topo_sort(plan)
        assert set(result) == {"1", "2"}

    def test_linear(self):
        plan = {
            "1": {"depends_on": []},
            "2": {"depends_on": ["1"]},
            "3": {"depends_on": ["2"]},
        }
        assert tools._topo_sort(plan) == ["1", "2", "3"]

    def test_diamond(self):
        plan = {
            "1": {"depends_on": []},
            "2": {"depends_on": ["1"]},
            "3": {"depends_on": ["1"]},
            "4": {"depends_on": ["2", "3"]},
        }
        result = tools._topo_sort(plan)
        assert result[0] == "1"
        assert result[-1] == "4"
        assert result.index("2") < result.index("4")
        assert result.index("3") < result.index("4")

    def test_single(self):
        assert tools._topo_sort({"1": {"depends_on": []}}) == ["1"]

    def test_cycle_returns_partial(self):
        plan = {
            "1": {"depends_on": ["2"]},
            "2": {"depends_on": ["1"]},
        }
        result = tools._topo_sort(plan)
        assert len(result) < 2  # cycle detected (incomplete)


# ══════════════════════════════════════════════════════════
#  _slugify
# ══════════════════════════════════════════════════════════


class TestSlugify:
    def test_english(self):
        assert tools._slugify("Scanner Interface") == "scanner-interface"

    def test_special_chars(self):
        assert tools._slugify("fix: bug #123") == "fix-bug-123"

    def test_empty(self):
        assert tools._slugify("") == "feature"

    def test_truncate(self):
        result = tools._slugify("a" * 100)
        assert len(result) <= 30


# ══════════════════════════════════════════════════════════
#  _check_feature_md_sections
# ══════════════════════════════════════════════════════════


class TestCheckFeatureMdSections:
    def test_both_present(self, tmp_dir):
        md = tmp_dir / "feature.md"
        md.write_text("## Analysis\nSome analysis\n\n## Plan\n1. Step one\n")
        assert tools._check_feature_md_sections(md) == (True, True)

    def test_empty_sections(self, tmp_dir):
        md = tmp_dir / "feature.md"
        md.write_text("## Analysis\n\n## Plan\n\n## Dev Log\n")
        assert tools._check_feature_md_sections(md) == (False, False)

    def test_analysis_only(self, tmp_dir):
        md = tmp_dir / "feature.md"
        md.write_text("## Analysis\nContent here\n\n## Plan\n\n## Dev Log\n")
        assert tools._check_feature_md_sections(md) == (True, False)

    def test_none_path(self):
        assert tools._check_feature_md_sections(None) == (False, False)

    def test_missing_file(self, tmp_dir):
        assert tools._check_feature_md_sections(tmp_dir / "nope.md") == (False, False)


# ══════════════════════════════════════════════════════════
#  _is_expired
# ══════════════════════════════════════════════════════════


class TestIsExpired:
    def test_not_expired(self):
        from datetime import datetime, timezone, timedelta
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        assert not tools._is_expired({"lease_expires_at": future})

    def test_expired(self):
        assert tools._is_expired({"lease_expires_at": "2020-01-01T00:00:00+00:00"})

    def test_no_field(self):
        assert not tools._is_expired({})


# ══════════════════════════════════════════════════════════
#  _append_journal
# ══════════════════════════════════════════════════════════


class TestAppendJournal:
    def test_create_file(self, tmp_dir):
        tools._append_journal(tmp_dir, "agent-a", "lock", "test message")
        journal = (tmp_dir / tools.CM_DIR / "JOURNAL.md").read_text()
        assert "[agent-a] lock" in journal
        assert "test message" in journal

    def test_append(self, tmp_dir):
        tools._append_journal(tmp_dir, "agent-a", "lock")
        tools._append_journal(tmp_dir, "agent-b", "claim")
        journal = (tmp_dir / tools.CM_DIR / "JOURNAL.md").read_text()
        assert "[agent-a] lock" in journal
        assert "[agent-b] claim" in journal

    def test_empty_message(self, tmp_dir):
        tools._append_journal(tmp_dir, "agent-a", "claim")
        journal = (tmp_dir / tools.CM_DIR / "JOURNAL.md").read_text()
        assert "[agent-a] claim" in journal


# ══════════════════════════════════════════════════════════
#  cmd_lock / cmd_unlock — integration with real git
# ══════════════════════════════════════════════════════════


class TestCmdLock:
    def _make_args(self, repo_name, **kwargs):
        args = mock.MagicMock()
        args.repo = repo_name
        args.agent = kwargs.get("agent", "test-agent")
        args.branch = kwargs.get("branch", None)
        return args

    def test_lock_success(self, git_repo):
        with mock.patch.object(tools, "_repo_path", return_value=git_repo):
            args = self._make_args(git_repo.name)
            result = tools.cmd_lock(args)
            assert result["ok"]
            assert "branch" in result["data"]
            lock = json.loads((git_repo / tools.CM_DIR / "lock.json").read_text())
            assert lock["session_phase"] == "locked"
            assert lock["locked_by"] == "test-agent"

    def test_lock_twice(self, git_repo):
        with mock.patch.object(tools, "_repo_path", return_value=git_repo):
            args = self._make_args(git_repo.name)
            r1 = tools.cmd_lock(args)
            assert r1["ok"]
            r2 = tools.cmd_lock(args)
            assert not r2["ok"]
            assert "already locked" in r2["error"]

    def test_lock_dirty_tree(self, git_repo):
        (git_repo / "dirty.txt").write_text("dirty")
        with mock.patch.object(tools, "_repo_path", return_value=git_repo):
            args = self._make_args(git_repo.name)
            result = tools.cmd_lock(args)
            assert not result["ok"]
            assert "not clean" in result["error"]

    def test_unlock(self, git_repo):
        with mock.patch.object(tools, "_repo_path", return_value=git_repo):
            args = self._make_args(git_repo.name)
            tools.cmd_lock(args)
            result = tools.cmd_unlock(args)
            assert result["ok"]
            lock = json.loads((git_repo / tools.CM_DIR / "lock.json").read_text())
            assert lock == {}


# ══════════════════════════════════════════════════════════
#  cmd_plan_ready
# ══════════════════════════════════════════════════════════


class TestCmdPlanReady:
    def _setup_locked(self, git_repo):
        with mock.patch.object(tools, "_repo_path", return_value=git_repo):
            args = mock.MagicMock(repo=git_repo.name, agent="test-agent", branch=None)
            tools.cmd_lock(args)
        return git_repo

    def test_plan_ready_success(self, git_repo):
        repo = self._setup_locked(git_repo)
        plan_path = repo / tools.CM_DIR / "PLAN.md"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text("""\
### Feature 1: Test Feature
**Depends on**: —

#### Task
Do something

#### Acceptance Criteria
- [ ] Works
""")
        with mock.patch.object(tools, "_repo_path", return_value=repo):
            args = mock.MagicMock(repo=repo.name, agent="test-agent")
            result = tools.cmd_plan_ready(args)
            assert result["ok"]
            lock = json.loads((repo / tools.CM_DIR / "lock.json").read_text())
            assert lock["session_phase"] == "reviewed"

    def test_plan_ready_no_plan(self, git_repo):
        repo = self._setup_locked(git_repo)
        with mock.patch.object(tools, "_repo_path", return_value=repo):
            args = mock.MagicMock(repo=repo.name, agent="test-agent")
            result = tools.cmd_plan_ready(args)
            assert not result["ok"]
            assert "not found" in result["error"]

    def test_plan_ready_missing_ac(self, git_repo):
        repo = self._setup_locked(git_repo)
        plan_path = repo / tools.CM_DIR / "PLAN.md"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text("""\
### Feature 1: Test
**Depends on**: —

#### Task
Do stuff
""")
        with mock.patch.object(tools, "_repo_path", return_value=repo):
            args = mock.MagicMock(repo=repo.name, agent="test-agent")
            result = tools.cmd_plan_ready(args)
            assert not result["ok"]
            assert "Acceptance Criteria" in str(result.get("data", {}).get("issues", []))

    def test_plan_ready_cycle(self, git_repo):
        repo = self._setup_locked(git_repo)
        plan_path = repo / tools.CM_DIR / "PLAN.md"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text("""\
### Feature 1: A
**Depends on**: Feature 2

#### Task
Task A

#### Acceptance Criteria
- [ ] AC

---

### Feature 2: B
**Depends on**: Feature 1

#### Task
Task B

#### Acceptance Criteria
- [ ] AC
""")
        with mock.patch.object(tools, "_repo_path", return_value=repo):
            args = mock.MagicMock(repo=repo.name, agent="test-agent")
            result = tools.cmd_plan_ready(args)
            assert not result["ok"]
            assert "cycle" in str(result.get("data", {}).get("issues", [])).lower()

    def test_plan_ready_idempotent(self, git_repo):
        repo = self._setup_locked(git_repo)
        plan_path = repo / tools.CM_DIR / "PLAN.md"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text("""\
### Feature 1: Test
**Depends on**: —

#### Task
Do stuff

#### Acceptance Criteria
- [ ] Works
""")
        with mock.patch.object(tools, "_repo_path", return_value=repo):
            args = mock.MagicMock(repo=repo.name, agent="test-agent")
            r1 = tools.cmd_plan_ready(args)
            assert r1["ok"]
            r2 = tools.cmd_plan_ready(args)
            assert r2["ok"]  # idempotent
