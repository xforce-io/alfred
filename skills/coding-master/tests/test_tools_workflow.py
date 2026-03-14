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
            tools.cmd_test(make_args(repo=repo.name, feature=1))

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
    def test_auto_renews_expired_lease(self, git_repo):
        repo, data = _setup_developing(git_repo)
        # Expire the lease
        lock_path = repo / tools.CM_DIR / "lock.json"
        lock = json.loads(lock_path.read_text())
        lock["lease_expires_at"] = "2020-01-01T00:00:00+00:00"
        lock_path.write_text(json.dumps(lock))

        with mock.patch.object(tools, "_repo_path", return_value=repo):
            _write_and_commit(data["worktree"])
            r = tools.cmd_test(make_args(repo=repo.name, feature=1))
            # Expired lease is auto-renewed, command proceeds
            assert r["ok"]
            # Verify lease was renewed
            renewed_lock = json.loads(lock_path.read_text())
            assert renewed_lock["lease_expires_at"] > "2020-01-01T00:00:00+00:00"

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


# ══════════════════════════════════════════════════════════
#  Mode gate & session_phase enforcement (P0 + P1)
# ══════════════════════════════════════════════════════════


def _setup_locked_with_mode(git_repo, mode="deliver"):
    """Lock workspace in a specific mode."""
    with _mock_repo(git_repo):
        r = tools.cmd_lock(make_args(repo=git_repo.name, mode=mode))
        assert r["ok"], r
    return git_repo


class TestModeGateAndSessionPhase:
    """P0: Mode gate enforcement via _precondition_check.
    P1: session_phase validation for mutation commands."""

    # ── P0: Mode gate rejects commands not in allowed_commands ──

    def test_mode_gate_rejects_dev_in_review_mode(self, git_repo):
        _setup_locked_with_mode(git_repo, mode="review")
        with _mock_repo(git_repo):
            r = tools.cmd_dev(make_args(repo=git_repo.name, feature=1))
        assert r["ok"] is False
        assert "not available in 'review' mode" in r["error"]

    def test_mode_gate_rejects_test_in_analyze_mode(self, git_repo):
        _setup_locked_with_mode(git_repo, mode="analyze")
        with _mock_repo(git_repo):
            r = tools.cmd_test(make_args(repo=git_repo.name, feature=1))
        assert r["ok"] is False
        assert "not available in 'analyze' mode" in r["error"]

    def test_mode_gate_rejects_done_in_review_mode(self, git_repo):
        _setup_locked_with_mode(git_repo, mode="review")
        with _mock_repo(git_repo):
            r = tools.cmd_done(make_args(repo=git_repo.name, feature=1))
        assert r["ok"] is False
        assert "not available in 'review' mode" in r["error"]

    def test_mode_gate_rejects_claim_in_analyze_mode(self, git_repo):
        _setup_locked_with_mode(git_repo, mode="analyze")
        with _mock_repo(git_repo):
            r = tools.cmd_claim(make_args(repo=git_repo.name, feature=1))
        assert r["ok"] is False
        assert "not available in 'analyze' mode" in r["error"]

    def test_mode_gate_allows_dev_in_debug_mode(self, git_repo):
        """debug mode allows dev/test — should NOT get mode gate error."""
        _setup_locked_with_mode(git_repo, mode="debug")
        with _mock_repo(git_repo):
            r = tools.cmd_dev(make_args(repo=git_repo.name, feature=1))
        # Will fail for other reasons (session_phase, no feature claimed),
        # but NOT for mode gate
        assert "not available" not in r.get("error", "")

    # ── P1: session_phase gate for mutation commands ──

    def test_session_phase_rejects_dev_when_locked(self, git_repo):
        """cmd_dev rejected when session_phase is 'locked'."""
        _setup_locked(git_repo)  # session_phase = "locked"
        with _mock_repo(git_repo):
            r = tools.cmd_dev(make_args(repo=git_repo.name, feature=1))
        assert r["ok"] is False
        assert "session" in r["error"].lower()
        assert "working" in r["error"].lower() or "claimed" in r["error"].lower()

    def test_session_phase_rejects_test_when_reviewed(self, git_repo):
        """cmd_test rejected when session_phase is 'reviewed' (no feature claimed)."""
        _setup_reviewed(git_repo)  # session_phase = "reviewed"
        with _mock_repo(git_repo):
            r = tools.cmd_test(make_args(repo=git_repo.name, feature=1))
        assert r["ok"] is False
        assert "session" in r["error"].lower()

    def test_session_phase_rejects_done_when_locked(self, git_repo):
        """cmd_done rejected when session_phase is 'locked'."""
        _setup_locked(git_repo)
        with _mock_repo(git_repo):
            r = tools.cmd_done(make_args(repo=git_repo.name, feature=1))
        assert r["ok"] is False
        assert "session" in r["error"].lower()

    def test_session_phase_allows_claim_when_reviewed(self, git_repo):
        """Regression: cmd_claim should work in 'reviewed' phase."""
        repo = _setup_reviewed(git_repo)
        with _mock_repo(repo):
            r = tools.cmd_claim(make_args(repo=repo.name, feature=1))
        assert r["ok"] is True

    def test_session_phase_allows_dev_when_working(self, git_repo):
        """Regression: cmd_dev works after claim (session_phase='working')."""
        repo, _ = _setup_claimed(git_repo)
        _fill_analysis_plan(repo, 1)
        with _mock_repo(repo):
            r = tools.cmd_dev(make_args(repo=repo.name, feature=1))
        assert r["ok"] is True


