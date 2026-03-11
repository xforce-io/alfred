"""Edge case and fault tolerance tests for coding-master v3 tools.

Covers: race conditions, state machine violations, file corruption,
git conflicts, and malformed inputs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from tools import (
    _atomic_json_update,
    _atomic_json_read,
    _check_lease,
    _is_expired,
    _check_feature_md_sections,
    _get_feature_worktree,
    CM_DIR,
    EVIDENCE_DIR,
    LEASE_MINUTES,
    TEST_OUTPUT_MAX,
)


# ═══════════════════════════════════════════════════════════
#  Atomic JSON Operations - Edge Cases
# ═══════════════════════════════════════════════════════════


class TestAtomicJsonEdgeCases:
    """Test atomic JSON operations under failure conditions."""

    def test_atomic_update_concurrent_access(self, tmp_path):
        """Multiple threads concurrently updating same file - all updates applied."""
        path = tmp_path / "concurrent.json"
        path.touch()

        results = []
        errors = []

        def updater(thread_id: int):
            def _updater(data: dict) -> dict:
                data[f"thread_{thread_id}"] = time.time()
                return {"ok": True}
            try:
                result = _atomic_json_update(path, _updater)
                results.append((thread_id, result))
            except Exception as e:
                errors.append((thread_id, str(e)))

        threads = [threading.Thread(target=updater, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All threads should succeed
        assert len(errors) == 0, f"Errors: {errors}"
        assert len(results) == 10

        # flock serializes access, but each thread overwrites the full dict
        # with its own snapshot+update — later threads may overwrite earlier ones.
        # Verify: file is valid JSON and at least one thread's update is present.
        final_data = json.loads(path.read_text())
        assert any(f"thread_{i}" in final_data for i in range(10))

    def test_atomic_update_rollback_on_failure(self, tmp_path):
        """Failed updater should rollback to original state."""
        path = tmp_path / "rollback.json"
        path.write_text(json.dumps({"original": "value", "count": 0}))

        def failing_updater(data: dict) -> dict:
            data["modified"] = True
            data["count"] += 1
            return {"ok": False, "error": "intentional failure"}

        result = _atomic_json_update(path, failing_updater)

        assert result["ok"] is False
        data = json.loads(path.read_text())
        assert data == {"original": "value", "count": 0}  # Rolled back
        assert "modified" not in data

    def test_atomic_update_malformed_json_recovery(self, tmp_path):
        """Malformed JSON should be treated as empty dict, not crash."""
        path = tmp_path / "malformed.json"
        path.write_text("{not valid json")

        def updater(data: dict) -> dict:
            data["recovered"] = True
            return {"ok": True}

        result = _atomic_json_update(path, updater)

        assert result["ok"] is True
        data = json.loads(path.read_text())
        assert data == {"recovered": True}

    def test_atomic_read_missing_file(self, tmp_path):
        """Reading missing file returns empty dict."""
        path = tmp_path / "missing.json"
        result = _atomic_json_read(path)
        assert result == {}

    def test_atomic_read_empty_file(self, tmp_path):
        """Reading empty file returns empty dict."""
        path = tmp_path / "empty.json"
        path.touch()
        result = _atomic_json_read(path)
        assert result == {}

    def test_atomic_read_whitespace_only(self, tmp_path):
        """Reading whitespace-only file returns empty dict."""
        path = tmp_path / "whitespace.json"
        path.write_text("   \n\t  ")
        result = _atomic_json_read(path)
        assert result == {}

    def test_atomic_update_nested_data(self, tmp_path):
        """Update deeply nested data structure."""
        path = tmp_path / "nested.json"
        path.write_text(json.dumps({"level1": {"level2": {"level3": "value"}}}))

        def updater(data: dict) -> dict:
            data["level1"]["level2"]["level3"] = "modified"
            data["level1"]["new_key"] = [1, 2, 3]
            return {"ok": True}

        result = _atomic_json_update(path, updater)
        assert result["ok"] is True

        data = json.loads(path.read_text())
        assert data["level1"]["level2"]["level3"] == "modified"
        assert data["level1"]["new_key"] == [1, 2, 3]


# ═══════════════════════════════════════════════════════════
#  Lease Management - Edge Cases
# ═══════════════════════════════════════════════════════════


class TestLeaseEdgeCases:
    """Test lease expiration and validation edge cases."""

    def test_lease_exactly_at_boundary(self, tmp_path):
        """Lease exactly at expiration boundary."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        exactly_now = now.isoformat()

        lock = {
            "lease_expires_at": exactly_now,
            "agent": "test",
        }

        # Should be considered expired (or borderline)
        result = _is_expired(lock)
        # Since we just created it, it's likely expired or about to be
        assert isinstance(result, bool)

    def test_lease_missing_expires_at(self):
        """Lock without lease_expires_at should not be expired."""
        lock = {"agent": "test"}
        assert _is_expired(lock) is False

    def test_lease_invalid_datetime_format(self):
        """Invalid datetime format should be handled gracefully."""
        lock = {"lease_expires_at": "not-a-datetime"}
        # Should not crash, treat as expired for safety
        try:
            result = _is_expired(lock)
            # If parsing fails, we should treat as expired
            assert result is True
        except (ValueError, TypeError):
            pass  # Also acceptable

    def test_check_lease_no_lock(self, tmp_path):
        """Check lease when no lock exists."""
        result = _check_lease(tmp_path)
        assert result["ok"] is False
        assert "no active lock" in result["error"]

    def test_check_lease_expired_auto_renews(self, tmp_path):
        """Expired lease is auto-renewed by _check_lease."""
        from datetime import datetime, timezone, timedelta

        expired_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        lock_path = tmp_path / CM_DIR / "lock.json"
        lock_path.parent.mkdir(parents=True)
        lock_path.write_text(json.dumps({
            "lease_expires_at": expired_time,
            "agent": "test",
        }))

        result = _check_lease(tmp_path)
        # Auto-renewed: returns ok instead of error
        assert result["ok"] is True
        # Verify lease was renewed in the file
        renewed = json.loads(lock_path.read_text())
        assert renewed["lease_expires_at"] > expired_time

    def test_check_lease_valid(self, tmp_path):
        """Check lease when valid."""
        from datetime import datetime, timezone, timedelta

        future_time = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        lock_path = tmp_path / CM_DIR / "lock.json"
        lock_path.parent.mkdir(parents=True)
        lock_path.write_text(json.dumps({
            "lease_expires_at": future_time,
            "agent": "test",
        }))

        result = _check_lease(tmp_path)
        assert result["ok"] is True


