"""Tests for dispatch.py — command handler functions with mocked dependencies."""

from __future__ import annotations

import json
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from dispatch import (
    _resolve_workspace_path,
    _get_engine,
    _load_artifact,
    with_lock_update,
    requires_workspace,
    cmd_config_list,
    cmd_config_add,
    cmd_config_set,
    cmd_config_remove,
    cmd_quick_status,
    cmd_quick_test,
    cmd_quick_find,
    cmd_quick_env,
    cmd_workspace_check,
    cmd_release,
    cmd_renew_lease,
    cmd_feature_plan,
    cmd_feature_next,
    cmd_feature_done,
    cmd_feature_list,
    cmd_feature_update,
    cmd_env_probe,
    cmd_test,
    cmd_submit_pr,
    ARTIFACT_DIR,
)


def _create_session(ws_path: str) -> None:
    """Helper: create a minimal session.json so @requires_workspace passes."""
    art_dir = Path(ws_path) / ARTIFACT_DIR
    art_dir.mkdir(parents=True, exist_ok=True)
    session = {"ws_path": ws_path, "workspace_name": "test", "task": "t", "engine": "e", "created_at": ""}
    (art_dir / "session.json").write_text(json.dumps(session))


# ── _resolve_workspace_path ─────────────────────────────


class TestResolveWorkspacePath:
    @patch("dispatch.ConfigManager")
    def test_found(self, MockCM):
        MockCM.return_value.get_workspace.return_value = {"path": "/app/ws0"}
        args = SimpleNamespace(workspace="ws0")
        assert _resolve_workspace_path(args) == "/app/ws0"

    @patch("dispatch.ConfigManager")
    def test_not_found(self, MockCM):
        MockCM.return_value.get_workspace.return_value = None
        args = SimpleNamespace(workspace="nope")
        assert _resolve_workspace_path(args) is None


# ── _get_engine ──────────────────────────────────────────


class TestGetEngine:
    @patch("dispatch.ClaudeRunner")
    def test_claude(self, MockRunner):
        engine = _get_engine("claude")
        assert engine is not None
        MockRunner.assert_called_once()

    def test_unknown(self):
        assert _get_engine("gpt-5") is None


# ── _load_artifact ───────────────────────────────────────


class TestLoadArtifact:
    def test_exists(self, tmp_path):
        art_dir = tmp_path / ARTIFACT_DIR
        art_dir.mkdir(parents=True)
        (art_dir / "snap.json").write_text('{"ok":true}')
        result = _load_artifact(str(tmp_path), "snap.json")
        assert '"ok"' in result

    def test_missing(self, tmp_path):
        result = _load_artifact(str(tmp_path), "nonexistent.json")
        assert result == "(not available)"


# ── with_lock_update ─────────────────────────────────────


class TestWithLockUpdate:
    @patch("dispatch.LockFile")
    def test_lock_not_found(self, MockLock):
        MockLock.return_value.verify_active.side_effect = RuntimeError("lock not found")
        result = with_lock_update("/ws", "testing", lambda: {"ok": True})
        assert result["ok"] is False
        assert result["error_code"] == "LOCK_NOT_FOUND"

    @patch("dispatch.LockFile")
    def test_lock_expired(self, MockLock):
        MockLock.return_value.verify_active.side_effect = RuntimeError("lease expired")
        result = with_lock_update("/ws", "testing", lambda: {"ok": True})
        assert result["ok"] is False
        assert result["error_code"] == "LEASE_EXPIRED"

    @patch("dispatch.LockFile")
    def test_fn_success_updates_lock(self, MockLock):
        lock = MockLock.return_value
        fn = MagicMock(return_value={"ok": True, "data": "x"})
        result = with_lock_update("/ws", "testing", fn)
        assert result["ok"] is True
        lock.update_phase.assert_called_once_with("testing")
        lock.renew_lease.assert_called_once()
        lock.save.assert_called_once()

    @patch("dispatch.LockFile")
    def test_fn_failure_no_lock_update(self, MockLock):
        lock = MockLock.return_value
        fn = MagicMock(return_value={"ok": False, "error": "fail"})
        result = with_lock_update("/ws", "testing", fn)
        assert result["ok"] is False
        lock.update_phase.assert_not_called()


# ── config commands ──────────────────────────────────────