# ══════════════════════════════════════════════════════════
#  File operations (v4.5)
# ══════════════════════════════════════════════════════════


class TestCmRead:
    """cmd_read: read file contents with optional line range."""

    def test_read_existing_file(self, git_repo):
        repo = _setup_locked(git_repo)
        with _mock_repo(repo):
            r = tools.cmd_read(make_args(repo=repo.name, file="README.md"))
        assert r["ok"] is True
        assert "# Test" in r["data"]["content"]
        assert r["data"]["total_lines"] == 1

    def test_read_with_line_range(self, git_repo):
        repo = _setup_locked(git_repo)
        # Write to repo (session worktree may exist, use absolute path)
        (repo / "multi.txt").write_text("line1\nline2\nline3\nline4\nline5\n")
        with _mock_repo(repo):
            r = tools.cmd_read(make_args(repo=repo.name,
                                         file=str(repo / "multi.txt"),
                                         start_line=2, end_line=4))
        assert r["ok"] is True
        assert r["data"]["start_line"] == 2
        assert r["data"]["end_line"] == 4
        assert "line2" in r["data"]["content"]
        assert "line1" not in r["data"]["content"]

    def test_read_nonexistent_file(self, git_repo):
        repo = _setup_locked(git_repo)
        with _mock_repo(repo):
            r = tools.cmd_read(make_args(repo=repo.name, file="nope.txt"))
        assert r["ok"] is False
        assert "not found" in r["error"]

    def test_read_outside_repo(self, git_repo):
        repo = _setup_locked(git_repo)
        with _mock_repo(repo):
            r = tools.cmd_read(make_args(repo=repo.name, file="/etc/passwd"))
        assert r["ok"] is False
        assert "outside repo" in r["error"]

    def test_read_in_review_mode(self, git_repo):
        """read should be allowed in review mode."""
        repo = _setup_locked_with_mode(git_repo, mode="review")
        with _mock_repo(repo):
            r = tools.cmd_read(make_args(repo=repo.name, file="README.md"))
        assert r["ok"] is True


class TestCmFind:
    """cmd_find: find files by glob pattern."""

    def test_find_python_files(self, git_repo):
        repo = _setup_locked(git_repo)
        # Find the session worktree and create files there
        lock = tools._atomic_json_read(repo / tools.CM_DIR / "lock.json")
        wt = Path(lock.get("session_worktree", str(repo)))
        (wt / "src").mkdir()
        (wt / "src" / "foo.py").write_text("pass")
        (wt / "src" / "bar.py").write_text("pass")
        with _mock_repo(repo):
            r = tools.cmd_find(make_args(repo=repo.name, pattern="src/*.py"))
        assert r["ok"] is True
        assert r["data"]["count"] == 2
        assert "src/foo.py" in r["data"]["files"]

    def test_find_truncation(self, git_repo):
        repo = _setup_locked(git_repo)
        lock = tools._atomic_json_read(repo / tools.CM_DIR / "lock.json")
        wt = Path(lock.get("session_worktree", str(repo)))
        for i in range(5):
            (wt / f"f{i}.txt").write_text("x")
        with _mock_repo(repo):
            r = tools.cmd_find(make_args(repo=repo.name, pattern="*.txt",
                                         max_results=3))
        assert r["ok"] is True
        assert r["data"]["count"] == 3
        assert r["data"]["truncated"] is True


