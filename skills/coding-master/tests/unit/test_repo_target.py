"""Tests for repo_target.py — two-layer model and unified resolver."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from repo_target import (
    RepoTarget,
    WorkspaceContext,
    RepoTargetBinding,
    TestStatus,
    resolve_repo_target,
    resolve_repo_target_for_feature,
    find_active_workspaces_by_repos,
    resolve_test_command,
    run_final_test,
    _resolve_single_repo,
    _load_workspace_snapshot,
)
from workspace import ARTIFACT_DIR


# ── TestStatus ──────────────────────────────────────────


class TestTestStatus:
    def test_passed(self):
        ts = TestStatus(status="passed", reason="success")
        assert ts.passed is True
        assert ts.skipped is False

    def test_failed(self):
        ts = TestStatus(status="failed", reason="command_failed")
        assert ts.passed is False
        assert ts.skipped is False

    def test_skipped(self):
        ts = TestStatus(status="skipped", reason="command_missing")
        assert ts.passed is False
        assert ts.skipped is True


# ── _resolve_single_repo ────────────────────────────────


class TestResolveSingleRepo:
    def test_no_snapshot_uses_ws_root(self, tmp_path):
        name, path = _resolve_single_repo(str(tmp_path), "ws0", None, None)
        assert path == str(tmp_path)

    def test_empty_repos_uses_ws_root(self, tmp_path):
        name, path = _resolve_single_repo(str(tmp_path), "ws0", {"repos": []}, None)
        assert path == str(tmp_path)

    def test_single_repo(self, tmp_path):
        snapshot = {"repos": [{"name": "only", "path": str(tmp_path / "only")}]}
        name, path = _resolve_single_repo(str(tmp_path), "ws0", snapshot, None)
        assert name == "only"

    def test_multi_repo_no_arg(self, tmp_path):
        snapshot = {"repos": [
            {"name": "a", "path": "/a"},
            {"name": "b", "path": "/b"},
        ]}
        result = _resolve_single_repo(str(tmp_path), "ws0", snapshot, None)
        assert isinstance(result, dict)
        assert result["error_code"] == "NEED_EXPLICIT_REPO"

    def test_multi_repo_with_arg(self, tmp_path):
        snapshot = {"repos": [
            {"name": "a", "path": "/a"},
            {"name": "b", "path": "/b"},
        ]}
        name, path = _resolve_single_repo(str(tmp_path), "ws0", snapshot, "b")
        assert name == "b"
        assert path == "/b"

    def test_multi_repo_wrong_arg(self, tmp_path):
        snapshot = {"repos": [
            {"name": "a", "path": "/a"},
            {"name": "b", "path": "/b"},
        ]}
        result = _resolve_single_repo(str(tmp_path), "ws0", snapshot, "nonexistent")
        assert isinstance(result, dict)
        assert result["error_code"] == "PATH_NOT_FOUND"


# ── _load_workspace_snapshot ────────────────────────────


class TestLoadWorkspaceSnapshot:
    def test_missing(self, tmp_path):
        assert _load_workspace_snapshot(str(tmp_path)) is None

    def test_valid(self, tmp_path):
        art_dir = tmp_path / ARTIFACT_DIR
        art_dir.mkdir()
        snapshot = {"repos": [{"name": "x", "path": "/x"}]}
        (art_dir / "workspace_snapshot.json").write_text(json.dumps(snapshot))
        result = _load_workspace_snapshot(str(tmp_path))
        assert result["repos"][0]["name"] == "x"

    def test_invalid_json(self, tmp_path):
        art_dir = tmp_path / ARTIFACT_DIR
        art_dir.mkdir()
        (art_dir / "workspace_snapshot.json").write_text("not json")
        assert _load_workspace_snapshot(str(tmp_path)) is None


# ── resolve_repo_target ─────────────────────────────────


class TestResolveRepoTarget:
    def test_no_args(self):
        from config_manager import ConfigManager
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            cm = ConfigManager(config_path=Path(td) / "cfg.yaml")
            result = resolve_repo_target(config=cm)
            assert isinstance(result, dict)
            assert result["error_code"] == "INVALID_ARGS"

    def test_multi_repo_rejected(self):
        from config_manager import ConfigManager
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            cm = ConfigManager(config_path=Path(td) / "cfg.yaml")
            result = resolve_repo_target(config=cm, repos_arg="a,b")
            assert isinstance(result, dict)
            assert result["error_code"] == "TASK_TOO_COMPLEX"

    def test_workspace_not_found(self):
        from config_manager import ConfigManager
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            cm = ConfigManager(config_path=Path(td) / "cfg.yaml")
            result = resolve_repo_target(config=cm, workspace_arg="nonexistent")
            assert isinstance(result, dict)
            assert result["error_code"] == "PATH_NOT_FOUND"

    def test_workspace_with_session(self, tmp_path):
        """Full resolution from workspace with session + snapshot."""
        from config_manager import ConfigManager
        import yaml

        ws_path = tmp_path / "ws"
        ws_path.mkdir()
        art_dir = ws_path / ARTIFACT_DIR
        art_dir.mkdir()

        # Session
        session = {"ws_path": str(ws_path), "workspace_name": "test-ws",
                   "task": "t", "engine": "e", "created_at": ""}
        (art_dir / "session.json").write_text(json.dumps(session))

        # Snapshot (single repo)
        repo_path = str(ws_path / "myrepo")
        Path(repo_path).mkdir()
        snapshot = {"repos": [{"name": "myrepo", "path": repo_path}]}
        (art_dir / "workspace_snapshot.json").write_text(json.dumps(snapshot))

        # Config
        cfg_path = tmp_path / "cfg.yaml"
        cfg_data = {"coding_master": {"workspaces": {"test-ws": str(ws_path)}}}
        cfg_path.write_text(yaml.dump(cfg_data))
        cm = ConfigManager(config_path=cfg_path)

        binding = resolve_repo_target(config=cm, workspace_arg="test-ws")
        assert isinstance(binding, RepoTargetBinding)
        assert binding.workspace.name == "test-ws"
        assert binding.target.repo_name == "myrepo"
        assert binding.target.repo_path == repo_path


# ── resolve_test_command ────────────────────────────────


class TestResolveTestCommand:
    def test_no_pyproject(self, tmp_path):
        from config_manager import ConfigManager
        cm = ConfigManager(config_path=tmp_path / "cfg.yaml")
        result = resolve_test_command(str(tmp_path), "myrepo", cm, "ws0")
        assert result is None

    def test_repo_config_fallback(self, tmp_path):
        from config_manager import ConfigManager
        import yaml

        cfg_path = tmp_path / "cfg.yaml"
        cfg_path.write_text(yaml.dump({
            "coding_master": {
                "repos": {
                    "myrepo": {
                        "url": str(tmp_path),
                        "test_command": "make test",
                    }
                }
            }
        }))
        cm = ConfigManager(config_path=cfg_path)
        result = resolve_test_command(str(tmp_path), "myrepo", cm, "ws0")
        assert result == "make test"

    def test_pyproject_exists(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname = \"x\"\n")
        from config_manager import ConfigManager
        cm = ConfigManager(config_path=tmp_path / "cfg.yaml")
        result = resolve_test_command(str(tmp_path), "myrepo", cm, "ws0")
        assert result is not None  # Should detect pytest


# ── run_final_test ──────────────────────────────────────


class TestRunFinalTest:
    def test_no_test_command(self):
        target = RepoTarget(
            repo_name="x", repo_path="/x",
            test_command=None, git_root="/x",
        )
        from config_manager import ConfigManager
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            cm = ConfigManager(config_path=Path(td) / "cfg.yaml")
            status = run_final_test(target, cm)
            assert status.status == "skipped"
            assert status.reason == "command_missing"

    def test_command_fails(self, tmp_path):
        target = RepoTarget(
            repo_name="x", repo_path=str(tmp_path),
            test_command="false",  # always exits 1
            git_root=str(tmp_path),
        )
        from config_manager import ConfigManager
        cm = ConfigManager(config_path=tmp_path / "cfg.yaml")
        status = run_final_test(target, cm)
        assert status.status == "failed"
        assert status.reason == "command_failed"

    def test_command_passes(self, tmp_path):
        target = RepoTarget(
            repo_name="x", repo_path=str(tmp_path),
            test_command="true",  # always exits 0
            git_root=str(tmp_path),
        )
        from config_manager import ConfigManager
        cm = ConfigManager(config_path=tmp_path / "cfg.yaml")
        status = run_final_test(target, cm)
        assert status.status == "passed"
        assert status.reason == "success"


# ── find_active_workspaces_by_repos ─────────────────────


class TestFindActiveWorkspacesByRepos:
    def test_no_workspaces(self):
        from config_manager import ConfigManager
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            cm = ConfigManager(config_path=Path(td) / "cfg.yaml")
            result = find_active_workspaces_by_repos(cm, ["myrepo"])
            assert result == []