class TestConfigCommands:
    @patch("dispatch.ConfigManager")
    def test_config_list(self, MockCM):
        MockCM.return_value.list_all.return_value = {"ok": True, "data": {}}
        result = cmd_config_list(SimpleNamespace())
        assert result["ok"] is True

    @patch("dispatch.ConfigManager")
    def test_config_add(self, MockCM):
        MockCM.return_value.add.return_value = {"ok": True}
        args = SimpleNamespace(kind="workspace", name="ws0", value="/tmp/ws0")
        result = cmd_config_add(args)
        assert result["ok"] is True

    @patch("dispatch.ConfigManager")
    def test_config_set(self, MockCM):
        MockCM.return_value.set_field.return_value = {"ok": True}
        args = SimpleNamespace(kind="workspace", name="ws0", key="path", value="/new")
        result = cmd_config_set(args)
        assert result["ok"] is True

    @patch("dispatch.ConfigManager")
    def test_config_remove(self, MockCM):
        MockCM.return_value.remove.return_value = {"ok": True}
        args = SimpleNamespace(kind="workspace", name="ws0")
        result = cmd_config_remove(args)
        assert result["ok"] is True


# ── cmd_workspace_check ──────────────────────────────────


class TestWorkspaceCheck:
    @patch("dispatch.WorkspaceManager")
    @patch("dispatch.ConfigManager")
    def test_success(self, MockCM, MockWM):
        MockCM.return_value.get_default_engine.return_value = "claude"
        MockWM.return_value.check_and_acquire.return_value = {"ok": True}
        args = SimpleNamespace(workspace="ws0", task="build feature", engine=None, repos=None)
        result = cmd_workspace_check(args)
        assert result["ok"] is True


# ── cmd_release / cmd_renew_lease ────────────────────────


class TestReleaseAndRenew:
    @patch("dispatch.WorkspaceManager")
    @patch("dispatch.ConfigManager")
    def test_release(self, MockCM, MockWM):
        MockWM.return_value.release.return_value = {"ok": True}
        args = SimpleNamespace(workspace="ws0", cleanup=False)
        result = cmd_release(args)
        assert result["ok"] is True

    @patch("dispatch.WorkspaceManager")
    @patch("dispatch.ConfigManager")
    def test_renew_lease(self, MockCM, MockWM):
        MockWM.return_value.renew_lease.return_value = {"ok": True}
        args = SimpleNamespace(workspace="ws0")
        result = cmd_renew_lease(args)
        assert result["ok"] is True


# ── feature commands ─────────────────────────────────────


class TestFeatureCommands:
    @patch("dispatch.FeatureManager")
    @patch("dispatch.ConfigManager")
    def test_feature_plan(self, MockCM, MockFM, tmp_path):
        ws_path = str(tmp_path)
        _create_session(ws_path)
        MockCM.return_value.get_workspace.return_value = {"path": ws_path}
        MockFM.return_value.create_plan.return_value = {"ok": True}
        args = SimpleNamespace(
            workspace="ws0", task="build app",
            features='[{"title":"feat1","task":"do x"}]',
        )
        result = cmd_feature_plan(args)
        assert result["ok"] is True

    @patch("dispatch.FeatureManager")
    @patch("dispatch.ConfigManager")
    def test_feature_plan_workspace_not_found(self, MockCM, MockFM):
        MockCM.return_value.get_workspace.return_value = None
        args = SimpleNamespace(workspace="nope", task="x", features="[]")
        result = cmd_feature_plan(args)
        assert result["ok"] is False

    @patch("dispatch.FeatureManager")
    @patch("dispatch.ConfigManager")
    def test_feature_next(self, MockCM, MockFM, tmp_path):
        ws_path = str(tmp_path)
        _create_session(ws_path)
        MockCM.return_value.get_workspace.return_value = {"path": ws_path}
        MockFM.return_value.next_feature.return_value = {"ok": True, "data": {"index": 0}}
        args = SimpleNamespace(workspace="ws0")
        result = cmd_feature_next(args)
        assert result["ok"] is True

    @patch("dispatch.FeatureManager")
    @patch("dispatch.ConfigManager")
    def test_feature_done(self, MockCM, MockFM, tmp_path):
        ws_path = str(tmp_path)
        _create_session(ws_path)
        MockCM.return_value.get_workspace.return_value = {"path": ws_path}
        MockFM.return_value.mark_done.return_value = {"ok": True}
        args = SimpleNamespace(workspace="ws0", index=0, branch="feat", pr="url")
        result = cmd_feature_done(args)
        assert result["ok"] is True

    @patch("dispatch.FeatureManager")
    @patch("dispatch.ConfigManager")
    def test_feature_list(self, MockCM, MockFM):
        MockCM.return_value.get_workspace.return_value = {"path": "/ws"}
        MockFM.return_value.list_all.return_value = {"ok": True, "data": []}
        args = SimpleNamespace(workspace="ws0")
        result = cmd_feature_list(args)
        assert result["ok"] is True

    @patch("dispatch.FeatureManager")
    @patch("dispatch.ConfigManager")
    def test_feature_update(self, MockCM, MockFM, tmp_path):
        ws_path = str(tmp_path)
        _create_session(ws_path)
        MockCM.return_value.get_workspace.return_value = {"path": ws_path}
        MockFM.return_value.update.return_value = {"ok": True}
        args = SimpleNamespace(
            workspace="ws0", index=0, status="done", title="new title", task_desc="desc",
        )
        result = cmd_feature_update(args)
        assert result["ok"] is True
        MockFM.return_value.update.assert_called_once_with(
            index=0, status="done", title="new title", task="desc",
        )


