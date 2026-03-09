#!/usr/bin/env python3
"""Workflow-oriented tests for coding-master tools.

Covers evidence flow, progress actions, preconditions, integration reporting,
reopen context, session start, and extended status/submit contracts.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest import mock

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import tools


# ══════════════════════════════════════════════════════════
#  Helpers (reused from test_tools_e2e.py)
# ══════════════════════════════════════════════════════════

SINGLE_FEATURE_PLAN = """\
# Feature Plan

## Origin Task
Add a utility function

## Features

### Feature 1: Add foo utility
**Depends on**: —

#### Task
Create foo.py with a foo() function that returns 42.

#### Acceptance Criteria
- [ ] foo() exists and returns 42
- [ ] Tests pass
"""

TWO_FEATURE_PLAN = """\
# Feature Plan

## Origin Task
Two features

## Features

### Feature 1: Base
**Depends on**: —

#### Task
Create base.

#### Acceptance Criteria
- [ ] Base exists

---

### Feature 2: Extension
**Depends on**: Feature 1

#### Task
Extend base.

#### Acceptance Criteria
- [ ] Extension works
"""


def make_args(**kwargs):
    defaults = {
        "repo": None, "agent": "test-agent", "branch": None,
        "feature": None, "title": None, "message": None, "fix": False,
        "plan_file": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "test-repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True)
    (repo / "README.md").write_text("# Test")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True, check=True)
    return repo


def _mock_repo(git_repo):
    return mock.patch.multiple(
        tools,
        _repo_path=mock.MagicMock(return_value=git_repo),
        _run_tests=mock.MagicMock(return_value={"ok": True, "output": "3 passed, 0 failed"}),
    )


def _setup_locked(git_repo):
    with _mock_repo(git_repo):
        r = tools.cmd_lock(make_args(repo=git_repo.name))
        assert r["ok"], r
    return git_repo


def _setup_reviewed(git_repo, plan_text=SINGLE_FEATURE_PLAN):
    repo = _setup_locked(git_repo)
    plan_path = repo / tools.CM_DIR / "PLAN.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(plan_text)
    with _mock_repo(repo):
        r = tools.cmd_plan_ready(make_args(repo=repo.name))
        assert r["ok"], r
    return repo


def _setup_claimed(git_repo, feature_id=1, plan_text=SINGLE_FEATURE_PLAN):
    repo = _setup_reviewed(git_repo, plan_text)
    with _mock_repo(repo):
        r = tools.cmd_claim(make_args(repo=repo.name, feature=feature_id))
        assert r["ok"], r
    return repo, r["data"]


def _fill_analysis_plan(repo, feature_id):
    feature_md = tools._find_feature_md(repo, str(feature_id))
    content = feature_md.read_text()
    content = content.replace("## Analysis\n", "## Analysis\n- Analyzed the code\n")
    content = content.replace("## Plan\n", "## Plan\n1. Implement it\n")
    feature_md.write_text(content)


def _setup_developing(git_repo, feature_id=1, plan_text=SINGLE_FEATURE_PLAN):
    repo, claim_data = _setup_claimed(git_repo, feature_id, plan_text)
    _fill_analysis_plan(repo, feature_id)
    with _mock_repo(repo):
        r = tools.cmd_dev(make_args(repo=repo.name, feature=feature_id))
        assert r["ok"], r
    return repo, claim_data


def _write_and_commit(worktree_path, filename="foo.py", content="def foo(): return 42\n"):
    wt = Path(worktree_path)
    (wt / filename).write_text(content)
    subprocess.run(["git", "add", filename], cwd=wt, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", f"add {filename}"], cwd=wt, capture_output=True, check=True)


# ══════════════════════════════════════════════════════════
#  Phase 1: Evidence layer
# ══════════════════════════════════════════════════════════


class TestCmTestWritesEvidence:
    def test_evidence_file_created(self, git_repo):
        repo, data = _setup_developing(git_repo)
        with _mock_repo(repo):
            _write_and_commit(data["worktree"])
            r = tools.cmd_test(make_args(repo=repo.name, feature=1))
            assert r["ok"]

        evidence_path = repo / tools.CM_DIR / tools.EVIDENCE_DIR / "1-verify.json"
        assert evidence_path.exists()
        evidence = json.loads(evidence_path.read_text())
        assert evidence["feature_id"] == "1"
        assert evidence["overall"] == "passed"
        assert evidence["lint"]["passed"] is True
        assert evidence["typecheck"]["passed"] is True
        assert evidence["test"]["passed"] is True
        assert "commit" in evidence
        assert "created_at" in evidence

    def test_evidence_returns_in_data(self, git_repo):
        repo, data = _setup_developing(git_repo)
        with _mock_repo(repo):
            _write_and_commit(data["worktree"])
            r = tools.cmd_test(make_args(repo=repo.name, feature=1))
            assert r["ok"]
            assert "evidence" in r["data"]
            assert r["data"]["evidence"]["overall"] == "passed"

    def test_evidence_failed_test(self, git_repo):
        repo, data = _setup_developing(git_repo)
        with mock.patch.object(tools, "_repo_path", return_value=repo), \
             mock.patch.object(tools, "_run_tests", return_value={"ok": False, "output": "FAILED: test_x"}):
            _write_and_commit(data["worktree"])
            r = tools.cmd_test(make_args(repo=repo.name, feature=1))
            assert r["ok"]  # command succeeds, but test_passed=False

        evidence = json.loads((repo / tools.CM_DIR / tools.EVIDENCE_DIR / "1-verify.json").read_text())
        assert evidence["overall"] == "failed"
        assert evidence["test"]["passed"] is False

    def test_lint_not_configured_passes(self, git_repo):
        """No lint command → lint.passed=true, does not block."""
        repo, data = _setup_developing(git_repo)
        with _mock_repo(repo):
            _write_and_commit(data["worktree"])
            r = tools.cmd_test(make_args(repo=repo.name, feature=1))

        evidence = json.loads((repo / tools.CM_DIR / tools.EVIDENCE_DIR / "1-verify.json").read_text())
        assert evidence["lint"]["passed"] is True
        assert evidence["lint"]["command"] is None

    def test_typecheck_not_configured_passes(self, git_repo):
        """No typecheck command → typecheck.passed=true, does not block."""
        repo, data = _setup_developing(git_repo)
        with _mock_repo(repo):
            _write_and_commit(data["worktree"])
            tools.cmd_test(make_args(repo=repo.name, feature=1))

        evidence = json.loads((repo / tools.CM_DIR / tools.EVIDENCE_DIR / "1-verify.json").read_text())
        assert evidence["typecheck"]["passed"] is True
        assert evidence["typecheck"]["command"] is None

    def test_test_does_not_write_evidence_when_feature_not_developing(self, git_repo):
        """Rejected cm test must not leave behind evidence for invalid feature state."""
        repo, data = _setup_developing(git_repo)
        claims_path = repo / tools.CM_DIR / "claims.json"
        claims = json.loads(claims_path.read_text())
        claims["features"]["1"]["phase"] = "analyzing"
        claims_path.write_text(json.dumps(claims))

        with _mock_repo(repo):
            _write_and_commit(data["worktree"])
            r = tools.cmd_test(make_args(repo=repo.name, feature=1))
            assert not r["ok"]
            assert "expected developing" in r["error"]

        evidence_path = repo / tools.CM_DIR / tools.EVIDENCE_DIR / "1-verify.json"
        assert not evidence_path.exists()


class TestCmDoneWithEvidence:
    def test_done_with_evidence_succeeds(self, git_repo):
        """cm done succeeds when evidence file exists and is valid."""
        repo, data = _setup_developing(git_repo)
        with _mock_repo(repo):
            _write_and_commit(data["worktree"])
            tools.cmd_test(make_args(repo=repo.name, feature=1))
            r = tools.cmd_done(make_args(repo=repo.name, feature=1))
            assert r["ok"]

    def test_done_rejects_stale_evidence(self, git_repo):
        """cm done rejects when evidence commit ≠ HEAD."""
        repo, data = _setup_developing(git_repo)
        with _mock_repo(repo):
            _write_and_commit(data["worktree"])
            tools.cmd_test(make_args(repo=repo.name, feature=1))
            # New commit after test
            _write_and_commit(data["worktree"], "bar.py", "x = 1\n")
            r = tools.cmd_done(make_args(repo=repo.name, feature=1))
            assert not r["ok"]
            assert "stale" in r["error"].lower() or "changed" in r["error"].lower()

    def test_done_rejects_failed_lint(self, git_repo):
        """cm done rejects when lint failed in evidence."""
        repo, data = _setup_developing(git_repo)
        with mock.patch.object(tools, "_repo_path", return_value=repo), \
             mock.patch.object(tools, "_run_tests", return_value={"ok": True, "output": "3 passed"}), \
             mock.patch.object(tools, "_run_lint", return_value={"passed": False, "command": "ruff check .", "output": "E501"}):
            _write_and_commit(data["worktree"])
            tools.cmd_test(make_args(repo=repo.name, feature=1))
            r = tools.cmd_done(make_args(repo=repo.name, feature=1))
            assert not r["ok"]
            assert "lint" in r["error"].lower()

    def test_evidence_backward_compat(self, git_repo):
        """Old session without evidence/ → cm done falls back to legacy check."""
        repo, data = _setup_developing(git_repo)
        with _mock_repo(repo):
            _write_and_commit(data["worktree"])
            tools.cmd_test(make_args(repo=repo.name, feature=1))

        # Delete evidence file to simulate v3 session
        evidence_path = repo / tools.CM_DIR / tools.EVIDENCE_DIR / "1-verify.json"
        if evidence_path.exists():
            evidence_path.unlink()

        with _mock_repo(repo):
            r = tools.cmd_done(make_args(repo=repo.name, feature=1))
            assert r["ok"]  # falls back to claims.json check


# ══════════════════════════════════════════════════════════
#  Phase 1: next_action in progress
# ══════════════════════════════════════════════════════════


class TestProgressNextAction:
    def test_next_action_for_developing_feature(self, git_repo):
        """Agent with a developing feature gets next_action to test/done."""
        repo, data = _setup_developing(git_repo)
        with _mock_repo(repo):
            _write_and_commit(data["worktree"])
            tools.cmd_test(make_args(repo=repo.name, feature=1))
            r = tools.cmd_progress(make_args(repo=repo.name))
            assert r["ok"]
            na = r["data"]["next_action"]
            assert na is not None
            assert "done" in na["command"].lower()

    def test_next_action_suggests_claim(self, git_repo):
        """Agent with no features gets next_action to claim."""
        repo = _setup_reviewed(git_repo)
        with _mock_repo(repo):
            r = tools.cmd_progress(make_args(repo=repo.name))
            na = r["data"]["next_action"]
            assert na is not None
            assert "claim" in na["command"].lower()

    def test_next_action_null_when_blocked(self, git_repo):
        """Agent with no executable actions gets null."""
        repo, data = _setup_developing(git_repo, 1, TWO_FEATURE_PLAN)
        # Feature 1 developing, Feature 2 blocked by 1
        # Agent "other-agent" has nothing to do
        with _mock_repo(repo):
            r = tools.cmd_progress(make_args(repo=repo.name, agent="other-agent"))
            na = r["data"]["next_action"]
            # other-agent doesn't own feature 1, and feature 2 is blocked
            # So next_action should be null (feature 1 is owned by test-agent)
            assert na is None

    def test_next_action_skips_other_owner(self, git_repo):
        """next_action only shows features owned by current agent."""
        repo, data = _setup_developing(git_repo)
        with _mock_repo(repo):
            _write_and_commit(data["worktree"])
            tools.cmd_test(make_args(repo=repo.name, feature=1))
            # Progress as different agent
            r = tools.cmd_progress(make_args(repo=repo.name, agent="other-agent"))
            na = r["data"]["next_action"]
            # other-agent doesn't own feature 1, no pending features
            assert na is None

    def test_session_next_action_shows_global(self, git_repo):
        """session_next_action reflects global state regardless of agent."""
        repo, data = _setup_developing(git_repo)
        with _mock_repo(repo):
            _write_and_commit(data["worktree"])
            tools.cmd_test(make_args(repo=repo.name, feature=1))
            r = tools.cmd_progress(make_args(repo=repo.name, agent="other-agent"))
            sna = r["data"]["session_next_action"]
            assert sna is not None
            # Session should suggest doing something about feature 1

    def test_all_done_suggests_integrate(self, git_repo):
        repo, data = _setup_developing(git_repo)
        with _mock_repo(repo):
            _write_and_commit(data["worktree"])
            tools.cmd_test(make_args(repo=repo.name, feature=1))
            tools.cmd_done(make_args(repo=repo.name, feature=1))
            r = tools.cmd_progress(make_args(repo=repo.name))
            na = r["data"]["next_action"]
            assert na is not None
            assert "integrate" in na["command"].lower()

    def test_integrating_suggests_submit(self, git_repo):
        repo, data = _setup_developing(git_repo)
        with _mock_repo(repo):
            _write_and_commit(data["worktree"])
            tools.cmd_test(make_args(repo=repo.name, feature=1))
            tools.cmd_done(make_args(repo=repo.name, feature=1))
            tools.cmd_integrate(make_args(repo=repo.name))
            r = tools.cmd_progress(make_args(repo=repo.name))
            na = r["data"]["next_action"]
            assert na is not None
            assert "submit" in na["command"].lower()


# ══════════════════════════════════════════════════════════
#  Phase 2: Precondition checks
# ══════════════════════════════════════════════════════════


class TestPreconditionChecks:
    def test_rejects_expired_lease(self, git_repo):
        repo, data = _setup_developing(git_repo)
        # Expire the lease
        lock_path = repo / tools.CM_DIR / "lock.json"
        lock = json.loads(lock_path.read_text())
        lock["lease_expires_at"] = "2020-01-01T00:00:00+00:00"
        lock_path.write_text(json.dumps(lock))

        with mock.patch.object(tools, "_repo_path", return_value=repo):
            _write_and_commit(data["worktree"])
            r = tools.cmd_test(make_args(repo=repo.name, feature=1))
            assert not r["ok"]
            assert "expired" in r["error"].lower()

    def test_rejects_done_session(self, git_repo):
        repo = _setup_locked(git_repo)
        # Set session to done
        lock_path = repo / tools.CM_DIR / "lock.json"
        lock = json.loads(lock_path.read_text())
        lock["session_phase"] = "done"
        lock_path.write_text(json.dumps(lock))

        with _mock_repo(repo):
            r = tools.cmd_claim(make_args(repo=repo.name, feature=1))
            assert not r["ok"]
            assert "done" in r["error"].lower()

    def test_rejects_branch_mismatch(self, git_repo):
        repo, data = _setup_developing(git_repo)
        wt_path = Path(data["worktree"])

        # Switch branch in worktree to cause mismatch
        subprocess.run(["git", "checkout", "-b", "wrong-branch"], cwd=wt_path, capture_output=True)

        with mock.patch.object(tools, "_repo_path", return_value=repo), \
             mock.patch.object(tools, "_run_tests", return_value={"ok": True, "output": "ok"}):
            _write_and_commit(data["worktree"])
            r = tools.cmd_test(make_args(repo=repo.name, feature=1))
            assert not r["ok"]
            assert "mismatch" in r["error"].lower()


# ══════════════════════════════════════════════════════════
#  Phase 2: Integration report
# ══════════════════════════════════════════════════════════


class TestIntegrationReport:
    def test_integrate_success_writes_report(self, git_repo):
        repo, data = _setup_developing(git_repo)
        with _mock_repo(repo):
            _write_and_commit(data["worktree"])
            tools.cmd_test(make_args(repo=repo.name, feature=1))
            tools.cmd_done(make_args(repo=repo.name, feature=1))
            r = tools.cmd_integrate(make_args(repo=repo.name))
            assert r["ok"]

        report_path = repo / tools.CM_DIR / tools.EVIDENCE_DIR / "integration-report.json"
        assert report_path.exists()
        report = json.loads(report_path.read_text())
        assert report["overall"] == "passed"
        assert len(report["merge_results"]) >= 1
        assert report["merge_results"][0]["status"] == "merged"

    def test_integrate_test_failure_writes_report(self, git_repo):
        repo, data = _setup_developing(git_repo)
        with _mock_repo(repo):
            _write_and_commit(data["worktree"])
            tools.cmd_test(make_args(repo=repo.name, feature=1))
            tools.cmd_done(make_args(repo=repo.name, feature=1))

        with mock.patch.object(tools, "_repo_path", return_value=repo), \
             mock.patch.object(tools, "_run_tests", return_value={"ok": False, "output": "FAIL: test_x"}):
            r = tools.cmd_integrate(make_args(repo=repo.name))
            assert not r["ok"]

        report_path = repo / tools.CM_DIR / tools.EVIDENCE_DIR / "integration-report.json"
        assert report_path.exists()
        report = json.loads(report_path.read_text())
        assert report["overall"] == "failed"
        assert report["failure_type"] == "test_failure"
        assert report["all_merged"] is True


# ══════════════════════════════════════════════════════════
#  Phase 2: Reopen with failure context
# ══════════════════════════════════════════════════════════


class TestReopenWithContext:
    def test_reopen_carries_failure_context(self, git_repo):
        repo, data = _setup_developing(git_repo)
        with _mock_repo(repo):
            _write_and_commit(data["worktree"])
            tools.cmd_test(make_args(repo=repo.name, feature=1))
            tools.cmd_done(make_args(repo=repo.name, feature=1))

        # Simulate integration test failure
        with mock.patch.object(tools, "_repo_path", return_value=repo), \
             mock.patch.object(tools, "_run_tests", return_value={"ok": False, "output": "FAIL: assertion error"}):
            tools.cmd_integrate(make_args(repo=repo.name))

        with _mock_repo(repo):
            r = tools.cmd_reopen(make_args(repo=repo.name, feature=1))
            assert r["ok"]
            assert "failure_context" in r["data"]
            ctx = r["data"]["failure_context"]
            assert ctx["type"] == "test_failure"

    def test_reopen_without_report_still_works(self, git_repo):
        """Reopen works even without integration report (backward compat)."""
        repo, data = _setup_developing(git_repo)
        with _mock_repo(repo):
            _write_and_commit(data["worktree"])
            tools.cmd_test(make_args(repo=repo.name, feature=1))
            tools.cmd_done(make_args(repo=repo.name, feature=1))
            # Skip integrate, just reopen
            # Manually set session to integrating for reopen to work
            lock_path = repo / tools.CM_DIR / "lock.json"
            lock = json.loads(lock_path.read_text())
            lock["session_phase"] = "working"
            lock_path.write_text(json.dumps(lock))

            r = tools.cmd_reopen(make_args(repo=repo.name, feature=1))
            assert r["ok"]
            assert "failure_context" not in r["data"]

    def test_reopen_clears_stale_integration_report(self, git_repo):
        repo, data = _setup_developing(git_repo)
        with _mock_repo(repo):
            _write_and_commit(data["worktree"])
            tools.cmd_test(make_args(repo=repo.name, feature=1))
            tools.cmd_done(make_args(repo=repo.name, feature=1))

        with mock.patch.object(tools, "_repo_path", return_value=repo), \
             mock.patch.object(tools, "_run_tests", return_value={"ok": False, "output": "FAIL: assertion error"}):
            tools.cmd_integrate(make_args(repo=repo.name))

        report_path = repo / tools.CM_DIR / tools.EVIDENCE_DIR / "integration-report.json"
        assert report_path.exists()

        with _mock_repo(repo):
            r = tools.cmd_reopen(make_args(repo=repo.name, feature=1))
            assert r["ok"]

        assert not report_path.exists()


# ══════════════════════════════════════════════════════════
#  Phase 3: cm start
# ══════════════════════════════════════════════════════════


class TestCmStart:
    def test_start_creates_session(self, git_repo, tmp_path):
        plan_file = tmp_path / "plan.md"
        plan_file.write_text(SINGLE_FEATURE_PLAN)

        with _mock_repo(git_repo):
            r = tools.cmd_start(make_args(repo=git_repo.name, plan_file=str(plan_file)))
            assert r["ok"]
            assert r["data"]["rolled_back"] is False

        lock = tools._atomic_json_read(git_repo / tools.CM_DIR / "lock.json")
        assert lock["session_phase"] == "reviewed"
        plan_path = git_repo / tools.CM_DIR / "PLAN.md"
        assert plan_path.exists()
        assert "foo" in plan_path.read_text().lower()

    def test_start_validates_plan(self, git_repo, tmp_path):
        plan_file = tmp_path / "bad_plan.md"
        plan_file.write_text("# Not a valid plan\nNo features here.")

        with _mock_repo(git_repo):
            r = tools.cmd_start(make_args(repo=git_repo.name, plan_file=str(plan_file)))
            assert not r["ok"]
            assert r["data"]["rolled_back"] is True

        # Lock should be released
        lock = tools._atomic_json_read(git_repo / tools.CM_DIR / "lock.json")
        assert lock == {}

    def test_start_missing_plan_file(self, git_repo):
        with _mock_repo(git_repo):
            r = tools.cmd_start(make_args(repo=git_repo.name, plan_file="/nonexistent/plan.md"))
            assert not r["ok"]
            assert r["data"]["rolled_back"] is True

    def test_start_without_plan_file(self, git_repo):
        """Start without --plan-file just locks."""
        with _mock_repo(git_repo):
            r = tools.cmd_start(make_args(repo=git_repo.name))
            assert r["ok"]
            assert r["data"]["session_phase"] == "locked"


# ══════════════════════════════════════════════════════════
#  Phase 3: Extended submit and status
# ══════════════════════════════════════════════════════════


class TestExtendedSubmit:
    def test_submit_returns_contract(self, git_repo):
        repo, data = _setup_developing(git_repo)
        with _mock_repo(repo):
            _write_and_commit(data["worktree"])
            tools.cmd_test(make_args(repo=repo.name, feature=1))
            tools.cmd_done(make_args(repo=repo.name, feature=1))
            tools.cmd_integrate(make_args(repo=repo.name))

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0, stdout="", stderr="")
            with mock.patch.object(tools, "_repo_path", return_value=repo):
                r = tools.cmd_submit(make_args(repo=repo.name, title="test: add foo"))
                assert r["ok"]
                assert "evidence_dir" in r["data"]
                assert r["data"]["exit_status"] == "success"
                assert r["data"]["features_completed"] == 1
                assert r["data"]["features_total"] == 1
                assert "journal" in r["data"]


class TestExtendedStatus:
    def test_status_partial(self, git_repo):
        repo, data = _setup_developing(git_repo)
        with _mock_repo(repo):
            r = tools.cmd_status(make_args(repo=repo.name))
            assert r["ok"]
            d = r["data"]
            assert d["exit_status"] == "partial"
            assert d["features_total"] == 1
            assert d["features_completed"] == 0

    def test_status_blocking_reason_priority(self, git_repo):
        """Expired lease takes priority over other blocking reasons."""
        repo, data = _setup_developing(git_repo)
        # Expire lease
        lock_path = repo / tools.CM_DIR / "lock.json"
        lock = json.loads(lock_path.read_text())
        lock["lease_expires_at"] = "2020-01-01T00:00:00+00:00"
        lock_path.write_text(json.dumps(lock))

        with _mock_repo(repo):
            r = tools.cmd_status(make_args(repo=repo.name))
            assert r["ok"]
            assert "expired" in r["data"].get("blocking_reason", "").lower()

    def test_status_no_blocking_when_ready(self, git_repo):
        repo, data = _setup_developing(git_repo)
        with _mock_repo(repo):
            _write_and_commit(data["worktree"])
            tools.cmd_test(make_args(repo=repo.name, feature=1))
            tools.cmd_done(make_args(repo=repo.name, feature=1))
            r = tools.cmd_status(make_args(repo=repo.name))
            assert r["ok"]
            assert r["data"]["exit_status"] == "ready"
            assert "blocking_reason" not in r["data"]

    def test_status_reports_stale_verification(self, git_repo):
        repo, data = _setup_developing(git_repo)
        with _mock_repo(repo):
            _write_and_commit(data["worktree"])
            tools.cmd_test(make_args(repo=repo.name, feature=1))
            _write_and_commit(data["worktree"], "bar.py", "x = 1\n")
            r = tools.cmd_status(make_args(repo=repo.name))
            assert r["ok"]
            assert "stale" in r["data"].get("blocking_reason", "").lower()
            assert "cm test --feature 1" in r["data"].get("resume_hint", "")