class TestCmGrep:
    """cmd_grep: search file contents."""

    def test_grep_finds_pattern(self, git_repo):
        repo = _setup_locked(git_repo)
        lock = tools._atomic_json_read(repo / tools.CM_DIR / "lock.json")
        wt = Path(lock.get("session_worktree", str(repo)))
        (wt / "code.py").write_text("def hello():\n    return 42\n")
        with _mock_repo(repo):
            r = tools.cmd_grep(make_args(repo=repo.name, pattern="return 42"))
        assert r["ok"] is True
        assert "return 42" in r["data"]["output"]


class TestCmEdit:
    """cmd_edit: precise text replacement."""

    def test_edit_in_deliver_mode(self, git_repo):
        repo, claim_data = _setup_developing(git_repo)
        wt = Path(claim_data["worktree"])
        (wt / "target.py").write_text("old_value = 1\n")
        with _mock_repo(repo):
            r = tools.cmd_edit(make_args(
                repo=repo.name, file="target.py",
                old_text="old_value = 1", new_text="new_value = 2",
                feature=1,
            ))
        assert r["ok"] is True
        assert (wt / "target.py").read_text() == "new_value = 2\n"

    def test_edit_rejected_without_developing_feature(self, git_repo):
        repo = _setup_locked(git_repo)
        lock = tools._atomic_json_read(repo / tools.CM_DIR / "lock.json")
        wt = Path(lock.get("session_worktree", str(repo)))
        (wt / "target.py").write_text("old_value = 1\n")
        with _mock_repo(repo):
            r = tools.cmd_edit(make_args(
                repo=repo.name, file="target.py",
                old_text="old_value = 1", new_text="new_value = 2",
            ))
        assert r["ok"] is False
        assert "developing" in r["error"]

    def test_edit_rejected_in_review_mode(self, git_repo):
        repo = _setup_locked_with_mode(git_repo, mode="review")
        (repo / "target.py").write_text("x = 1\n")
        with _mock_repo(repo):
            r = tools.cmd_edit(make_args(
                repo=repo.name, file="target.py",
                old_text="x = 1", new_text="x = 2",
            ))
        assert r["ok"] is False
        assert "read-only" in r["error"]

    def test_edit_non_unique_match(self, git_repo):
        repo, claim_data = _setup_developing(git_repo)
        wt = Path(claim_data["worktree"])
        (wt / "dup.py").write_text("x = 1\nx = 1\n")
        with _mock_repo(repo):
            r = tools.cmd_edit(make_args(
                repo=repo.name, file="dup.py",
                old_text="x = 1", new_text="x = 2",
                feature=1,
            ))
        assert r["ok"] is False
        assert "matches 2 locations" in r["error"]

    def test_edit_allowed_in_debug_mode(self, git_repo):
        repo = _setup_locked_with_mode(git_repo, mode="debug")
        lock = tools._atomic_json_read(repo / tools.CM_DIR / "lock.json")
        wt = Path(lock.get("session_worktree", str(repo)))
        (wt / "fix.py").write_text("bug = True\n")
        with _mock_repo(repo):
            r = tools.cmd_edit(make_args(
                repo=repo.name, file="fix.py",
                old_text="bug = True", new_text="bug = False",
            ))
        assert r["ok"] is True

    def test_edit_plan_md_in_locked_phase(self, git_repo):
        """PLAN.md can be created via edit in locked phase (no developing feature needed)."""
        repo = _setup_locked(git_repo)
        with _mock_repo(repo):
            r = tools.cmd_edit(make_args(
                repo=repo.name, file=f"{tools.CM_DIR}/PLAN.md",
                old_text="", new_text="# Feature Plan\n\n## Features\n\n### Feature 1: Fix bug\n",
            ))
        assert r["ok"] is True
        assert r["data"].get("created") is True
        plan_path = repo / tools.CM_DIR / "PLAN.md"
        assert plan_path.exists()
        assert "Feature 1" in plan_path.read_text()

    def test_edit_feature_md_in_locked_phase(self, git_repo):
        """features/*.md can be written in locked phase (CM metadata)."""
        repo = _setup_locked(git_repo)
        features_dir = repo / tools.CM_DIR / "features"
        features_dir.mkdir(parents=True, exist_ok=True)
        md_path = features_dir / "01-fix-bug.md"
        md_path.write_text("# Feature 1\n")
        with _mock_repo(repo):
            r = tools.cmd_edit(make_args(
                repo=repo.name, file=f"{tools.CM_DIR}/features/01-fix-bug.md",
                old_text="# Feature 1", new_text="# Feature 1: Fix bug\n\n## Analysis\nDone.",
            ))
        assert r["ok"] is True

    def test_edit_overwrite_existing_plan_md(self, git_repo):
        """old_text='' on existing PLAN.md overwrites it (stale state from previous session)."""
        repo = _setup_locked(git_repo)
        plan_path = repo / tools.CM_DIR / "PLAN.md"
        plan_path.write_text("# Old stale plan from previous session\n")
        with _mock_repo(repo):
            r = tools.cmd_edit(make_args(
                repo=repo.name, file=f"{tools.CM_DIR}/PLAN.md",
                old_text="", new_text="# New Plan\n\n## Features\n\n### Feature 1: Fix\n",
            ))
        assert r["ok"] is True
        assert "New Plan" in plan_path.read_text()
        assert "Old stale" not in plan_path.read_text()

    def test_edit_bare_plan_md_in_locked_phase(self, git_repo):
        """Bare 'PLAN.md' (without .coding-master/ prefix) auto-resolves to CM metadata."""
        repo = _setup_locked(git_repo)
        with _mock_repo(repo):
            r = tools.cmd_edit(make_args(
                repo=repo.name, file="PLAN.md",
                old_text="", new_text="# Feature Plan\n\n## Features\n\n### Feature 1: Fix bug\n",
            ))
        assert r["ok"] is True
        assert r["data"].get("created") is True
        plan_path = repo / tools.CM_DIR / "PLAN.md"
        assert plan_path.exists()

    def test_edit_absolute_worktree_plan_md_in_locked_phase(self, git_repo):
        """Absolute path to worktree PLAN.md auto-resolves to .coding-master/PLAN.md."""
        repo = _setup_locked(git_repo)
        lock = tools._atomic_json_read(repo / tools.CM_DIR / "lock.json")
        wt = Path(lock.get("session_worktree", str(repo)))
        # Agent passes absolute worktree path like /path/alfred-session/PLAN.md
        abs_path = str(wt / "PLAN.md")
        with _mock_repo(repo):
            r = tools.cmd_edit(make_args(
                repo=repo.name, file=abs_path,
                old_text="", new_text="# Feature Plan\n\n## Features\n\n### Feature 1: Fix bug\n",
            ))
        assert r["ok"] is True
        assert r["data"].get("created") is True
        # Should have been created under .coding-master/, not worktree
        plan_path = repo / tools.CM_DIR / "PLAN.md"
        assert plan_path.exists()

    def test_edit_source_still_blocked_in_locked_phase(self, git_repo):
        """Source code edit is still blocked without developing feature."""
        repo = _setup_locked(git_repo)
        lock = tools._atomic_json_read(repo / tools.CM_DIR / "lock.json")
        wt = Path(lock.get("session_worktree", str(repo)))
        (wt / "target.py").write_text("old_value = 1\n")
        with _mock_repo(repo):
            r = tools.cmd_edit(make_args(
                repo=repo.name, file="target.py",
                old_text="old_value = 1", new_text="new_value = 2",
            ))
        assert r["ok"] is False
        assert "developing" in r["error"]

    def test_edit_nested_cm_dir_path_resolves_to_plan(self, git_repo):
        """Path like 'skills/coding-master/.coding-master/PLAN.md' should resolve to .coding-master/PLAN.md.

        Agents sometimes prefix .coding-master/ with the skill directory they were reading.
        """
        repo = _setup_locked(git_repo)
        with _mock_repo(repo):
            r = tools.cmd_edit(make_args(
                repo=repo.name,
                file="skills/coding-master/.coding-master/PLAN.md",
                old_text="",
                new_text="# Feature Plan\n\n## Features\n\n### Feature 1: Fix bug\n",
            ))
        assert r["ok"] is True, f"Expected ok, got: {r}"
        assert r["data"].get("created") is True
        plan_path = repo / tools.CM_DIR / "PLAN.md"
        assert plan_path.exists()

    def test_edit_error_hint_includes_example(self, git_repo):
        """Error hint for locked phase should include a concrete _cm_edit example."""
        repo = _setup_locked(git_repo)
        lock = tools._atomic_json_read(repo / tools.CM_DIR / "lock.json")
        wt = Path(lock.get("session_worktree", str(repo)))
        (wt / "target.py").write_text("x = 1\n")
        with _mock_repo(repo):
            r = tools.cmd_edit(make_args(
                repo=repo.name, file="target.py",
                old_text="x = 1", new_text="x = 2",
            ))
        assert r["ok"] is False
        # Error should include actionable guidance for the agent
        assert "next_action" in r
        assert "hint" in r