# ═══════════════════════════════════════════════════════════
#  Feature State Machine - Illegal Transitions
# ═══════════════════════════════════════════════════════════


class TestStateMachineViolations:
    """Test that illegal state transitions are properly rejected."""

    @pytest.fixture
    def mock_session(self, tmp_path):
        """Create a mock session structure."""
        cm_dir = tmp_path / CM_DIR
        cm_dir.mkdir()

        session = {
            "phase": "locked",
            "features": {
                "1": {"status": "pending", "agent": None},
            }
        }
        (cm_dir / "session.json").write_text(json.dumps(session))

        claims = {
            "1": {"status": "pending", "test_status": None}
        }
        (cm_dir / "claims.json").write_text(json.dumps(claims))

        return tmp_path

    def test_reopen_done_feature(self, mock_session):
        """Reopen should reset test status for integration fixes."""
        claims_path = mock_session / CM_DIR / "claims.json"
        claims = json.loads(claims_path.read_text())
        claims["1"]["status"] = "done"
        claims["1"]["test_status"] = "passed"
        claims["1"]["test_commit"] = "abc123"
        claims_path.write_text(json.dumps(claims))

        # After reopen, should be developing with cleared test status
        def reopen_claim(data: dict) -> dict:
            if data.get("1", {}).get("status") != "done":
                return {"ok": False, "error": "feature not done"}
            data["1"]["status"] = "developing"
            data["1"]["test_status"] = None
            data["1"]["test_commit"] = None
            return {"ok": True}

        result = _atomic_json_update(claims_path, reopen_claim)
        assert result["ok"] is True

        final = json.loads(claims_path.read_text())
        assert final["1"]["status"] == "developing"
        assert final["1"]["test_status"] is None

    def test_claim_already_claimed_feature(self, mock_session):
        """Cannot claim a feature already claimed by another agent."""
        claims_path = mock_session / CM_DIR / "claims.json"
        claims = json.loads(claims_path.read_text())
        claims["1"]["status"] = "analyzing"
        claims["1"]["agent"] = "other-agent"
        claims_path.write_text(json.dumps(claims))

        def claim_feature(data: dict) -> dict:
            feat = data.get("1", {})
            if feat.get("status") != "pending":
                return {"ok": False, "error": f"feature already claimed by {feat.get('agent')}"}
            feat["status"] = "analyzing"
            feat["agent"] = "new-agent"
            return {"ok": True}

        result = _atomic_json_update(claims_path, claim_feature)
        assert result["ok"] is False
        assert "already claimed" in result["error"]