# ── cmd_env_probe ────────────────────────────────────────


class TestCmdEnvProbe:
    @patch("dispatch.ConfigManager")
    def test_workspace_not_found(self, MockCM):
        MockCM.return_value.get_workspace.return_value = None
        args = SimpleNamespace(workspace="nope", env="dev", commands=None)
        result = cmd_env_probe(args)
        assert result["ok"] is False
        assert "PATH_NOT_FOUND" in result.get("error_code", "")


# ── cmd_test ─────────────────────────────────────────────


class TestCmdTest:
    @patch("dispatch.ConfigManager")
    def test_workspace_not_found(self, MockCM):
        MockCM.return_value.get_workspace.return_value = None
        args = SimpleNamespace(workspace="nope")
        result = cmd_test(args)
        assert result["ok"] is False
        assert "PATH_NOT_FOUND" in result.get("error_code", "")


# ── cmd_submit_pr ────────────────────────────────────────


class TestCmdSubmitPR:
    @patch("dispatch.ConfigManager")
    def test_workspace_not_found(self, MockCM):
        MockCM.return_value.get_workspace.return_value = None
        args = SimpleNamespace(workspace="nope", title="pr", body="body")
        result = cmd_submit_pr(args)
        assert result["ok"] is False
        assert "PATH_NOT_FOUND" in result.get("error_code", "")


# ── Quick query commands ────────────────────────────────


class TestQuickStatus:
    @patch("dispatch.LockFile")
    @patch("dispatch.WorkspaceManager")
    @patch("dispatch.ConfigManager")
    def test_success_no_lock(self, MockCM, MockWM, MockLock):
        MockCM.return_value.get_workspace.return_value = {"path": "/ws"}
        MockWM.return_value._probe_git.return_value = {"branch": "main", "dirty": False}
        MockWM.return_value._probe_runtime.return_value = {"type": "python"}
        MockWM.return_value._probe_project.return_value = {"test_command": "pytest"}
        MockLock.return_value.exists.return_value = False
        args = SimpleNamespace(workspace="ws0")
        result = cmd_quick_status(args)
        assert result["ok"] is True
        assert result["data"]["git"]["branch"] == "main"
        assert result["data"]["lock"] is None

    @patch("dispatch.LockFile")
    @patch("dispatch.WorkspaceManager")
    @patch("dispatch.ConfigManager")
    def test_success_with_active_lock(self, MockCM, MockWM, MockLock):
        MockCM.return_value.get_workspace.return_value = {"path": "/ws"}
        MockWM.return_value._probe_git.return_value = {"branch": "fix/bug"}
        MockWM.return_value._probe_runtime.return_value = {"type": "python"}
        MockWM.return_value._probe_project.return_value = {}
        lock_inst = MockLock.return_value
        lock_inst.exists.return_value = True
        lock_inst.data = {"task": "fix bug", "phase": "testing", "engine": "codex", "started_at": "2026-01-01"}
        lock_inst.is_expired.return_value = False
        args = SimpleNamespace(workspace="ws0")
        result = cmd_quick_status(args)
        assert result["ok"] is True
        assert result["data"]["lock"]["task"] == "fix bug"
        assert result["data"]["lock"]["expired"] is False

    @patch("dispatch.ConfigManager")
    def test_workspace_not_found(self, MockCM):
        MockCM.return_value.get_workspace.return_value = None
        args = SimpleNamespace(workspace="nope")
        result = cmd_quick_status(args)
        assert result["ok"] is False
        assert result["error_code"] == "PATH_NOT_FOUND"


