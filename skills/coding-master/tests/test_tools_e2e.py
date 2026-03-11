#!/usr/bin/env python3
"""E2E, idempotency, doctor, and concurrency tests for coding-master v3 tools.py."""

from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import tools


# ══════════════════════════════════════════════════════════
#  Helpers
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

MULTI_FEATURE_PLAN = """\
# Feature Plan

## Origin Task
Refactor module with dependencies

## Features

### Feature 1: Base interface
**Depends on**: —

#### Task
Create base interface.

#### Acceptance Criteria
- [ ] Interface exists
- [ ] Tests pass

---

### Feature 2: Implementation A
**Depends on**: Feature 1

#### Task
Implement variant A.

#### Acceptance Criteria
- [ ] Implementation works

---

### Feature 3: Implementation B
**Depends on**: Feature 1

#### Task
Implement variant B.

#### Acceptance Criteria
- [ ] Implementation works

---

### Feature 4: Integration tests
**Depends on**: Feature 2, Feature 3

#### Task
Add integration tests for A + B.

#### Acceptance Criteria
- [ ] 3 test scenarios
"""

THREE_INDEPENDENT_PLAN = """\
# Feature Plan

## Origin Task
Three independent tasks

## Features

### Feature 1: Task A
**Depends on**: —

#### Task
Do A

#### Acceptance Criteria
- [ ] A works

---

### Feature 2: Task B
**Depends on**: —

#### Task
Do B

#### Acceptance Criteria
- [ ] B works

---

### Feature 3: Task C
**Depends on**: —

#### Task
Do C

#### Acceptance Criteria
- [ ] C works
"""


