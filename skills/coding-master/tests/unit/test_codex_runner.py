"""Tests for CodexRunner engine."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch, MagicMock

import pytest

from engine.codex_runner import CodexRunner, _get_changed_files


class TestCodexRunner:
    def setup_method(self):
        self.runner = CodexRunner()

    @patch("engine.codex_runner._get_changed_files", return_value=["file.py"])
    @patch("engine.codex_runner.subprocess.run")
    def test_success(self, mock_run, mock_files):
        jsonl_output = json.dumps({"message": "Fixed the bug in main.py"})
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=jsonl_output,
            stderr="",
        )

        result = self.runner.run("/repo", "fix the bug")

        assert result.success is True
        assert "Fixed the bug" in result.summary
        assert result.files_changed == ["file.py"]
        assert result.error is None

        # Verify command structure
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "codex"
        assert "exec" in cmd
        assert "--full-auto" in cmd
        assert "--json" in cmd
        assert "-C" in cmd
        assert "/repo" in cmd

    @patch("engine.codex_runner.subprocess.run")
    def test_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="codex", timeout=600)

        result = self.runner.run("/repo", "fix bug", timeout=600)

        assert result.success is False
        assert "timed out" in result.error

    @patch("engine.codex_runner.subprocess.run")
    def test_not_found(self, mock_run):
        mock_run.side_effect = FileNotFoundError()

        result = self.runner.run("/repo", "fix bug")

        assert result.success is False
        assert "not found" in result.error

    @patch("engine.codex_runner._get_changed_files", return_value=[])
    @patch("engine.codex_runner.subprocess.run")
    def test_nonzero_exit_with_output(self, mock_run, mock_files):
        """Non-zero exit but with stdout — still returns success with summary."""
        jsonl_output = json.dumps({"message": "Partial result"})
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout=jsonl_output,
            stderr="some warning",
        )

        result = self.runner.run("/repo", "fix bug")

        # Has summary, so treated as success
        assert result.success is True
        assert "Partial result" in result.summary

    @patch("engine.codex_runner._get_changed_files", return_value=[])
    @patch("engine.codex_runner.subprocess.run")
    def test_nonzero_exit_no_output(self, mock_run, mock_files):
        """Non-zero exit with no stdout — returns failure."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="fatal error",
        )

        result = self.runner.run("/repo", "fix bug")

        assert result.success is False
        assert "exited with code 1" in result.error

    @patch("engine.codex_runner._get_changed_files", return_value=[])
    @patch("engine.codex_runner.subprocess.run")
    def test_multi_line_jsonl(self, mock_run, mock_files):
        """Multiple JSONL lines — last one is used as summary."""
        lines = "\n".join([
            json.dumps({"type": "start"}),
            json.dumps({"type": "progress", "content": "working..."}),
            json.dumps({"message": "Final answer here"}),
        ])
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=lines,
            stderr="",
        )

        result = self.runner.run("/repo", "fix bug")

        assert result.success is True
        assert "Final answer" in result.summary

    @patch("engine.codex_runner._get_changed_files", return_value=[])
    @patch("engine.codex_runner.subprocess.run")
    def test_invalid_json_fallback(self, mock_run, mock_files):
        """Invalid JSON output — falls back to raw stdout."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="not json at all",
            stderr="",
        )

        result = self.runner.run("/repo", "fix bug")

        assert result.success is True
        assert "not json at all" in result.summary


class TestGetChangedFiles:
    @patch("engine.codex_runner.subprocess.run")
    def test_detects_changes(self, mock_run):
        mock_run.side_effect = [
            MagicMock(stdout="a.py\nb.py\n"),  # unstaged
            MagicMock(stdout="b.py\nc.py\n"),  # staged
        ]

        files = _get_changed_files("/repo")

        assert files == ["a.py", "b.py", "c.py"]

    @patch("engine.codex_runner.subprocess.run")
    def test_exception_returns_empty(self, mock_run):
        mock_run.side_effect = Exception("git broken")

        files = _get_changed_files("/repo")

        assert files == []