class TestQuickTest:
    @patch("dispatch.TestRunner")
    @patch("dispatch.ConfigManager")
    def test_success_test_only(self, MockCM, MockRunner):
        MockCM.return_value.get_workspace.return_value = {"path": "/ws"}
        runner = MockRunner.return_value
        runner._detect_commands.return_value = {"test_command": "pytest", "lint_command": "ruff check ."}
        from test_runner import TestResult
        runner._run_test.return_value = TestResult(passed=True, total=42, passed_count=42, failed_count=0, output="42 passed")
        args = SimpleNamespace(workspace="ws0", path=None, lint=False)
        result = cmd_quick_test(args)
        assert result["ok"] is True
        assert result["data"]["overall"] == "passed"
        assert result["data"]["test"]["total"] == 42
        assert "lint" not in result["data"]

    @patch("dispatch.TestRunner")
    @patch("dispatch.ConfigManager")
    def test_success_with_lint(self, MockCM, MockRunner):
        MockCM.return_value.get_workspace.return_value = {"path": "/ws"}
        runner = MockRunner.return_value
        runner._detect_commands.return_value = {"test_command": "pytest", "lint_command": "ruff check ."}
        from test_runner import TestResult, LintResult
        runner._run_test.return_value = TestResult(passed=True, total=10, passed_count=10, failed_count=0, output="10 passed")
        runner._run_lint.return_value = LintResult(passed=True, output="ok")
        args = SimpleNamespace(workspace="ws0", path=None, lint=True)
        result = cmd_quick_test(args)
        assert result["ok"] is True
        assert result["data"]["overall"] == "passed"
        assert result["data"]["lint"]["passed"] is True

    @patch("dispatch.TestRunner")
    @patch("dispatch.ConfigManager")
    def test_path_override(self, MockCM, MockRunner):
        MockCM.return_value.get_workspace.return_value = {"path": "/ws"}
        runner = MockRunner.return_value
        runner._detect_commands.return_value = {"test_command": "pytest", "lint_command": None}
        from test_runner import TestResult
        runner._run_test.return_value = TestResult(passed=True, total=5, passed_count=5, failed_count=0, output="5 passed")
        args = SimpleNamespace(workspace="ws0", path="tests/unit/", lint=False)
        result = cmd_quick_test(args)
        assert result["ok"] is True
        # Verify the test command was called with the path appended
        runner._run_test.assert_called_once_with("/ws", "pytest tests/unit/")

    @patch("dispatch.ConfigManager")
    def test_workspace_not_found(self, MockCM):
        MockCM.return_value.get_workspace.return_value = None
        args = SimpleNamespace(workspace="nope", path=None, lint=False)
        result = cmd_quick_test(args)
        assert result["ok"] is False
        assert result["error_code"] == "PATH_NOT_FOUND"


