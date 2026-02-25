"""Tests for workspace.py — LockFile and WorkspaceManager."""

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from workspace import LockFile, WorkspaceManager, LOCK_FILENAME, ARTIFACT_DIR, GITIGNORE_ENTRIES


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  LockFile
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestLockFileCreate:
    def test_atomic_create(self, ws_dir):
        lf = LockFile.create(str(ws_dir), task="fix bug", engine="claude")
        assert lf.lock_path.exists()
        data = json.loads(lf.lock_path.read_text())
        assert data["task"] == "fix bug"
        assert data["engine"] == "claude"
        assert data["phase"] == "workspace-check"
        assert data["pushed_to_remote"] is False
        assert data["phase_history"] == []

    def test_duplicate_create_raises(self, ws_dir):
        LockFile.create(str(ws_dir), task="t1", engine="claude")
        with pytest.raises(FileExistsError):
            LockFile.create(str(ws_dir), task="t2", engine="claude")


class TestLockFileLease:
    def test_not_expired_within_lease(self, ws_dir):
        lf = LockFile.create(str(ws_dir), task="t", engine="e")
        assert not lf.is_expired()

    def test_expired_when_past_lease(self, ws_dir):
        lf = LockFile.create(str(ws_dir), task="t", engine="e")
        # Force expiration
        lf.data["lease_expires_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        ).isoformat()
        assert lf.is_expired()

    def test_expired_when_no_lease_field(self, ws_dir):
        lf = LockFile(str(ws_dir))
        lf.data = {}
        assert lf.is_expired()

    def test_verify_active_cleans_stale(self, ws_dir):
        lf = LockFile.create(str(ws_dir), task="t", engine="e")
        lf.data["lease_expires_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        ).isoformat()
        lf.save()
        lf2 = LockFile(str(ws_dir))
        with pytest.raises(RuntimeError, match="expired"):
            lf2.verify_active()
        assert not lf2.lock_path.exists()

    def test_verify_active_no_lock(self, ws_dir):
        lf = LockFile(str(ws_dir))
        with pytest.raises(RuntimeError, match="no active lock"):
            lf.verify_active()

    def test_renew_lease(self, ws_dir):
        lf = LockFile.create(str(ws_dir), task="t", engine="e")
        old_expires = lf.data["lease_expires_at"]
        time.sleep(0.01)
        lf.renew_lease(minutes=60)
        assert lf.data["lease_expires_at"] != old_expires
        assert not lf.is_expired()


class TestLockFilePhase:
    def test_update_phase_records_history(self, ws_dir):
        lf = LockFile.create(str(ws_dir), task="t", engine="e")
        assert lf.data["phase"] == "workspace-check"
        lf.update_phase("analyzing")
        assert lf.data["phase"] == "analyzing"
        assert len(lf.data["phase_history"]) == 1
        assert lf.data["phase_history"][0]["phase"] == "workspace-check"

    def test_update_phase_no_history_on_first(self, ws_dir):
        lf = LockFile(str(ws_dir))
        lf.data = {"phase_history": []}
        lf.update_phase("first-phase")
        assert lf.data["phase"] == "first-phase"
        assert lf.data["phase_history"] == []

    def test_pushed_to_remote_field(self, ws_dir):
        lf = LockFile.create(str(ws_dir), task="t", engine="e")
        assert lf.data["pushed_to_remote"] is False
        lf.data["pushed_to_remote"] = True
        lf.save()
        lf2 = LockFile(str(ws_dir)).load()
        assert lf2.data["pushed_to_remote"] is True


class TestLockFilePersistence:
    def test_save_and_load(self, ws_dir):
        lf = LockFile.create(str(ws_dir), task="persist", engine="e")
        lf.data["custom"] = "value"
        lf.save()
        lf2 = LockFile(str(ws_dir)).load()
        assert lf2.data["custom"] == "value"
        assert lf2.data["task"] == "persist"

    def test_delete(self, ws_dir):
        lf = LockFile.create(str(ws_dir), task="t", engine="e")
        assert lf.exists()
        lf.delete()
        assert not lf.exists()

    def test_add_artifact(self, ws_dir):
        lf = LockFile.create(str(ws_dir), task="t", engine="e")
        lf.add_artifact("report", ".coding-master/report.json")
        assert lf.data["artifacts"]["report"] == ".coding-master/report.json"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  WorkspaceManager
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestWorkspaceManagerCheckAndAcquire:
    @patch("workspace._run_git", return_value="")
    @patch("workspace._run_cmd", return_value="Python 3.11.0")
    def test_success(self, mock_cmd, mock_git, config_manager, ws_dir):
        # Create pyproject.toml for runtime probe
        (ws_dir / "pyproject.toml").write_text("[tool.ruff]\n")
        mgr = WorkspaceManager(config_manager)
        result = mgr.check_and_acquire("test-ws", "fix bug", "claude")
        assert result["ok"] is True
        assert "snapshot" in result["data"]
        # Lock file created
        assert (ws_dir / LOCK_FILENAME).exists()
        # Artifact dir created
        assert (ws_dir / ARTIFACT_DIR).exists()

    def test_workspace_not_found(self, config_manager):
        mgr = WorkspaceManager(config_manager)
        result = mgr.check_and_acquire("nonexistent", "t", "e")
        assert result["ok"] is False
        assert result["error_code"] == "PATH_NOT_FOUND"

    def test_path_not_exists(self, config_manager, tmp_path):
        # Point workspace to a nonexistent path
        config_manager._load()["coding_master"]["workspaces"]["bad"] = "/nonexistent/path"
        mgr = WorkspaceManager(config_manager)
        result = mgr.check_and_acquire("bad", "t", "e")
        assert result["ok"] is False
        assert result["error_code"] == "PATH_NOT_FOUND"

    def test_not_git_repo(self, config_manager, tmp_path):
        # Workspace exists but has no .git
        bare_dir = tmp_path / "bare"
        bare_dir.mkdir()
        config_manager._load()["coding_master"]["workspaces"]["bare"] = str(bare_dir)
        mgr = WorkspaceManager(config_manager)
        result = mgr.check_and_acquire("bare", "t", "e")
        assert result["ok"] is False
        assert "not a git repository" in result["error"]

    @patch("workspace._run_git", return_value="")
    def test_already_locked(self, mock_git, config_manager, ws_dir):
        LockFile.create(str(ws_dir), task="existing", engine="e")
        mgr = WorkspaceManager(config_manager)
        result = mgr.check_and_acquire("test-ws", "new task", "e")
        assert result["ok"] is False
        assert result["error_code"] == "WORKSPACE_LOCKED"

    @patch("workspace._run_git", return_value=" M file.py\n")
    def test_dirty_working_tree(self, mock_git, config_manager, ws_dir):
        mgr = WorkspaceManager(config_manager)
        result = mgr.check_and_acquire("test-ws", "t", "e")
        assert result["ok"] is False
        assert result["error_code"] == "GIT_DIRTY"


class TestWorkspaceManagerGitignore:
    def test_ensure_gitignore_creates_new(self, ws_dir):
        result = WorkspaceManager._ensure_gitignore(str(ws_dir))
        assert result is True
        content = (ws_dir / ".gitignore").read_text()
        for entry in GITIGNORE_ENTRIES:
            assert entry in content

    def test_ensure_gitignore_appends(self, ws_dir):
        gi = ws_dir / ".gitignore"
        gi.write_text("*.pyc\n")
        result = WorkspaceManager._ensure_gitignore(str(ws_dir))
        assert result is True
        content = gi.read_text()
        assert "*.pyc" in content
        for entry in GITIGNORE_ENTRIES:
            assert entry in content

    def test_ensure_gitignore_noop(self, ws_dir):
        gi = ws_dir / ".gitignore"
        gi.write_text(".coding-master.lock\n.coding-master/\n")
        result = WorkspaceManager._ensure_gitignore(str(ws_dir))
        assert result is False


class TestWorkspaceManagerRelease:
    @patch("workspace._run_git")
    def test_release_basic(self, mock_git, config_manager, ws_dir):
        LockFile.create(str(ws_dir), task="t", engine="e")
        art_dir = ws_dir / ARTIFACT_DIR
        art_dir.mkdir()
        mgr = WorkspaceManager(config_manager)
        result = mgr.release("test-ws")
        assert result["ok"] is True
        assert not (ws_dir / LOCK_FILENAME).exists()
        assert not art_dir.exists()

    def test_release_already_released(self, config_manager):
        mgr = WorkspaceManager(config_manager)
        result = mgr.release("test-ws")
        assert result["ok"] is True
        assert "already released" in result["data"]["message"]

    @patch("workspace._run_git")
    def test_release_cleanup_deletes_branch(self, mock_git, config_manager, ws_dir):
        lf = LockFile.create(str(ws_dir), task="t", engine="e")
        lf.data["branch"] = "fix/my-branch"
        lf.data["pushed_to_remote"] = True
        lf.save()
        # Create snapshot for original branch detection
        art_dir = ws_dir / ARTIFACT_DIR
        art_dir.mkdir(exist_ok=True)
        snap = {"git": {"branch": "main"}}
        (art_dir / "workspace_snapshot.json").write_text(json.dumps(snap))

        mgr = WorkspaceManager(config_manager)
        result = mgr.release("test-ws", cleanup=True)
        assert result["ok"] is True
        # Verify git commands were called for branch cleanup
        calls = [str(c) for c in mock_git.call_args_list]
        assert any("checkout" in c for c in calls)
        assert any("-D" in c for c in calls)
        assert any("push" in c and "--delete" in c for c in calls)