# ══════════════════════════════════════════════════════════
#  Lock branch consistency
# ══════════════════════════════════════════════════════════


class TestLockBranchConsistency:
    """cmd_lock: joined session should detect worktree branch mismatch."""

    def test_lock_join_detects_branch_mismatch(self, git_repo):
        """When joining a session, if worktree branch differs from lock, report it."""
        repo = _setup_locked(git_repo)
        lock = tools._atomic_json_read(repo / tools.CM_DIR / "lock.json")
        wt = Path(lock["session_worktree"])
        lock_branch = lock["branch"]

        # Simulate branch mismatch: checkout a different branch in the worktree
        subprocess.run(
            ["git", "checkout", "-b", "rogue-branch"],
            cwd=wt, capture_output=True, check=True,
        )

        # Join the session again
        with _mock_repo(repo):
            r = tools.cmd_lock(make_args(repo=repo.name))

        assert r["ok"] is True
        # Should warn about or fix the branch mismatch
        data = r.get("data", {})
        actual_branch_in_wt = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=wt, capture_output=True, text=True,
        ).stdout.strip()
        # After join, worktree should be on the lock branch, or result should warn
        assert (
            actual_branch_in_wt == lock_branch
            or data.get("branch_mismatch")
            or "mismatch" in str(data).lower()
        ), f"Worktree on '{actual_branch_in_wt}' but lock says '{lock_branch}', no warning/fix"