def make_args(**kwargs):
    """Create an args namespace with sensible defaults."""
    import argparse
    defaults = {
        "repo": None, "agent": "test-agent", "branch": None,
        "feature": None, "title": None, "message": None, "fix": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


@pytest.fixture
def git_repo(tmp_path):
    """Create a test git repo with initial commit."""
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
    """Return a context manager that patches _repo_path and _run_tests."""
    return mock.patch.multiple(
        tools,
        _repo_path=mock.MagicMock(return_value=git_repo),
        _run_tests=mock.MagicMock(return_value={"ok": True, "output": "3 passed, 0 failed"}),
    )


def _setup_locked(git_repo):
    """Lock the repo, return repo path."""
    with _mock_repo(git_repo):
        r = tools.cmd_lock(make_args(repo=git_repo.name, branch=None))
        assert r["ok"], r
    return git_repo


def _setup_reviewed(git_repo, plan_text=SINGLE_FEATURE_PLAN):
    """Lock + write PLAN.md + plan-ready."""
    repo = _setup_locked(git_repo)
    plan_path = repo / tools.CM_DIR / "PLAN.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(plan_text)
    with _mock_repo(repo):
        r = tools.cmd_plan_ready(make_args(repo=repo.name))
        assert r["ok"], r
    return repo


def _setup_claimed(git_repo, feature_id=1, plan_text=SINGLE_FEATURE_PLAN):
    """Lock + plan-ready + claim feature."""
    repo = _setup_reviewed(git_repo, plan_text)
    with _mock_repo(repo):
        r = tools.cmd_claim(make_args(repo=repo.name, feature=feature_id))
        assert r["ok"], r
    return repo, r["data"]


def _fill_analysis_plan(repo, feature_id):
    """Write Analysis + Plan into the feature MD."""
    feature_md = tools._find_feature_md(repo, str(feature_id))
    assert feature_md is not None, f"No feature MD found for {feature_id}"
    content = feature_md.read_text()
    content = content.replace("## Analysis\n", "## Analysis\n- Analyzed the code\n")
    content = content.replace("## Plan\n", "## Plan\n1. Implement it\n")
    feature_md.write_text(content)
    return feature_md


def _setup_developing(git_repo, feature_id=1, plan_text=SINGLE_FEATURE_PLAN):
    """Lock + plan-ready + claim + fill analysis + dev."""
    repo, claim_data = _setup_claimed(git_repo, feature_id, plan_text)
    _fill_analysis_plan(repo, feature_id)
    with _mock_repo(repo):
        r = tools.cmd_dev(make_args(repo=repo.name, feature=feature_id))
        assert r["ok"], r
    return repo, claim_data


def _write_and_commit(worktree_path, filename="foo.py", content="def foo(): return 42\n"):
    """Write a file and commit it in a worktree."""
    wt = Path(worktree_path)
    (wt / filename).write_text(content)
    subprocess.run(["git", "add", filename], cwd=wt, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", f"add {filename}"], cwd=wt, capture_output=True, check=True)


# ══════════════════════════════════════════════════════════
#  E2E: Single agent, single feature, full flow
# ══════════════════════════════════════════════════════════


class TestE2ESingleFeature:
    """lock → plan → plan-ready → claim → dev → test → done → integrate → submit"""

    def test_full_flow(self, git_repo):
        repo = git_repo

        with _mock_repo(repo):
            # 1. Lock
            r = tools.cmd_lock(make_args(repo=repo.name, branch="dev/test-branch"))
            assert r["ok"]
            assert r["data"]["branch"] == "dev/test-branch"
            lock = tools._atomic_json_read(repo / tools.CM_DIR / "lock.json")
            assert lock["session_phase"] == "locked"

            # 2. Create PLAN.md
            plan_path = repo / tools.CM_DIR / "PLAN.md"
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.write_text(SINGLE_FEATURE_PLAN)

            # 3. Plan-ready
            r = tools.cmd_plan_ready(make_args(repo=repo.name))
            assert r["ok"]
            lock = tools._atomic_json_read(repo / tools.CM_DIR / "lock.json")
            assert lock["session_phase"] == "reviewed"

            # 4. Claim
            r = tools.cmd_claim(make_args(repo=repo.name, feature=1))
            assert r["ok"]
            wt = r["data"]["worktree"]
            feature_md_path = r["data"]["feature_md"]
            assert Path(wt).exists()
            assert Path(feature_md_path).exists()
            lock = tools._atomic_json_read(repo / tools.CM_DIR / "lock.json")
            assert lock["session_phase"] == "working"
            claims = tools._atomic_json_read(repo / tools.CM_DIR / "claims.json")
            assert claims["features"]["1"]["phase"] == "analyzing"

            # 5. Write Analysis + Plan → dev
            _fill_analysis_plan(repo, 1)
            r = tools.cmd_dev(make_args(repo=repo.name, feature=1))
            assert r["ok"]
            claims = tools._atomic_json_read(repo / tools.CM_DIR / "claims.json")
            assert claims["features"]["1"]["phase"] == "developing"

            # 6. Code + commit
            _write_and_commit(wt)

            # 7. Test
            r = tools.cmd_test(make_args(repo=repo.name, feature=1))
            assert r["ok"]
            assert r["data"]["test_passed"]
            claims = tools._atomic_json_read(repo / tools.CM_DIR / "claims.json")
            assert claims["features"]["1"]["developing"]["test_status"] == "passed"

            # 8. Done
            r = tools.cmd_done(make_args(repo=repo.name, feature=1))
            assert r["ok"]
            assert r["data"]["all_done"]
            claims = tools._atomic_json_read(repo / tools.CM_DIR / "claims.json")
            assert claims["features"]["1"]["phase"] == "done"

            # 9. Progress
            r = tools.cmd_progress(make_args(repo=repo.name))
            assert r["ok"]
            assert r["data"]["done"] == 1
            assert "integrate" in str(r["data"]["suggestions"]).lower()

            # 10. Integrate
            r = tools.cmd_integrate(make_args(repo=repo.name))
            assert r["ok"]
            lock = tools._atomic_json_read(repo / tools.CM_DIR / "lock.json")
            assert lock["session_phase"] == "integrating"

            # 11. Submit (mock gh)
            with mock.patch("subprocess.run") as mock_run:
                # Mock git and gh commands
                mock_run.return_value = mock.MagicMock(
                    returncode=0, stdout="", stderr=""
                )
                r = tools.cmd_submit(make_args(repo=repo.name, title="test: add foo"))
                assert r["ok"]

            # Verify JOURNAL.md has key events
            journal = (repo / tools.CM_DIR / "JOURNAL.md").read_text()
            assert "lock" in journal
            assert "plan-ready" in journal
            assert "claim" in journal
            assert "done" in journal
            assert "integrate" in journal
            assert "submit" in journal


# ══════════════════════════════════════════════════════════
#  E2E: Multi-feature with dependencies
# ══════════════════════════════════════════════════════════


class TestE2EMultiFeature:
    """Test dependency chain: 1 → {2, 3} → 4"""

    def test_dependency_blocking(self, git_repo):
        """Features with unmet deps cannot be claimed."""
        repo = _setup_reviewed(git_repo, MULTI_FEATURE_PLAN)
        with _mock_repo(repo):
            # Feature 1 (no deps) → OK
            r = tools.cmd_claim(make_args(repo=repo.name, feature=1))
            assert r["ok"]

            # Feature 2 (depends on 1) → blocked
            r = tools.cmd_claim(make_args(repo=repo.name, feature=2))
            assert not r["ok"]
            assert "blocked" in r["error"]

            # Feature 4 (depends on 2,3) → blocked
            r = tools.cmd_claim(make_args(repo=repo.name, feature=4))
            assert not r["ok"]
            assert "blocked" in r["error"]

    def test_unblock_on_done(self, git_repo):
        """done(1) unblocks 2 and 3; done(2)+done(3) unblocks 4."""
        repo, data1 = _setup_developing(git_repo, 1, MULTI_FEATURE_PLAN)
        with _mock_repo(repo):
            # Commit in worktree 1
            _write_and_commit(data1["worktree"], "base.py", "class Base: pass\n")
            tools.cmd_test(make_args(repo=repo.name, feature=1))

            # Done feature 1 → should unblock 2, 3
            r = tools.cmd_done(make_args(repo=repo.name, feature=1))
            assert r["ok"]
            unblocked_ids = {u["id"] for u in r["data"]["unblocked"]}
            assert "2" in unblocked_ids
            assert "3" in unblocked_ids
            assert "4" not in unblocked_ids  # still blocked by 2 and 3

            # Now claim feature 2 → should work
            r = tools.cmd_claim(make_args(repo=repo.name, feature=2, agent="agent-a"))
            assert r["ok"]

            # Claim feature 3 → should work
            r = tools.cmd_claim(make_args(repo=repo.name, feature=3, agent="agent-b"))
            assert r["ok"]

            # Feature 4 still blocked
            r = tools.cmd_claim(make_args(repo=repo.name, feature=4))
            assert not r["ok"]
            assert "blocked" in r["error"]

    def test_progress_shows_all_states(self, git_repo):
        """Progress shows correct mix of phases."""
        repo, data1 = _setup_developing(git_repo, 1, MULTI_FEATURE_PLAN)
        with _mock_repo(repo):
            r = tools.cmd_progress(make_args(repo=repo.name))
            assert r["ok"]
            d = r["data"]
            assert d["developing"] == 1  # feature 1
            # Feature 2, 3 blocked by 1; feature 4 blocked by 2,3
            assert d["blocked"] == 3


# ══════════════════════════════════════════════════════════
#  Idempotency tests
# ══════════════════════════════════════════════════════════


class TestIdempotency:
    def test_lock_twice_joins_session(self, git_repo):
        repo = _setup_locked(git_repo)
        lock = json.loads((repo / tools.CM_DIR / "lock.json").read_text())
        original_branch = lock["branch"]
        with _mock_repo(repo):
            r = tools.cmd_lock(make_args(repo=repo.name, branch=None))
            assert r["ok"]
            # Joins existing session, reuses same branch
            assert r["data"]["branch"] == original_branch

    def test_claim_twice_returns_error(self, git_repo):
        repo, _ = _setup_claimed(git_repo)
        with _mock_repo(repo):
            r = tools.cmd_claim(make_args(repo=repo.name, feature=1))
            assert not r["ok"]
            assert "already" in r["error"]

    def test_plan_ready_idempotent(self, git_repo):
        repo = _setup_reviewed(git_repo)
        with _mock_repo(repo):
            r = tools.cmd_plan_ready(make_args(repo=repo.name))
            assert r["ok"]

    def test_done_twice_returns_error(self, git_repo):
        repo, data = _setup_developing(git_repo)
        with _mock_repo(repo):
            _write_and_commit(data["worktree"])
            tools.cmd_test(make_args(repo=repo.name, feature=1))
            r1 = tools.cmd_done(make_args(repo=repo.name, feature=1))
            assert r1["ok"]
            r2 = tools.cmd_done(make_args(repo=repo.name, feature=1))
            assert not r2["ok"]
            assert "already done" in r2["error"]

    def test_done_without_test_rejected(self, git_repo):
        repo, _ = _setup_developing(git_repo)
        with _mock_repo(repo):
            r = tools.cmd_done(make_args(repo=repo.name, feature=1))
            assert not r["ok"]
            assert "test" in r["error"].lower()

    def test_claim_before_plan_ready_rejected(self, git_repo):
        repo = _setup_locked(git_repo)
        plan_path = repo / tools.CM_DIR / "PLAN.md"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(SINGLE_FEATURE_PLAN)
        with _mock_repo(repo):
            r = tools.cmd_claim(make_args(repo=repo.name, feature=1))
            assert not r["ok"]
            assert "plan-ready" in r["error"]

    def test_reopen_not_done_rejected(self, git_repo):
        repo, _ = _setup_developing(git_repo)
        with _mock_repo(repo):
            r = tools.cmd_reopen(make_args(repo=repo.name, feature=1))
            assert not r["ok"]
            assert "expected done" in r["error"]

    def test_test_is_idempotent(self, git_repo):
        """Running test twice updates state consistently."""
        repo, data = _setup_developing(git_repo)
        with _mock_repo(repo):
            _write_and_commit(data["worktree"])
            r1 = tools.cmd_test(make_args(repo=repo.name, feature=1))
            assert r1["ok"]
            r2 = tools.cmd_test(make_args(repo=repo.name, feature=1))
            assert r2["ok"]
            assert r1["data"]["test_commit"] == r2["data"]["test_commit"]

    def test_test_allows_untracked_files(self, git_repo):
        """Untracked files should NOT block cm test."""
        repo, data = _setup_developing(git_repo)
        with _mock_repo(repo):
            _write_and_commit(data["worktree"], "foo.py", "def foo(): pass\n")
            # Add untracked file (not git-added)
            (Path(data["worktree"]) / "scratch.txt").write_text("notes")
            r = tools.cmd_test(make_args(repo=repo.name, feature=1))
            assert r["ok"]

    def test_test_rejects_modified_tracked(self, git_repo):
        """Modified tracked files SHOULD block cm test."""
        repo, data = _setup_developing(git_repo)
        with _mock_repo(repo):
            _write_and_commit(data["worktree"], "foo.py", "def foo(): pass\n")
            # Modify tracked file without committing
            (Path(data["worktree"]) / "foo.py").write_text("def foo(): return 99\n")
            r = tools.cmd_test(make_args(repo=repo.name, feature=1))
            assert not r["ok"]
            assert "uncommitted" in r["error"]


# ══════════════════════════════════════════════════════════
#  cmd_done: HEAD check (critical fix from review)
# ══════════════════════════════════════════════════════════


class TestDoneHeadCheck:
    def test_done_after_new_commit_rejected(self, git_repo):
        """If code changes after cm test, cm done should reject."""
        repo, data = _setup_developing(git_repo)
        with _mock_repo(repo):
            _write_and_commit(data["worktree"], "foo.py", "def foo(): return 42\n")
            tools.cmd_test(make_args(repo=repo.name, feature=1))

            # New commit after test
            _write_and_commit(data["worktree"], "bar.py", "def bar(): return 99\n")

            r = tools.cmd_done(make_args(repo=repo.name, feature=1))
            assert not r["ok"]
            assert "code changed" in r["error"]

    def test_done_after_retest_succeeds(self, git_repo):
        """After re-running test on new code, done should succeed."""
        repo, data = _setup_developing(git_repo)
        with _mock_repo(repo):
            _write_and_commit(data["worktree"], "foo.py", "def foo(): return 42\n")
            tools.cmd_test(make_args(repo=repo.name, feature=1))

            # New commit
            _write_and_commit(data["worktree"], "bar.py", "def bar(): return 99\n")

            # Re-test
            tools.cmd_test(make_args(repo=repo.name, feature=1))

            # Now done should work
            r = tools.cmd_done(make_args(repo=repo.name, feature=1))
            assert r["ok"]

    def test_done_with_failed_test_rejected(self, git_repo):
        """cm done with failed test should be rejected."""
        repo, data = _setup_developing(git_repo)
        with mock.patch.object(tools, "_repo_path", return_value=repo), \
             mock.patch.object(tools, "_run_tests", return_value={"ok": False, "output": "FAILED: test_foo"}):
            _write_and_commit(data["worktree"])
            tools.cmd_test(make_args(repo=repo.name, feature=1))
            r = tools.cmd_done(make_args(repo=repo.name, feature=1))
            assert not r["ok"]
            assert "failed" in r["error"].lower()


# ══════════════════════════════════════════════════════════
#  Reopen + re-integrate flow
# ══════════════════════════════════════════════════════════


class TestReopenFlow:
    def test_reopen_resets_test_status(self, git_repo):
        repo, data = _setup_developing(git_repo)
        with _mock_repo(repo):
            _write_and_commit(data["worktree"])
            tools.cmd_test(make_args(repo=repo.name, feature=1))
            tools.cmd_done(make_args(repo=repo.name, feature=1))

            # Reopen
            r = tools.cmd_reopen(make_args(repo=repo.name, feature=1))
            assert r["ok"]
            claims = tools._atomic_json_read(repo / tools.CM_DIR / "claims.json")
            feat = claims["features"]["1"]
            assert feat["phase"] == "developing"
            assert feat["developing"]["test_status"] == "pending"

    def test_reopen_reverts_session_from_integrating(self, git_repo):
        repo, data = _setup_developing(git_repo)
        with _mock_repo(repo):
            _write_and_commit(data["worktree"])
            tools.cmd_test(make_args(repo=repo.name, feature=1))
            tools.cmd_done(make_args(repo=repo.name, feature=1))
            tools.cmd_integrate(make_args(repo=repo.name))
            lock = tools._atomic_json_read(repo / tools.CM_DIR / "lock.json")
            assert lock["session_phase"] == "integrating"

            tools.cmd_reopen(make_args(repo=repo.name, feature=1))
            lock = tools._atomic_json_read(repo / tools.CM_DIR / "lock.json")
            assert lock["session_phase"] == "working"


# ══════════════════════════════════════════════════════════
#  cmd_doctor tests
# ══════════════════════════════════════════════════════════


class TestDoctor:
    def test_clean_state(self, git_repo):
        repo = _setup_locked(git_repo)
        with _mock_repo(repo):
            r = tools.cmd_doctor(make_args(repo=repo.name, fix=False))
            assert r["ok"]
            assert len(r["data"]["issues"]) == 0

    def test_detect_expired_lease(self, git_repo):
        repo = _setup_locked(git_repo)
        # Manually expire the lease
        lock_path = repo / tools.CM_DIR / "lock.json"
        lock = json.loads(lock_path.read_text())
        lock["lease_expires_at"] = "2020-01-01T00:00:00+00:00"
        lock_path.write_text(json.dumps(lock))
        with _mock_repo(repo):
            r = tools.cmd_doctor(make_args(repo=repo.name, fix=False))
            assert not r["ok"]
            assert any("expired" in i for i in r["data"]["issues"])

    def test_detect_orphaned_worktree(self, git_repo):
        repo = _setup_locked(git_repo)
        orphan = repo.parent / f"{repo.name}-feature-99"
        orphan.mkdir()
        with _mock_repo(repo):
            r = tools.cmd_doctor(make_args(repo=repo.name, fix=False))
            assert any("orphaned" in i for i in r["data"]["issues"])

    def test_fix_orphaned_worktree(self, git_repo):
        repo = _setup_locked(git_repo)
        orphan = repo.parent / f"{repo.name}-feature-99"
        orphan.mkdir()
        with _mock_repo(repo):
            tools.cmd_doctor(make_args(repo=repo.name, fix=True))
            assert not orphan.exists()

    def test_detect_missing_worktree(self, git_repo):
        repo, data = _setup_claimed(git_repo)
        # Remove the worktree directory manually
        wt = Path(data["worktree"])
        if wt.exists():
            subprocess.run(["git", "worktree", "remove", str(wt), "--force"],
                           cwd=repo, capture_output=True)
        with _mock_repo(repo):
            r = tools.cmd_doctor(make_args(repo=repo.name, fix=False))
            assert any("does not exist" in i for i in r["data"]["issues"])

    def test_fix_missing_worktree_resets_to_pending(self, git_repo):
        repo, data = _setup_claimed(git_repo)
        wt = Path(data["worktree"])
        if wt.exists():
            subprocess.run(["git", "worktree", "remove", str(wt), "--force"],
                           cwd=repo, capture_output=True)
        with _mock_repo(repo):
            tools.cmd_doctor(make_args(repo=repo.name, fix=True))
            claims = tools._atomic_json_read(repo / tools.CM_DIR / "claims.json")
            assert claims["features"]["1"]["phase"] == "pending"

    def test_detect_plan_claims_mismatch(self, git_repo):
        repo = _setup_locked(git_repo)
        plan_path = repo / tools.CM_DIR / "PLAN.md"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(SINGLE_FEATURE_PLAN)
        # Write claims with a feature not in PLAN.md
        claims_path = repo / tools.CM_DIR / "claims.json"
        claims_path.write_text(json.dumps({"features": {"99": {"phase": "developing"}}}))
        with _mock_repo(repo):
            r = tools.cmd_doctor(make_args(repo=repo.name, fix=False))
            assert any("not in PLAN" in i for i in r["data"]["issues"])


# ══════════════════════════════════════════════════════════
#  cmd_renew
# ══════════════════════════════════════════════════════════


class TestRenew:
    def test_renew_extends_lease(self, git_repo):
        repo = _setup_locked(git_repo)
        with _mock_repo(repo):
            lock_before = tools._atomic_json_read(repo / tools.CM_DIR / "lock.json")
            r = tools.cmd_renew(make_args(repo=repo.name))
            assert r["ok"]
            lock_after = tools._atomic_json_read(repo / tools.CM_DIR / "lock.json")
            assert lock_after["lease_expires_at"] >= lock_before["lease_expires_at"]

    def test_renew_wrong_agent_rejected(self, git_repo):
        repo = _setup_locked(git_repo)
        with _mock_repo(repo):
            r = tools.cmd_renew(make_args(repo=repo.name, agent="stranger"))
            assert not r["ok"]
            assert "not in session" in r["error"]


# ══════════════════════════════════════════════════════════
#  cmd_journal
# ══════════════════════════════════════════════════════════


class TestJournal:
    def test_journal_append(self, git_repo):
        repo = _setup_locked(git_repo)
        with _mock_repo(repo):
            r = tools.cmd_journal(make_args(repo=repo.name, message="test note"))
            assert r["ok"]
            journal = (repo / tools.CM_DIR / "JOURNAL.md").read_text()
            assert "test note" in journal


# ══════════════════════════════════════════════════════════
#  Concurrency: multi-process claim race
# ══════════════════════════════════════════════════════════


def _journal_worker(repo_path_str, i):
    """Worker for concurrent journal append test."""
    import sys
    scripts = str(Path(__file__).resolve().parent.parent / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import tools as t
    t._append_journal(Path(repo_path_str), f"agent-{i}", "test", f"entry-{i}")
    return True


def _claim_worker(repo_path_str, feature_id, agent_name):
    """Worker function for multiprocessing claim race test."""
    # Re-import in subprocess
    import sys
    scripts = str(Path(__file__).resolve().parent.parent / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import tools as t

    with mock.patch.object(t, "_repo_path", return_value=Path(repo_path_str)):
        args = mock.MagicMock()
        args.repo = Path(repo_path_str).name
        args.agent = agent_name
        args.feature = feature_id
        result = t.cmd_claim(args)
        return {"ok": result.get("ok", False), "agent": agent_name}


class TestConcurrency:
    def test_concurrent_claim_same_feature(self, git_repo):
        """Multiple processes race to claim the same feature — only 1 wins."""
        repo = _setup_reviewed(git_repo, THREE_INDEPENDENT_PLAN)
        n_workers = 5

        with multiprocessing.Pool(n_workers) as pool:
            futures = [
                pool.apply_async(_claim_worker, (str(repo), 1, f"agent-{i}"))
                for i in range(n_workers)
            ]
            results = [f.get(timeout=30) for f in futures]

        success = [r for r in results if r["ok"]]
        failed = [r for r in results if not r["ok"]]
        assert len(success) == 1, f"Expected exactly 1 success, got {len(success)}: {results}"
        assert len(failed) == n_workers - 1

        claims = tools._atomic_json_read(repo / tools.CM_DIR / "claims.json")
        assert claims["features"]["1"]["phase"] == "analyzing"

    def test_concurrent_claim_different_features(self, git_repo):
        """3 processes claim different features sequentially — all succeed.

        Note: git worktree operations aren't safe under true parallelism
        (they share .git/worktrees), so we test sequential claims from
        different agents instead. The flock on claims.json guarantees
        atomicity; worktree creation is the non-parallelizable part.
        """
        repo = _setup_reviewed(git_repo, THREE_INDEPENDENT_PLAN)

        with _mock_repo(repo):
            for i in range(3):
                r = tools.cmd_claim(make_args(
                    repo=repo.name, feature=i + 1, agent=f"agent-{i}",
                ))
                assert r["ok"], f"Feature {i+1} claim failed: {r}"

        claims = tools._atomic_json_read(repo / tools.CM_DIR / "claims.json")
        assert len(claims["features"]) == 3
        agents = {f["agent"] for f in claims["features"].values()}
        assert len(agents) == 3  # each agent owns a different feature

    def test_concurrent_journal_append(self, git_repo):
        """Multiple processes appending to JOURNAL.md — no data loss."""
        repo = _setup_locked(git_repo)
        n_workers = 10

        with multiprocessing.Pool(n_workers) as pool:
            futures = [
                pool.apply_async(_journal_worker, (str(repo), i))
                for i in range(n_workers)
            ]
            results = [f.get(timeout=10) for f in futures]

        assert all(results)
        journal = (repo / tools.CM_DIR / "JOURNAL.md").read_text()
        for i in range(n_workers):
            assert f"entry-{i}" in journal, f"Missing entry-{i} in journal"


# ══════════════════════════════════════════════════════════
#  cmd_progress: action guidance
# ══════════════════════════════════════════════════════════


class TestProgress:
    def test_locked_no_plan(self, git_repo):
        repo = _setup_locked(git_repo)
        with _mock_repo(repo):
            r = tools.cmd_progress(make_args(repo=repo.name))
            assert r["ok"]
            steps = r["data"]["session_steps"]
            assert any("PLAN" in s for s in steps)

    def test_locked_with_plan(self, git_repo):
        repo = _setup_locked(git_repo)
        plan_path = repo / tools.CM_DIR / "PLAN.md"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(SINGLE_FEATURE_PLAN)
        with _mock_repo(repo):
            r = tools.cmd_progress(make_args(repo=repo.name))
            steps = r["data"]["session_steps"]
            assert any("plan-ready" in s.lower() for s in steps)

    def test_reviewed_suggests_claim(self, git_repo):
        repo = _setup_reviewed(git_repo)
        with _mock_repo(repo):
            r = tools.cmd_progress(make_args(repo=repo.name))
            steps = r["data"]["session_steps"]
            assert any("claim" in s.lower() for s in steps)

    def test_all_done_suggests_integrate(self, git_repo):
        repo, data = _setup_developing(git_repo)
        with _mock_repo(repo):
            _write_and_commit(data["worktree"])
            tools.cmd_test(make_args(repo=repo.name, feature=1))
            tools.cmd_done(make_args(repo=repo.name, feature=1))
            r = tools.cmd_progress(make_args(repo=repo.name))
            assert any("integrate" in s.lower() for s in r["data"]["suggestions"])

    def test_developing_with_failed_test(self, git_repo):
        repo, data = _setup_developing(git_repo)
        # Write a failed test result
        with mock.patch.object(tools, "_repo_path", return_value=repo), \
             mock.patch.object(tools, "_run_tests", return_value={"ok": False, "output": "FAILED: test_x"}):
            _write_and_commit(data["worktree"])
            tools.cmd_test(make_args(repo=repo.name, feature=1))

        with _mock_repo(repo):
            r = tools.cmd_progress(make_args(repo=repo.name))
            feat = r["data"]["features"][0]
            assert feat["phase"] == "developing"
            # action_steps should mention fixing
            assert any("fix" in s.lower() or "FAILED" in s for s in feat["action_steps"])


# ══════════════════════════════════════════════════════════
#  Integrate failure scenarios
# ══════════════════════════════════════════════════════════


class TestIntegrate:
    def test_integrate_not_all_done(self, git_repo):
        repo, _ = _setup_developing(git_repo)
        with _mock_repo(repo):
            r = tools.cmd_integrate(make_args(repo=repo.name))
            assert not r["ok"]
            assert "not done" in r["error"]

    def test_integrate_test_failure_rollback(self, git_repo):
        repo, data = _setup_developing(git_repo)
        with _mock_repo(repo):
            _write_and_commit(data["worktree"])
            tools.cmd_test(make_args(repo=repo.name, feature=1))
            tools.cmd_done(make_args(repo=repo.name, feature=1))

        # Integrate with test failure
        with mock.patch.object(tools, "_repo_path", return_value=repo), \
             mock.patch.object(tools, "_run_tests", return_value={"ok": False, "output": "FAIL"}):
            r = tools.cmd_integrate(make_args(repo=repo.name))
            assert not r["ok"]
            assert "failed" in r["error"]
            # session_phase should NOT have changed
            lock = tools._atomic_json_read(repo / tools.CM_DIR / "lock.json")
            assert lock["session_phase"] == "working"
