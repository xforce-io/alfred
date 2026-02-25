"""Tests for git_ops.py — all subprocess calls are mocked."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import patch, call, MagicMock

import pytest

from git_ops import GitOps, PROTECTED_BRANCHES


@pytest.fixture
def git(tmp_path):
    return GitOps(str(tmp_path))


# ── helpers ──────────────────────────────────────────────


def _run_ok(stdout="", stderr="", rc=0):
    """Build a fake subprocess.CompletedProcess."""
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=rc)


# ── get_current_branch ───────────────────────────────────


class TestGetCurrentBranch:
    @patch("subprocess.run", return_value=_run_ok(stdout="feature/foo\n"))
    def test_returns_stripped_branch(self, mock_run, git):
        assert git.get_current_branch() == "feature/foo"
        mock_run.assert_called_once()
        args = mock_run.call_args
        assert args[0][0] == ["git", "rev-parse", "--abbrev-ref", "HEAD"]


# ── create_branch ────────────────────────────────────────


class TestCreateBranch:
    @patch("subprocess.run", return_value=_run_ok(stdout="feature/x\n"))
    def test_already_on_branch(self, mock_run, git):
        result = git.create_branch("feature/x")
        assert result["ok"] is True
        assert result["data"]["created"] is False

    @patch("subprocess.run")
    def test_creates_new_branch(self, mock_run, git):
        mock_run.side_effect = [
            _run_ok(stdout="main\n"),   # get_current_branch
            _run_ok(),                   # checkout -b
        ]
        result = git.create_branch("feature/new")
        assert result["ok"] is True
        assert result["data"]["created"] is True
        assert result["data"]["from"] == "main"


# ── get_diff_summary ────────────────────────────────────


class TestGetDiffSummary:
    @patch("subprocess.run", return_value=_run_ok(stdout=" 2 files changed\n"))
    def test_returns_diff_stat(self, mock_run, git):
        assert "2 files changed" in git.get_diff_summary()


# ── stage_and_commit ─────────────────────────────────────


class TestStageAndCommit:
    @patch("subprocess.run")
    def test_nothing_to_commit(self, mock_run, git):
        mock_run.side_effect = [
            _run_ok(),                   # git add -A
            _run_ok(stdout="  \n"),      # git status --porcelain (empty)
        ]
        result = git.stage_and_commit("msg")
        assert result["ok"] is False
        assert "nothing to commit" in result["error"]

    @patch("subprocess.run")
    def test_successful_commit(self, mock_run, git):
        mock_run.side_effect = [
            _run_ok(),                       # git add -A
            _run_ok(stdout="M foo.py\n"),     # git status --porcelain
            _run_ok(),                        # git commit -m
        ]
        result = git.stage_and_commit("feat: add foo")
        assert result["ok"] is True
        assert result["data"]["message"] == "feat: add foo"


# ── push ─────────────────────────────────────────────────


class TestPush:
    @patch("subprocess.run", return_value=_run_ok(stdout="main\n"))
    def test_refuse_protected_branch(self, mock_run, git):
        result = git.push("main")
        assert result["ok"] is False
        assert "protected branch" in result["error"]

    @patch("subprocess.run")
    def test_push_failure(self, mock_run, git):
        mock_run.side_effect = [
            _run_ok(stdout="feat\n"),                      # get_current_branch (for branch=None)
            _run_ok(stdout="", stderr="rejected", rc=1),   # push
        ]
        result = git.push()  # branch=None → resolves to "feat"
        assert result["ok"] is False
        assert "rejected" in result["error"]

    @patch("subprocess.run")
    def test_push_success(self, mock_run, git):
        mock_run.side_effect = [
            _run_ok(stdout="feat\n"),  # get_current_branch
            _run_ok(),                  # push
        ]
        result = git.push()
        assert result["ok"] is True
        assert result["data"]["branch"] == "feat"

    @patch("subprocess.run", return_value=_run_ok())
    def test_push_explicit_branch(self, mock_run, git):
        result = git.push("my-branch")
        assert result["ok"] is True
        # Should not call get_current_branch when branch is explicit
        assert mock_run.call_count == 1


# ── create_pr ────────────────────────────────────────────


class TestCreatePR:
    @patch("subprocess.run", side_effect=FileNotFoundError)
    def test_gh_not_found(self, mock_run, git):
        result = git.create_pr("title", "body")
        assert result["ok"] is False
        assert "gh CLI not found" in result["error"]

    @patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=60))
    def test_timeout(self, mock_run, git):
        result = git.create_pr("title", "body")
        assert result["ok"] is False
        assert "timed out" in result["error"]

    @patch("subprocess.run", return_value=_run_ok(stderr="permission denied", rc=1))
    def test_gh_failure(self, mock_run, git):
        result = git.create_pr("title", "body")
        assert result["ok"] is False
        assert "permission denied" in result["error"]

    @patch("subprocess.run", return_value=_run_ok(stdout="https://github.com/o/r/pull/42\n"))
    def test_success(self, mock_run, git):
        result = git.create_pr("title", "body")
        assert result["ok"] is True
        assert result["data"]["pr_url"] == "https://github.com/o/r/pull/42"


# ── submit_pr ────────────────────────────────────────────


class TestSubmitPR:
    @patch("subprocess.run", return_value=_run_ok(stdout="main\n"))
    def test_protected_branch(self, mock_run, git):
        result = git.submit_pr("title", "body")
        assert result["ok"] is False
        assert "protected branch" in result["error"]

    @patch.object(GitOps, "get_current_branch", return_value="feat")
    @patch.object(GitOps, "stage_and_commit", return_value={"ok": False, "error": "nothing to commit"})
    def test_commit_failure_short_circuits(self, mock_commit, mock_branch, git):
        result = git.submit_pr("title", "body")
        assert result["ok"] is False
        assert "nothing to commit" in result["error"]

    @patch.object(GitOps, "get_current_branch", return_value="feat")
    @patch.object(GitOps, "stage_and_commit", return_value={"ok": True, "data": {}})
    @patch.object(GitOps, "push", return_value={"ok": False, "error": "rejected"})
    def test_push_failure_short_circuits(self, mock_push, mock_commit, mock_branch, git):
        result = git.submit_pr("title", "body")
        assert result["ok"] is False
        assert "rejected" in result["error"]

    @patch.object(GitOps, "get_current_branch", return_value="feat")
    @patch.object(GitOps, "stage_and_commit", return_value={"ok": True, "data": {}})
    @patch.object(GitOps, "push", return_value={"ok": True, "data": {}})
    @patch.object(GitOps, "create_pr", return_value={"ok": True, "data": {"pr_url": "https://x"}})
    def test_full_success(self, mock_pr, mock_push, mock_commit, mock_branch, git):
        result = git.submit_pr("title", "body")
        assert result["ok"] is True
        assert result["data"]["pr_url"] == "https://x"

    @patch.object(GitOps, "get_current_branch", return_value="feat")
    @patch.object(GitOps, "stage_and_commit", return_value={"ok": True, "data": {}})
    @patch.object(GitOps, "push", return_value={"ok": True, "data": {}})
    @patch.object(GitOps, "create_pr", return_value={"ok": True, "data": {"pr_url": "url"}})
    def test_uses_commit_message_when_provided(self, mock_pr, mock_push, mock_commit, mock_branch, git):
        git.submit_pr("PR Title", "body", commit_message="custom msg")
        mock_commit.assert_called_once_with("custom msg")


# ── cleanup_branch ───────────────────────────────────────


class TestCleanupBranch:
    @patch("subprocess.run")
    def test_checkout_and_delete(self, mock_run, git):
        mock_run.side_effect = [
            _run_ok(stdout="task-branch\n"),  # get_current_branch
            _run_ok(),                         # checkout main
            _run_ok(),                         # branch -D
        ]
        result = git.cleanup_branch("main", "task-branch")
        assert result["ok"] is True
        assert result["data"]["deleted"] == "task-branch"

    @patch("subprocess.run")
    def test_already_on_original_branch(self, mock_run, git):
        mock_run.side_effect = [
            _run_ok(stdout="main\n"),   # get_current_branch — already on original
            _run_ok(),                   # branch -D (no checkout needed)
        ]
        result = git.cleanup_branch("main", "task-branch")
        assert result["ok"] is True
        # Only 2 subprocess calls (no checkout)
        assert mock_run.call_count == 2

    @patch("subprocess.run")
    def test_delete_failure(self, mock_run, git):
        mock_run.side_effect = [
            _run_ok(stdout="main\n"),                      # get_current_branch
            _run_ok(stdout="", stderr="not found", rc=1),  # branch -D fails
        ]
        result = git.cleanup_branch("main", "task-branch")
        assert result["ok"] is False
        assert "not found" in result["error"]