# ══════════════════════════════════════════════════════════
#  Plan layer cleanup on new session
# ══════════════════════════════════════════════════════════


class TestPlanLayerCleanup:
    """New session cleans stale plan-layer state."""

    def test_new_lock_cleans_stale_plan(self, git_repo):
        """Stale PLAN.md from previous session is removed on new lock."""
        repo = _setup_locked(git_repo)
        # Create stale plan-layer files as if from a previous session
        cm = repo / tools.CM_DIR
        (cm / "PLAN.md").write_text("# Old plan\n")
        (cm / "claims.json").write_text('{"features":{"1":{"phase":"done"}}}')
        features_dir = cm / "features"
        features_dir.mkdir(parents=True, exist_ok=True)
        (features_dir / "01-old.md").write_text("# Old feature\n")
        # Unlock current session
        with _mock_repo(repo):
            tools.cmd_unlock(make_args(repo=repo.name, force=True))
        # Lock again — should clean stale state
        with _mock_repo(repo):
            r = tools.cmd_lock(make_args(repo=repo.name, mode="deliver"))
        assert r["ok"] is True
        assert not (cm / "PLAN.md").exists()
        assert not (cm / "claims.json").exists()
        assert not features_dir.exists()

    def test_new_lock_preserves_session_json(self, git_repo):
        """session.json survives across sessions (cross-session by design)."""
        repo = _setup_locked(git_repo)
        cm = repo / tools.CM_DIR
        session_path = cm / "session.json"
        assert session_path.exists()
        old_content = session_path.read_text()
        with _mock_repo(repo):
            tools.cmd_unlock(make_args(repo=repo.name, force=True))
        with _mock_repo(repo):
            tools.cmd_lock(make_args(repo=repo.name, mode="deliver"))
        assert session_path.exists()