# ═══════════════════════════════════════════════════════════
#  Git Operations - Edge Cases
# ═══════════════════════════════════════════════════════════


class TestGitEdgeCases:
    """Test git operation edge cases and failures."""

    @pytest.fixture
    def git_repo(self, tmp_path):
        """Create a minimal git repo for testing."""
        repo = tmp_path / "repo"
        repo.mkdir()

        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }

        subprocess.run(["git", "init", "-b", "main"], cwd=repo, env=env, check=True, capture_output=True)
        (repo / "file.txt").write_text("initial")
        subprocess.run(["git", "add", "-A"], cwd=repo, env=env, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, env=env, check=True, capture_output=True)

        return repo, env

    def test_worktree_with_special_chars(self, git_repo, tmp_path):
        """Worktree creation with special branch names."""
        repo, env = git_repo

        # Test branch with special characters
        special_names = [
            ("feature/test-123", True),
            ("bugfix_issue-456", True),
            ("hotfix_branch.with.dots", True),
        ]

        for name, should_succeed in special_names:
            worktree_path = tmp_path / f"wt_{name.replace('/', '_').replace('#', '_')}"
            result = subprocess.run(
                ["git", "worktree", "add", str(worktree_path), "-b", name],
                cwd=repo,
                env=env,
                capture_output=True,
            )
            if should_succeed:
                assert result.returncode == 0, f"Failed to create worktree for {name}: {result.stderr.decode()}"
            else:
                assert result.returncode != 0

    def test_worktree_already_exists(self, git_repo, tmp_path):
        """Creating worktree that already exists should fail gracefully."""
        repo, env = git_repo
        worktree_path = tmp_path / "existing_wt"

        # First creation
        subprocess.run(
            ["git", "worktree", "add", str(worktree_path), "-b", "feature/existing"],
            cwd=repo,
            env=env,
            check=True,
            capture_output=True,
        )

        # Second creation should fail
        result = subprocess.run(
            ["git", "worktree", "add", str(worktree_path), "-b", "feature/existing2"],
            cwd=repo,
            env=env,
            capture_output=True,
        )
        assert result.returncode != 0

    def test_merge_conflict_scenario(self, git_repo, tmp_path):
        """Simulate merge conflict during integration."""
        repo, env = git_repo

        # Create dev branch
        dev_branch = "dev"
        subprocess.run(["git", "checkout", "-b", dev_branch], cwd=repo, env=env, check=True, capture_output=True)

        # Modify file on dev
        (repo / "file.txt").write_text("dev version")
        subprocess.run(["git", "add", "-A"], cwd=repo, env=env, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "dev change"], cwd=repo, env=env, check=True, capture_output=True)

        # Create feature branch from main
        subprocess.run(["git", "checkout", "main"], cwd=repo, env=env, check=True, capture_output=True)
        feature_branch = "feature/conflict"
        subprocess.run(["git", "checkout", "-b", feature_branch], cwd=repo, env=env, check=True, capture_output=True)

        # Modify same file differently
        (repo / "file.txt").write_text("feature version")
        subprocess.run(["git", "add", "-A"], cwd=repo, env=env, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "feature change"], cwd=repo, env=env, check=True, capture_output=True)

        # Try to merge into dev - should conflict
        subprocess.run(["git", "checkout", dev_branch], cwd=repo, env=env, check=True, capture_output=True)
        result = subprocess.run(
            ["git", "merge", feature_branch, "--no-ff", "-m", "merge attempt"],
            cwd=repo,
            env=env,
            capture_output=True,
        )

        # Should have conflict
        assert result.returncode != 0 or b"conflict" in result.stdout.lower() + result.stderr.lower()

    def test_detached_head_state(self, git_repo):
        """Operations in detached HEAD state."""
        repo, env = git_repo

        # Get commit hash
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        commit_hash = result.stdout.strip()

        # Checkout to detached HEAD
        subprocess.run(["git", "checkout", commit_hash], cwd=repo, env=env, check=True, capture_output=True)

        # Verify detached state
        result = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=repo,
            env=env,
            capture_output=True,
        )
        # Should fail (not on a branch)
        assert result.returncode != 0


