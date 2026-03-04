"""Tests for lifecycle.py — start, stop, restart."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from lifecycle import cmd_start, cmd_stop, cmd_restart, _find_everbot_bin


class TestFindEverbotBin:
    def test_found_via_status(self, alfred_home, running_status, tmp_path):
        # Create a fake bin/everbot at the project_root path
        project_root = tmp_path / "project"
        (project_root / "bin").mkdir(parents=True)
        (project_root / "bin" / "everbot").write_text("#!/bin/bash\n")

        # Update status with real project_root
        status = json.loads((alfred_home / "everbot.status.json").read_text())
        status["project_root"] = str(project_root)
        (alfred_home / "everbot.status.json").write_text(json.dumps(status))

        result = _find_everbot_bin(alfred_home)
        assert result == str(project_root / "bin" / "everbot")

    def test_not_found_no_status(self, alfred_home):
        assert _find_everbot_bin(alfred_home) is None

    def test_not_found_no_project_root(self, alfred_home, stopped_status):
        # Clear project_root
        status = json.loads((alfred_home / "everbot.status.json").read_text())
        status["project_root"] = ""
        (alfred_home / "everbot.status.json").write_text(json.dumps(status))
        assert _find_everbot_bin(alfred_home) is None


class TestCmdStart:
    def test_already_running(self, alfred_home, running_status):
        result = cmd_start(alfred_home)
        assert result["ok"] is True
        assert "already running" in result["data"]["message"]

    def test_no_everbot_bin(self, alfred_home):
        result = cmd_start(alfred_home)
        assert result["ok"] is False
        assert "cannot locate" in result["error"]

    @patch("lifecycle._run_everbot")
    def test_start_success(self, mock_run, alfred_home, stopped_status, tmp_path):
        # Setup fake bin/everbot
        project_root = tmp_path / "project"
        (project_root / "bin").mkdir(parents=True)
        (project_root / "bin" / "everbot").write_text("#!/bin/bash\n")
        status = json.loads((alfred_home / "everbot.status.json").read_text())
        status["project_root"] = str(project_root)
        (alfred_home / "everbot.status.json").write_text(json.dumps(status))

        mock_run.return_value = {"exit_code": 0, "stdout": "started", "stderr": ""}
        result = cmd_start(alfred_home)
        assert result["ok"] is True
        mock_run.assert_called_once()


class TestCmdStop:
    def test_not_running_no_bin(self, alfred_home):
        result = cmd_stop(alfred_home)
        assert result["ok"] is True
        assert "not running" in result["data"]["message"]

    @patch("lifecycle._run_everbot")
    def test_stop_success(self, mock_run, alfred_home, running_status, tmp_path):
        project_root = tmp_path / "project"
        (project_root / "bin").mkdir(parents=True)
        (project_root / "bin" / "everbot").write_text("#!/bin/bash\n")
        status = json.loads((alfred_home / "everbot.status.json").read_text())
        status["project_root"] = str(project_root)
        (alfred_home / "everbot.status.json").write_text(json.dumps(status))

        mock_run.return_value = {"exit_code": 0, "stdout": "stopped", "stderr": ""}
        result = cmd_stop(alfred_home)
        assert result["ok"] is True


class TestCmdRestart:
    def test_no_everbot_bin(self, alfred_home):
        result = cmd_restart(alfred_home)
        assert result["ok"] is False
        assert "cannot locate" in result["error"]