# ══════════════════════════════════════════════════════════
#  Lock upgrade (v4.5)
# ══════════════════════════════════════════════════════════


class TestLockUpgrade:
    """cm lock upgrade: review/analyze → debug."""

    def test_review_to_debug_upgrade(self, git_repo):
        repo = _setup_locked_with_mode(git_repo, mode="review")
        with _mock_repo(repo):
            r = tools.cmd_lock(make_args(repo=repo.name, mode="debug"))
        assert r["ok"] is True
        assert r["data"].get("upgraded") is True
        assert r["data"]["mode"] == "debug"
        # Verify lock.json was updated
        lock = tools._atomic_json_read(repo / tools.CM_DIR / "lock.json")
        assert lock["mode"] == "debug"
        assert lock["read_only"] is False

    def test_analyze_to_debug_upgrade(self, git_repo):
        repo = _setup_locked_with_mode(git_repo, mode="analyze")
        with _mock_repo(repo):
            r = tools.cmd_lock(make_args(repo=repo.name, mode="debug"))
        assert r["ok"] is True
        assert r["data"].get("upgraded") is True

    def test_review_to_deliver_rejected(self, git_repo):
        repo = _setup_locked_with_mode(git_repo, mode="review")
        with _mock_repo(repo):
            r = tools.cmd_lock(make_args(repo=repo.name, mode="deliver"))
        assert r["ok"] is False
        assert "cannot upgrade" in r["error"]

    def test_debug_to_debug_idempotent(self, git_repo):
        repo = _setup_locked_with_mode(git_repo, mode="debug")
        with _mock_repo(repo):
            r = tools.cmd_lock(make_args(repo=repo.name, mode="debug"))
        # Should join (idempotent), not fail
        assert r["ok"] is True


# ══════════════════════════════════════════════════════════
#  Report auto-unlock fix (v4.5)
# ══════════════════════════════════════════════════════════


class TestReportDebugNoAutoUnlock:
    """cmd_report in debug mode should NOT auto-unlock."""

    def test_debug_report_keeps_session(self, git_repo):
        repo = _setup_locked_with_mode(git_repo, mode="debug")
        # Create scope.json (required artifact)
        scope_path = repo / tools.CM_DIR / "scope.json"
        scope_path.parent.mkdir(parents=True, exist_ok=True)
        scope_path.write_text('{"type": "repo"}')
        with _mock_repo(repo):
            r = tools.cmd_report(make_args(
                repo=repo.name, content="# Diagnosis\nFound a bug.",
            ))
        assert r["ok"] is True
        assert r["data"]["auto_unlocked"] is False
        # Session should still be active
        lock = tools._atomic_json_read(repo / tools.CM_DIR / "lock.json")
        assert lock.get("session_phase") is not None
        assert lock.get("mode") == "debug"

    def test_review_report_auto_unlocks(self, git_repo):
        repo = _setup_locked_with_mode(git_repo, mode="review")
        with _mock_repo(repo):
            r = tools.cmd_report(make_args(
                repo=repo.name, content="# Review\nAll good.",
            ))
        assert r["ok"] is True
        assert r["data"]["auto_unlocked"] is True


class TestChangeSummaryDiffUrl:
    def test_diff_url_resolves_local_base_ref_to_commit_sha(self, git_repo):
        subprocess.run(
            ["git", "remote", "add", "origin", "git@github.com:org/test-repo.git"],
            cwd=git_repo,
            capture_output=True,
            check=True,
        )
        (git_repo / "README.md").write_text("# Test\nupdated\n")
        subprocess.run(["git", "add", "README.md"], cwd=git_repo, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "update readme"], cwd=git_repo, capture_output=True, check=True)

        base_sha = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD~1"],
            cwd=git_repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        head_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=git_repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        summary = tools._build_change_summary(git_repo, "HEAD~1")

        assert summary["diff_url"] == (
            f"https://github.com/org/test-repo/compare/{base_sha}...{head_sha}"
        )
