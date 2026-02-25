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
    cmd_config_list,
    cmd_config_add,
    cmd_config_set,
    cmd_config_remove,
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
        args = SimpleNamespace(workspace="ws0", task="build feature", engine=None)
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
    def test_feature_plan(self, MockCM, MockFM):
        MockCM.return_value.get_workspace.return_value = {"path": "/ws"}
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
    def test_feature_next(self, MockCM, MockFM):
        MockCM.return_value.get_workspace.return_value = {"path": "/ws"}
        MockFM.return_value.next_feature.return_value = {"ok": True, "data": {"index": 0}}
        args = SimpleNamespace(workspace="ws0")
        result = cmd_feature_next(args)
        assert result["ok"] is True

    @patch("dispatch.FeatureManager")
    @patch("dispatch.ConfigManager")
    def test_feature_done(self, MockCM, MockFM):
        MockCM.return_value.get_workspace.return_value = {"path": "/ws"}
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
    def test_feature_update(self, MockCM, MockFM):
        MockCM.return_value.get_workspace.return_value = {"path": "/ws"}
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