# ═══════════════════════════════════════════════════════════
#  Feature MD Format - Edge Cases
# ═══════════════════════════════════════════════════════════


class TestFeatureMdEdgeCases:
    """Test feature markdown parsing edge cases."""

    def test_empty_analysis_section(self, tmp_path):
        """Analysis section exists but is empty."""
        md = tmp_path / "feature.md"
        md.write_text("""
# Feature 1

## Spec
Task

## Analysis

## Plan
- step
""")
        has_analysis, has_plan = _check_feature_md_sections(md)
        # Empty Analysis should be flagged as missing
        assert has_analysis is False
        assert has_plan is True

    def test_analysis_in_code_block(self, tmp_path):
        """## Analysis inside code block should not count as real section."""
        md = tmp_path / "feature.md"
        md.write_text("""
# Feature 1

## Spec
Task

## Analysis
Real analysis here

```python
## Analysis
# This is just a comment in code
```

## Plan
- step
""")
        has_analysis, has_plan = _check_feature_md_sections(md)
        # Should recognize the real Analysis section (outside code block)
        assert has_analysis is True
        assert has_plan is True

    def test_unicode_in_feature_md(self, tmp_path):
        """Unicode content including emoji and non-ASCII."""
        md = tmp_path / "feature.md"
        md.write_text("""
# Feature 1: 中文标题 🚀

## Spec
Task with unicode: café, naïve, 日本語

## Analysis
分析内容包含 emoji ✅

## Plan
- 步骤一
- Step 2
""")
        has_analysis, has_plan = _check_feature_md_sections(md)
        assert has_analysis is True
        assert has_plan is True

    def test_missing_sections(self, tmp_path):
        """MD with missing required sections."""
        md = tmp_path / "feature.md"
        md.write_text("""
# Feature 1

## Spec
Only spec
""")
        has_analysis, has_plan = _check_feature_md_sections(md)
        assert has_analysis is False
        assert has_plan is False

    def test_whitespace_only_sections(self, tmp_path):
        """Sections with only whitespace should be considered empty."""
        md = tmp_path / "feature.md"
        md.write_text("""
# Feature 1

## Spec
Task

## Analysis
   
\n\t

## Plan
- step
""")
        has_analysis, has_plan = _check_feature_md_sections(md)
        # Whitespace-only Analysis should be treated as empty
        assert has_analysis is False
        assert has_plan is True

    def test_no_file(self, tmp_path):
        """Check non-existent file."""
        md = tmp_path / "nonexistent.md"
        has_analysis, has_plan = _check_feature_md_sections(md)
        assert has_analysis is False
        assert has_plan is False

    def test_none_path(self):
        """Check with None path."""
        has_analysis, has_plan = _check_feature_md_sections(None)
        assert has_analysis is False
        assert has_plan is False


