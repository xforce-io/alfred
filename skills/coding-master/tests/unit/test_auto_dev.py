"""Tests for auto-dev, submit, and related helpers."""

from __future__ import annotations

import json
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from repo_target import RepoTarget, RepoTargetBinding, WorkspaceContext
from dispatch import (
    _looks_complex,
    _suggest_next,
    _clean_workspace_repos,
    cmd_auto_dev,
    cmd_submit,
    COMMANDS,
    _build_parser,
    ARTIFACT_DIR,
)


# ── _looks_complex ──────────────────────────────────────


class TestLooksComplex:
    def test_simple_task(self):
        assert _looks_complex("fix the login button") is False

    def test_single_signal(self):
        assert _looks_complex("refactor the auth module") is False

    def test_two_signals_triggers(self):
        assert _looks_complex("重构整个 inspector 模块") is True

    def test_mixed_lang_signals(self):
        assert _looks_complex("refactor redesign the system") is True

    def test_case_insensitive(self):
        assert _looks_complex("Refactor and Redesign") is True

    def test_empty(self):
        assert _looks_complex("") is False


# ── _suggest_next ───────────────────────────────────────


class TestSuggestNext:
    def test_feature_mode_success(self):
        args = SimpleNamespace(workspace="env0", repos=None)
        hint = _suggest_next(args, feature_mode=True, tests_passed=True)
        assert "feature next" in hint

    def test_feature_mode_failure(self):
        args = SimpleNamespace(workspace="env0", repos=None)
        hint = _suggest_next(args, feature_mode=True, tests_passed=False)
        assert "reset-worktree" in hint

    def test_workspace_mode_success(self):
        args = SimpleNamespace(workspace="env0", repos=None)
        hint = _suggest_next(args, feature_mode=False, tests_passed=True)
        assert "submit" in hint

    def test_repo_mode_success(self):
        args = SimpleNamespace(workspace=None, repos="myrepo")
        hint = _suggest_next(args, feature_mode=False, tests_passed=True)
        assert "submit" in hint
        assert "myrepo" in hint


# ── _clean_workspace_repos ──────────────────────────────


class TestCleanWorkspaceRepos:
    def test_invalid_json(self):
        result = _clean_workspace_repos("/ws", "not json")
        assert result["ok"] is False
        assert result["error_code"] == "NO_SNAPSHOT"

    @patch("dispatch.GitOps.force_clean", return_value={"ok": True})
    def test_no_repos_cleans_root(self, mock_clean):
        result = _clean_workspace_repos("/ws", '{"workspace": {}}')
        assert result["ok"] is True
        mock_clean.assert_called_once_with("/ws")

    @patch("dispatch.GitOps.force_clean", return_value={"ok": True})
    def test_with_repos(self, mock_clean):
        snapshot = json.dumps({"repos": [
            {"name": "a", "path": "/ws/a"},
            {"name": "b", "path": "/ws/b"},
        ]})
        result = _clean_workspace_repos("/ws", snapshot)
        assert result["ok"] is True
        assert mock_clean.call_count == 2


# ── Parser registration ────────────────────────────────


class TestNewCommandRegistration:
    def test_auto_dev_registered(self):
        assert "auto-dev" in COMMANDS

    def test_submit_registered(self):
        assert "submit" in COMMANDS

    def test_aliases_registered(self):
        assert "status" in COMMANDS
        assert "find" in COMMANDS

    def test_auto_dev_parses(self):
        parser = _build_parser()
        args = parser.parse_args([
            "auto-dev", "--repos", "myrepo", "--task", "fix bug"
        ])
        assert args.command == "auto-dev"
        assert args.repos == "myrepo"
        assert args.task == "fix bug"
        assert args.allow_complex is False

    def test_auto_dev_feature_mode_parses(self):
        parser = _build_parser()
        args = parser.parse_args([
            "auto-dev", "--workspace", "env0", "--feature", "next"
        ])
        assert args.feature == "next"
        assert args.workspace == "env0"

    def test_submit_parses(self):
        parser = _build_parser()
        args = parser.parse_args([
            "submit", "--repos", "myrepo", "--title", "my PR"
        ])
        assert args.command == "submit"
        assert args.title == "my PR"

    def test_submit_keep_lock(self):
        parser = _build_parser()
        args = parser.parse_args([
            "submit", "--workspace", "env0", "--title", "PR", "--keep-lock"
        ])
        assert args.keep_lock is True


# ── cmd_auto_dev error paths ────────────────────────────