class TestQuickFind:
    @patch("dispatch.subprocess")
    @patch("dispatch.ConfigManager")
    def test_success(self, MockCM, mock_sub):
        MockCM.return_value.get_workspace.return_value = {"path": "/ws"}
        mock_sub.run.return_value = MagicMock(stdout="./file.py:10:match_line\n./file.py:20:another\n")
        args = SimpleNamespace(workspace="ws0", query="match", glob=None)
        result = cmd_quick_find(args)
        assert result["ok"] is True
        assert result["data"]["count"] == 2
        assert result["data"]["truncated"] is False

    @patch("dispatch.subprocess")
    @patch("dispatch.ConfigManager")
    def test_with_glob(self, MockCM, mock_sub):
        MockCM.return_value.get_workspace.return_value = {"path": "/ws"}
        mock_sub.run.return_value = MagicMock(stdout="./a.py:1:hit\n")
        args = SimpleNamespace(workspace="ws0", query="hit", glob="*.py")
        result = cmd_quick_find(args)
        assert result["ok"] is True
        # Verify --include flag was used
        call_args = mock_sub.run.call_args[0][0]
        assert "--include=*.py" in call_args

    @patch("dispatch.subprocess")
    @patch("dispatch.ConfigManager")
    def test_no_matches(self, MockCM, mock_sub):
        MockCM.return_value.get_workspace.return_value = {"path": "/ws"}
        mock_sub.run.return_value = MagicMock(stdout="")
        args = SimpleNamespace(workspace="ws0", query="nonexistent", glob=None)
        result = cmd_quick_find(args)
        assert result["ok"] is True
        assert result["data"]["count"] == 0
        assert result["data"]["matches"] == []

    @patch("dispatch.subprocess")
    @patch("dispatch.ConfigManager")
    def test_timeout(self, MockCM, mock_sub):
        MockCM.return_value.get_workspace.return_value = {"path": "/ws"}
        import subprocess as real_sub
        mock_sub.run.side_effect = real_sub.TimeoutExpired(cmd="grep", timeout=30)
        mock_sub.TimeoutExpired = real_sub.TimeoutExpired
        args = SimpleNamespace(workspace="ws0", query="pattern", glob=None)
        result = cmd_quick_find(args)
        assert result["ok"] is False
        assert result["error_code"] == "TIMEOUT"

    @patch("dispatch.ConfigManager")
    def test_workspace_not_found(self, MockCM):
        MockCM.return_value.get_workspace.return_value = None
        args = SimpleNamespace(workspace="nope", query="x", glob=None)
        result = cmd_quick_find(args)
        assert result["ok"] is False
        assert result["error_code"] == "PATH_NOT_FOUND"


# ── requires_workspace decorator ─────────────────────


class TestRequiresWorkspace:
    @patch("dispatch.ConfigManager")
    def test_workspace_not_found(self, MockCM):
        MockCM.return_value.get_workspace.return_value = None

        @requires_workspace
        def dummy(args):
            return {"ok": True}

        result = dummy(SimpleNamespace(workspace="nope"))
        assert result["ok"] is False
        assert result["error_code"] == "PATH_NOT_FOUND"

    @patch("dispatch.ConfigManager")
    def test_no_session_returns_error(self, MockCM, tmp_path):
        """Workspace exists but no session.json → NO_SESSION."""
        MockCM.return_value.get_workspace.return_value = {"path": str(tmp_path)}

        @requires_workspace
        def dummy(args):
            return {"ok": True}

        result = dummy(SimpleNamespace(workspace="ws0"))
        assert result["ok"] is False
        assert result["error_code"] == "NO_SESSION"

    @patch("dispatch.ConfigManager")
    def test_session_injects_ws_path(self, MockCM, tmp_path):
        """Session exists → args._ws_path is injected from session."""
        ws_path = str(tmp_path)
        _create_session(ws_path)
        MockCM.return_value.get_workspace.return_value = {"path": ws_path}

        captured = {}

        @requires_workspace
        def dummy(args):
            captured["ws_path"] = args._ws_path
            return {"ok": True}

        result = dummy(SimpleNamespace(workspace="ws0"))
        assert result["ok"] is True
        assert captured["ws_path"] == ws_path


# ── quick-env ────────────────────────────────────────


class TestQuickEnv:
    @patch("dispatch.EnvProber")
    @patch("dispatch.ConfigManager")
    def test_success(self, MockCM, MockProber):
        MockProber.return_value.probe.return_value = {"ok": True, "data": {"modules": []}}
        args = SimpleNamespace(env="prod", commands=None)
        result = cmd_quick_env(args)
        assert result["ok"] is True
        MockProber.return_value.probe.assert_called_once_with("prod", extra_commands=None)

    @patch("dispatch.EnvProber")
    @patch("dispatch.ConfigManager")
    def test_with_extra_commands(self, MockCM, MockProber):
        MockProber.return_value.probe.return_value = {"ok": True, "data": {}}
        args = SimpleNamespace(env="prod", commands=["tail -50 /var/log/app.log"])
        result = cmd_quick_env(args)
        assert result["ok"] is True
        MockProber.return_value.probe.assert_called_once_with(
            "prod", extra_commands=["tail -50 /var/log/app.log"]
        )

    @patch("dispatch.EnvProber")
    @patch("dispatch.ConfigManager")
    def test_env_not_found(self, MockCM, MockProber):
        MockProber.return_value.probe.return_value = {
            "ok": False, "error": "env 'nope' not found", "error_code": "PATH_NOT_FOUND"
        }
        args = SimpleNamespace(env="nope", commands=None)
        result = cmd_quick_env(args)
        assert result["ok"] is False
