"""Tests for EnvProber.verify() and cmd_env_verify dispatch handler."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from env_probe import EnvProber, _extract_all_errors, _build_verify_summary


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  _extract_all_errors
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestExtractAllErrors:
    def test_empty_snapshot(self):
        assert _extract_all_errors({}) == []
        assert _extract_all_errors({"modules": []}) == []

    def test_single_module(self):
        snapshot = {
            "modules": [
                {"name": "daemon", "recent_errors": ["ERROR foo", "ERROR bar"]}
            ]
        }
        assert _extract_all_errors(snapshot) == ["ERROR foo", "ERROR bar"]

    def test_multiple_modules(self):
        snapshot = {
            "modules": [
                {"name": "web", "recent_errors": ["ERROR web1"]},
                {"name": "worker", "recent_errors": ["ERROR worker1", "ERROR worker2"]},
            ]
        }
        errors = _extract_all_errors(snapshot)
        assert len(errors) == 3

    def test_module_without_errors_key(self):
        snapshot = {"modules": [{"name": "svc"}]}
        assert _extract_all_errors(snapshot) == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  _build_verify_summary
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBuildVerifySummary:
    def test_all_resolved(self):
        s = _build_verify_summary(["err1", "err2"], [], [])
        assert "Resolved 2" in s

    def test_remaining_errors(self):
        s = _build_verify_summary([], ["err1"], [])
        assert "still present" in s

    def test_new_errors(self):
        s = _build_verify_summary([], [], ["new1"])
        assert "new error" in s

    def test_mixed(self):
        s = _build_verify_summary(["resolved1"], ["remaining1"], ["new1"])
        assert "Resolved 1" in s
        assert "still present" in s
        assert "new" in s

    def test_no_errors_at_all(self):
        s = _build_verify_summary([], [], [])
        assert "No errors" in s


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  EnvProber.verify()
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestEnvProberVerify:
    def test_baseline_not_found(self):
        prober = EnvProber()
        result = prober.verify("some-env", "/nonexistent/path.json")
        assert result["ok"] is False
        assert "baseline" in result["error"]

    def test_baseline_invalid_json(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("not json{{{")
        prober = EnvProber()
        result = prober.verify("some-env", str(bad))
        assert result["ok"] is False
        assert "failed to load baseline" in result["error"]

    def test_probe_failure_propagated(self, tmp_path):
        baseline = tmp_path / "baseline.json"
        baseline.write_text(json.dumps({"modules": []}))

        prober = EnvProber()
        # Mock probe to return failure
        prober.probe = MagicMock(
            return_value={"ok": False, "error": "env not found"}
        )
        result = prober.verify("bad-env", str(baseline))
        assert result["ok"] is False

    def test_all_resolved(self, tmp_path):
        baseline = tmp_path / "baseline.json"
        baseline.write_text(json.dumps({
            "modules": [
                {"name": "daemon", "recent_errors": ["ERROR heartbeat skipped"]}
            ]
        }))

        prober = EnvProber()
        prober.probe = MagicMock(return_value={
            "ok": True,
            "data": {
                "modules": [
                    {"name": "daemon", "recent_errors": []}
                ]
            },
        })

        result = prober.verify("myapp-staging", str(baseline))
        assert result["ok"] is True
        data = result["data"]
        assert data["resolved"] is True
        assert data["baseline_errors"] == ["ERROR heartbeat skipped"]
        assert data["current_errors"] == []
        assert data["resolved_errors"] == ["ERROR heartbeat skipped"]
        assert data["remaining_errors"] == []
        assert data["new_errors"] == []

    def test_errors_still_present(self, tmp_path):
        baseline = tmp_path / "baseline.json"
        baseline.write_text(json.dumps({
            "modules": [
                {"name": "daemon", "recent_errors": ["ERROR heartbeat skipped"]}
            ]
        }))

        prober = EnvProber()
        prober.probe = MagicMock(return_value={
            "ok": True,
            "data": {
                "modules": [
                    {"name": "daemon", "recent_errors": ["ERROR heartbeat skipped"]}
                ]
            },
        })

        result = prober.verify("myapp-staging", str(baseline))
        assert result["ok"] is True
        data = result["data"]
        assert data["resolved"] is False
        assert data["remaining_errors"] == ["ERROR heartbeat skipped"]

    def test_new_errors_appeared(self, tmp_path):
        baseline = tmp_path / "baseline.json"
        baseline.write_text(json.dumps({
            "modules": [
                {"name": "daemon", "recent_errors": ["ERROR old"]}
            ]
        }))

        prober = EnvProber()
        prober.probe = MagicMock(return_value={
            "ok": True,
            "data": {
                "modules": [
                    {"name": "daemon", "recent_errors": ["ERROR new"]}
                ]
            },
        })

        result = prober.verify("myapp-staging", str(baseline))
        assert result["ok"] is True
        data = result["data"]
        assert data["resolved"] is False
        assert data["resolved_errors"] == ["ERROR old"]
        assert data["new_errors"] == ["ERROR new"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  cmd_env_verify (dispatch handler)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCmdEnvVerify:
    @patch("dispatch.ConfigManager")
    def test_workspace_not_found(self, MockCM):
        from dispatch import cmd_env_verify
        MockCM.return_value.get_workspace.return_value = None
        args = SimpleNamespace(workspace="nope", env="dev")
        result = cmd_env_verify(args)
        assert result["ok"] is False
        assert "PATH_NOT_FOUND" in result.get("error_code", "")

    @patch("dispatch.LockFile")
    @patch("dispatch.EnvProber")
    @patch("dispatch.ConfigManager")
    def test_success_saves_artifact(self, MockCM, MockProber, MockLock, tmp_path):
        from dispatch import cmd_env_verify
        from workspace import ARTIFACT_DIR

        ws_path = str(tmp_path)
        art_dir = tmp_path / ARTIFACT_DIR
        art_dir.mkdir(parents=True)
        # Create session.json so @requires_workspace passes
        (art_dir / "session.json").write_text(
            json.dumps({"ws_path": ws_path, "workspace_name": "ws0",
                        "task": "t", "engine": "e", "created_at": ""})
        )
        # Create baseline env_snapshot
        (art_dir / "env_snapshot.json").write_text(
            json.dumps({"modules": [{"name": "d", "recent_errors": ["ERR"]}]})
        )

        MockCM.return_value.get_workspace.return_value = {"path": ws_path}
        MockProber.return_value.verify.return_value = {
            "ok": True,
            "data": {
                "resolved": True,
                "env": "test-env",
                "baseline_errors": ["ERR"],
                "current_errors": [],
            },
        }
        mock_lock = MockLock.return_value
        mock_lock.verify_active.return_value = None
        mock_lock.exists.return_value = True

        args = SimpleNamespace(workspace="ws0", env="test-env")
        result = cmd_env_verify(args)
        assert result["ok"] is True

        # Verify artifact was saved
        report_path = art_dir / "env_verify_report.json"
        assert report_path.exists()
        report = json.loads(report_path.read_text())
        assert report["resolved"] is True
