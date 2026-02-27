"""Tests for workspace.py — LockFile and WorkspaceManager."""

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from workspace import LockFile, WorkspaceManager, LOCK_FILENAME, ARTIFACT_DIR, GITIGNORE_ENTRIES
from git_ops import GitOps


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

    @patch("workspace._run_git", return_value="")
    @patch("workspace._run_cmd", return_value="Python 3.11.0")
    def test_session_json_created(self, mock_cmd, mock_git, config_manager, ws_dir):
        """workspace-check must write session.json with ws_path."""
        (ws_dir / "pyproject.toml").write_text("[tool.ruff]\n")
        mgr = WorkspaceManager(config_manager)
        result = mgr.check_and_acquire("test-ws", "fix bug", "claude")
        assert result["ok"] is True
        session_path = ws_dir / ARTIFACT_DIR / "session.json"
        assert session_path.exists()
        session = json.loads(session_path.read_text())
        assert session["ws_path"] == str(ws_dir.resolve())
        assert session["workspace_name"] == "test-ws"
        assert session["task"] == "fix bug"
        assert session["engine"] == "claude"

    @patch("workspace._run_git", return_value="")
    @patch("workspace._run_cmd", return_value="Python 3.11.0")
    def test_lock_contains_ws_path(self, mock_cmd, mock_git, config_manager, ws_dir):
        """LockFile should store resolved ws_path."""
        (ws_dir / "pyproject.toml").write_text("[tool.ruff]\n")
        mgr = WorkspaceManager(config_manager)
        mgr.check_and_acquire("test-ws", "fix bug", "claude")
        lock = LockFile(str(ws_dir)).load()
        assert lock.data["ws_path"] == str(ws_dir.resolve())

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

    @patch("workspace._run_git")
    def test_release_cleans_session(self, mock_git, config_manager, ws_dir):
        """Release must remove session.json along with other artifacts."""
        LockFile.create(str(ws_dir), task="t", engine="e")
        art_dir = ws_dir / ARTIFACT_DIR
        art_dir.mkdir()
        (art_dir / "session.json").write_text('{"ws_path": "/x"}')
        mgr = WorkspaceManager(config_manager)
        result = mgr.release("test-ws")
        assert result["ok"] is True
        assert not (art_dir / "session.json").exists()

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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Repo-based workflow
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestFindFreeWorkspace:
    def test_returns_first_unlocked(self, repo_config_manager):
        mgr = WorkspaceManager(repo_config_manager)
        ws = mgr._find_free_workspace()
        assert ws is not None
        assert ws["name"] in ("env0", "env1")

    def test_skips_locked_workspace(self, repo_config_manager):
        mgr = WorkspaceManager(repo_config_manager)
        # Lock env0
        ws0 = repo_config_manager.get_workspace("env0")
        LockFile.create(ws0["path"], task="busy", engine="e")
        ws = mgr._find_free_workspace()
        assert ws is not None
        assert ws["name"] == "env1"

    def test_returns_none_when_all_locked(self, repo_config_manager):
        mgr = WorkspaceManager(repo_config_manager)
        for name in ("env0", "env1"):
            ws = repo_config_manager.get_workspace(name)
            LockFile.create(ws["path"], task="busy", engine="e")
        assert mgr._find_free_workspace() is None

    def test_reclaims_expired_lock(self, repo_config_manager):
        mgr = WorkspaceManager(repo_config_manager)
        ws0 = repo_config_manager.get_workspace("env0")
        lf = LockFile.create(ws0["path"], task="old", engine="e")
        lf.data["lease_expires_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        ).isoformat()
        lf.save()
        ws = mgr._find_free_workspace()
        assert ws is not None
        assert ws["name"] == "env0"


class TestEnsureRepo:
    def test_clone_new_repo(self, repo_config_manager, bare_repo):
        mgr = WorkspaceManager(repo_config_manager)
        ws = repo_config_manager.get_workspace("env0")
        rc = repo_config_manager.get_repo("myrepo")
        path = mgr._ensure_repo(ws["path"], rc)
        assert path is not None
        assert (Path(path) / ".git").exists()
        assert (Path(path) / "README.md").exists()

    def test_update_existing_repo(self, repo_config_manager, bare_repo):
        mgr = WorkspaceManager(repo_config_manager)
        ws = repo_config_manager.get_workspace("env0")
        rc = repo_config_manager.get_repo("myrepo")
        # Clone first
        path1 = mgr._ensure_repo(ws["path"], rc)
        # Run again — should fetch/pull, not re-clone
        path2 = mgr._ensure_repo(ws["path"], rc)
        assert path1 == path2
        assert (Path(path2) / ".git").exists()

    def test_clone_failure_returns_none(self, repo_config_manager):
        mgr = WorkspaceManager(repo_config_manager)
        ws = repo_config_manager.get_workspace("env0")
        rc = {"name": "bad", "url": "file:///nonexistent/repo.git"}
        path = mgr._ensure_repo(ws["path"], rc)
        assert isinstance(path, dict) and path["ok"] is False and path["error_code"] == "GIT_ERROR"


class TestCheckAndAcquireForRepos:
    def test_success_auto_allocate(self, repo_config_manager, bare_repo):
        mgr = WorkspaceManager(repo_config_manager)
        result = mgr.check_and_acquire_for_repos(
            ["myrepo"], "fix bug", "claude"
        )
        assert result["ok"] is True
        snapshot = result["data"]["snapshot"]
        assert len(snapshot["repos"]) == 1
        assert snapshot["repos"][0]["name"] == "myrepo"
        assert snapshot["primary_repo"]["name"] == "myrepo"
        assert snapshot["repos"][0]["git"]["branch"] is not None
        assert snapshot["repos"][0]["runtime"]["type"] == "python"

    def test_success_explicit_workspace(self, repo_config_manager, bare_repo):
        mgr = WorkspaceManager(repo_config_manager)
        result = mgr.check_and_acquire_for_repos(
            ["myrepo"], "fix bug", "claude", workspace_name="env1"
        )
        assert result["ok"] is True
        assert result["data"]["snapshot"]["workspace"]["name"] == "env1"

    def test_repo_not_found(self, repo_config_manager):
        mgr = WorkspaceManager(repo_config_manager)
        result = mgr.check_and_acquire_for_repos(
            ["nonexistent"], "task", "claude"
        )
        assert result["ok"] is False
        assert "not found" in result["error"]

    def test_workspace_not_found(self, repo_config_manager):
        mgr = WorkspaceManager(repo_config_manager)
        result = mgr.check_and_acquire_for_repos(
            ["myrepo"], "task", "claude", workspace_name="nosuch"
        )
        assert result["ok"] is False
        assert "not found" in result["error"]

    def test_no_free_workspace(self, repo_config_manager, bare_repo):
        mgr = WorkspaceManager(repo_config_manager)
        # Lock all workspaces
        for name in ("env0", "env1"):
            ws = repo_config_manager.get_workspace(name)
            LockFile.create(ws["path"], task="busy", engine="e")
        result = mgr.check_and_acquire_for_repos(
            ["myrepo"], "task", "claude"
        )
        assert result["ok"] is False
        assert result["error_code"] == "WORKSPACE_LOCKED"

    def test_lock_and_snapshot_created(self, repo_config_manager, bare_repo):
        mgr = WorkspaceManager(repo_config_manager)
        result = mgr.check_and_acquire_for_repos(
            ["myrepo"], "fix it", "codex"
        )
        assert result["ok"] is True
        ws_path = result["data"]["snapshot"]["workspace"]["path"]
        # Lock file exists
        assert (Path(ws_path) / LOCK_FILENAME).exists()
        # Snapshot artifact exists
        assert (Path(ws_path) / ARTIFACT_DIR / "workspace_snapshot.json").exists()

    def test_session_json_created_for_repos(self, repo_config_manager, bare_repo):
        """Repo-based acquire must also write session.json."""
        mgr = WorkspaceManager(repo_config_manager)
        result = mgr.check_and_acquire_for_repos(
            ["myrepo"], "fix it", "codex"
        )
        assert result["ok"] is True
        ws_path = result["data"]["snapshot"]["workspace"]["path"]
        session_path = Path(ws_path) / ARTIFACT_DIR / "session.json"
        assert session_path.exists()
        session = json.loads(session_path.read_text())
        assert session["ws_path"] == str(Path(ws_path).resolve())
        assert session["task"] == "fix it"
        assert session["engine"] == "codex"