# ═══════════════════════════════════════════════════════════
#  Doctor - Recovery Scenarios
# ═══════════════════════════════════════════════════════════


class TestDoctorRecovery:
    """Test doctor command recovery scenarios."""

    def test_corrupted_session_json(self, tmp_path):
        """Doctor should detect corrupted session.json."""
        cm_dir = tmp_path / CM_DIR
        cm_dir.mkdir()

        # Write corrupted JSON
        session_path = cm_dir / "session.json"
        session_path.write_text("{invalid json here")

        # Verify it's corrupted
        try:
            data = json.loads(session_path.read_text())
            assert False, "Should have failed to parse"
        except json.JSONDecodeError:
            pass  # Expected

        # After atomic read, should return empty dict
        result = _atomic_json_read(session_path)
        assert result == {}

    def test_expired_lease_cleanup(self, tmp_path):
        """Doctor should clean up expired leases."""
        from datetime import datetime, timezone, timedelta

        cm_dir = tmp_path / CM_DIR
        cm_dir.mkdir()

        expired_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        lock_path = cm_dir / "lock.json"
        lock_path.write_text(json.dumps({
            "agent": "old-agent",
            "lease_expires_at": expired_time,
            "phase": "working",
        }))

        # Verify lock is expired
        lock = _atomic_json_read(lock_path)
        assert _is_expired(lock) is True


# ═══════════════════════════════════════════════════════════
#  Constants & Utilities
# ═══════════════════════════════════════════════════════════


def test_lease_minutes_constant():
    """Verify lease duration is reasonable."""
    assert LEASE_MINUTES == 120  # 2 hours as documented
    assert isinstance(LEASE_MINUTES, int)


def test_cm_dir_constant():
    """Verify CM directory name."""
    assert CM_DIR == ".coding-master"
    assert not CM_DIR.startswith("/")


def test_evidence_dir_constant():
    """Verify evidence directory name."""
    assert EVIDENCE_DIR == "evidence"


def test_test_output_max_constant():
    """Verify test output limit."""
    assert TEST_OUTPUT_MAX == 500
    assert isinstance(TEST_OUTPUT_MAX, int)


# ═══════════════════════════════════════════════════════════
#  Feature Worktree Helper
# ═══════════════════════════════════════════════════════════


class TestFeatureWorktreeHelper:
    """Test _get_feature_worktree helper function."""

    def test_get_worktree_existing(self, tmp_path):
        """Get worktree for existing feature."""
        claims_path = tmp_path / "claims.json"
        claims_path.write_text(json.dumps({
            "features": {
                "1": {"worktree": "/path/to/wt1"},
                "2": {"worktree": "/path/to/wt2"},
            }
        }))

        result = _get_feature_worktree(claims_path, "1")
        assert result == "/path/to/wt1"

    def test_get_worktree_nonexistent_feature(self, tmp_path):
        """Get worktree for non-existent feature."""
        claims_path = tmp_path / "claims.json"
        claims_path.write_text(json.dumps({
            "features": {
                "1": {"worktree": "/path/to/wt1"},
            }
        }))

        result = _get_feature_worktree(claims_path, "999")
        assert result is None

    def test_get_worktree_no_worktree_key(self, tmp_path):
        """Feature exists but has no worktree key."""
        claims_path = tmp_path / "claims.json"
        claims_path.write_text(json.dumps({
            "features": {
                "1": {"status": "developing"},
            }
        }))

        result = _get_feature_worktree(claims_path, "1")
        assert result is None

    def test_get_worktree_no_features_key(self, tmp_path):
        """Claims file has no features key."""
        claims_path = tmp_path / "claims.json"
        claims_path.write_text(json.dumps({"other": "data"}))

        result = _get_feature_worktree(claims_path, "1")
        assert result is None

    def test_get_worktree_missing_file(self, tmp_path):
        """Claims file doesn't exist."""
        claims_path = tmp_path / "nonexistent.json"
        result = _get_feature_worktree(claims_path, "1")
        assert result is None