class TestCmdAutoDevErrors:
    def test_no_repos_no_workspace(self):
        args = SimpleNamespace(
            repos=None, workspace=None, task="fix", feature=None,
            engine=None, allow_complex=False, reset_worktree=False,
            branch=None, repo=None, plan=None,
        )
        result = cmd_auto_dev(args)
        assert result["ok"] is False

    def test_no_task(self):
        args = SimpleNamespace(
            repos="myrepo", workspace=None, task=None, feature=None,
            engine=None, allow_complex=False, reset_worktree=False,
            branch=None, repo=None, plan=None,
        )
        result = cmd_auto_dev(args)
        assert result["ok"] is False
        assert result["error_code"] == "INVALID_ARGS"

    def test_complex_task_blocked(self):
        args = SimpleNamespace(
            repos="myrepo", workspace=None, task="重构整个系统", feature=None,
            engine=None, allow_complex=False, reset_worktree=False,
            branch=None, repo=None, plan=None,
        )
        result = cmd_auto_dev(args)
        assert result["ok"] is False
        assert result["error_code"] == "TASK_TOO_COMPLEX"

    @patch("dispatch.ConfigManager")
    def test_feature_mode_no_workspace(self, MockCM):
        args = SimpleNamespace(
            repos=None, workspace=None, task=None, feature="next",
            engine=None, allow_complex=False, reset_worktree=False,
            branch=None, repo=None, plan=None,
        )
        result = cmd_auto_dev(args)
        assert result["ok"] is False
        assert result["error_code"] == "MISSING_WORKSPACE"

    @patch("dispatch.ConfigManager")
    def test_unknown_engine(self, MockCM):
        MockCM.return_value.get_default_engine.return_value = "unknown-llm"
        args = SimpleNamespace(
            repos="myrepo", workspace=None, task="fix bug", feature=None,
            engine="unknown-llm", allow_complex=False, reset_worktree=False,
            branch=None, repo=None, plan=None,
        )
        result = cmd_auto_dev(args)
        assert result["ok"] is False
        assert result["error_code"] == "ENGINE_ERROR"

    @patch("dispatch._sync_coding_stats")
    @patch("dispatch.with_lock_update", side_effect=lambda path, phase, fn: fn())
    @patch("dispatch._get_engine")
    @patch("dispatch.WorkspaceManager")
    @patch("dispatch.ConfigManager")
    @patch("repo_target.run_final_test")
    @patch("repo_target.resolve_repo_target")
    def test_acquired_workspace_preserves_repo_arg(
        self,
        mock_resolve_repo_target,
        mock_run_final_test,
        MockCM,
        MockWorkspaceManager,
        mock_get_engine,
        _mock_with_lock_update,
        _mock_sync,
    ):
        args = SimpleNamespace(
            repos="myrepo", workspace=None, task="fix bug", feature=None,
            engine=None, allow_complex=False, reset_worktree=False,
            branch=None, repo=None, plan=None,
        )
        config = MockCM.return_value
        config.get_default_engine.return_value = "codex"
        config.get_max_turns.return_value = 5

        engine = mock_get_engine.return_value
        engine.run.return_value = SimpleNamespace(success=True, summary="ok", files_changed=[])

        mgr = MockWorkspaceManager.return_value
        mgr.check_and_acquire_for_repos.return_value = {
            "ok": True,
            "data": {"snapshot": {"workspace": {"name": "env0"}}},
        }

        mock_resolve_repo_target.side_effect = [
            {
                "ok": False,
                "error_code": "NEEDS_WORKSPACE_ACQUISITION",
                "data": {"repo_names": ["myrepo"], "repo_name": "myrepo"},
            },
            RepoTargetBinding(
                workspace=WorkspaceContext(name="env0", path="/tmp/env0", snapshot={}),
                target=RepoTarget(
                    repo_name="myrepo",
                    repo_path="/tmp/env0/myrepo",
                    test_command="pytest",
                    git_root="/tmp/env0/myrepo",
                ),
            ),
        ]
        mock_run_final_test.return_value = SimpleNamespace(
            status="passed",
            reason="success",
            report={},
            passed=True,
        )

        with patch("dispatch._load_artifact", return_value=""):
            result = cmd_auto_dev(args)

        assert result["ok"] is True
        assert mock_resolve_repo_target.call_args_list[1].kwargs["repo_arg"] == "myrepo"


# ── cmd_submit error paths ──────────────────────────────


class TestCmdSubmitErrors:
    def test_no_workspace_no_repos(self):
        args = SimpleNamespace(
            workspace=None, repos=None, repo=None,
            title="PR", body="", keep_lock=False,
        )
        result = cmd_submit(args)
        assert result["ok"] is False
        assert result["error_code"] == "INVALID_ARGS"

    @patch("dispatch._sync_coding_stats")
    @patch("dispatch.cmd_release")
    @patch("dispatch.with_lock_update", side_effect=lambda path, phase, fn: fn())
    @patch("dispatch.ConfigManager")
    @patch("repo_target.resolve_repo_target")
    @patch("repo_target.find_active_workspaces_by_repos")
    def test_submit_repos_preserves_repo_arg(
        self,
        mock_find_active_workspaces,
        mock_resolve_repo_target,
        MockCM,
        _mock_with_lock_update,
        _mock_release,
        _mock_sync,
    ):
        args = SimpleNamespace(
            workspace=None, repos="myrepo", repo=None,
            title="PR", body="", keep_lock=False,
        )
        mock_find_active_workspaces.return_value = [{"name": "env0", "path": "/tmp/env0"}]
        mock_resolve_repo_target.return_value = RepoTargetBinding(
            workspace=WorkspaceContext(name="env0", path="/tmp/env0", snapshot={}),
            target=RepoTarget(
                repo_name="myrepo",
                repo_path="/tmp/env0/myrepo",
                test_command="pytest",
                git_root="/tmp/env0/myrepo",
            ),
        )

        submit_result = {"ok": True, "data": {"pr_url": "https://example.test/pr/1"}}
        with patch("dispatch.GitOps.submit_pr", return_value=submit_result):
            result = cmd_submit(args)

        assert result["ok"] is True
        assert mock_resolve_repo_target.call_args.kwargs["repo_arg"] == "myrepo"
