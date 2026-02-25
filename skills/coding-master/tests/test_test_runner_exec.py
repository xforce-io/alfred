"""Tests for test_runner.py — execution paths with mocked subprocess."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from test_runner import TestRunner, _exec, _truncate, _has_tool, _parse_pytest_output


def _run_ok(stdout="", stderr="", rc=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=rc)


# ── _exec ────────────────────────────────────────────────


class TestExec:
    @patch("subprocess.run", return_value=_run_ok(stdout="ok\n", stderr="warn"))
    def test_normal_run(self, mock_run):
        out, err, rc = _exec("/tmp", "pytest")
        assert out == "ok\n"
        assert err == "warn"
        assert rc == 0

    @patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="x", timeout=300))
    def test_timeout(self, mock_run):
        out, err, rc = _exec("/tmp", "pytest")
        assert out == ""
        assert "timeout" in err
        assert rc == 1

    @patch("subprocess.run", side_effect=OSError("no such file"))
    def test_generic_exception(self, mock_run):
        out, err, rc = _exec("/tmp", "bad-cmd")
        assert out == ""
        assert "no such file" in err
        assert rc == 1


# ── _truncate ────────────────────────────────────────────


class TestTruncate:
    def test_under_limit(self):
        assert _truncate("short", 100) == "short"

    def test_over_limit(self):
        result = _truncate("A" * 200, 50)
        assert len(result) < 200
        assert "(truncated)" in result


# ── _has_tool ────────────────────────────────────────────


class TestHasTool:
    def test_found(self, tmp_path):
        f = tmp_path / "pyproject.toml"
        f.write_text("[tool.ruff]\nline-length = 88\n")
        assert _has_tool(f, "ruff") is True

    def test_not_found(self, tmp_path):
        f = tmp_path / "pyproject.toml"
        f.write_text("[project]\nname = 'x'\n")
        assert _has_tool(f, "ruff") is False

    def test_file_missing(self, tmp_path):
        f = tmp_path / "nonexistent.toml"
        assert _has_tool(f, "ruff") is False


# ── _parse_pytest_output (additional edge cases) ────────


class TestParsePytestOutputExtra:
    def test_with_errors(self):
        total, p, f = _parse_pytest_output("3 passed, 1 failed, 2 error")
        assert p == 3
        assert f == 1
        assert total == 6  # 3+1 + 2 errors

    def test_only_errors(self):
        total, p, f = _parse_pytest_output("5 error")
        assert total == 5
        assert p == 0
        assert f == 0


# ── TestRunner._detect_commands ──────────────────────────


class TestDetectCommands:
    def _make_runner(self):
        return TestRunner(config=MagicMock())

    def test_configured_commands_take_priority(self):
        runner = self._make_runner()
        ws_config = {"test_command": "make test", "lint_command": "make lint"}
        result = runner._detect_commands("/tmp/ws", ws_config)
        assert result["test_command"] == "make test"
        assert result["lint_command"] == "make lint"

    def test_python_project(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n")
        runner = self._make_runner()
        result = runner._detect_commands(str(tmp_path), {})
        assert result["test_command"] == "pytest"
        assert result["lint_command"] == "ruff check ."

    def test_python_project_without_ruff(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        runner = self._make_runner()
        result = runner._detect_commands(str(tmp_path), {})
        assert result["test_command"] == "pytest"
        assert result["lint_command"] is None

    def test_node_project(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        runner = self._make_runner()
        result = runner._detect_commands(str(tmp_path), {})
        assert result["test_command"] == "npm test"
        assert result["lint_command"] == "npm run lint"

    def test_rust_project(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]\n")
        runner = self._make_runner()
        result = runner._detect_commands(str(tmp_path), {})
        assert result["test_command"] == "cargo test"
        assert result["lint_command"] == "cargo clippy"

    def test_unknown_project(self, tmp_path):
        runner = self._make_runner()
        result = runner._detect_commands(str(tmp_path), {})
        assert result["test_command"] is None
        assert result["lint_command"] is None


# ── TestRunner._run_lint ─────────────────────────────────


class TestRunLint:
    def _make_runner(self):
        return TestRunner(config=MagicMock())

    def test_no_command(self):
        runner = self._make_runner()
        result = runner._run_lint("/tmp", None)
        assert result.passed is True
        assert "no lint command" in result.output

    @patch("test_runner._exec", return_value=("all clean\n", "", 0))
    def test_lint_pass(self, mock_exec):
        runner = self._make_runner()
        result = runner._run_lint("/tmp/ws", "ruff check .")
        assert result.passed is True
        assert "all clean" in result.output

    @patch("test_runner._exec", return_value=("", "error at line 5", 1))
    def test_lint_fail(self, mock_exec):
        runner = self._make_runner()
        result = runner._run_lint("/tmp/ws", "ruff check .")
        assert result.passed is False


# ── TestRunner._run_test ─────────────────────────────────


class TestRunTest:
    def _make_runner(self):
        return TestRunner(config=MagicMock())

    def test_no_command(self):
        runner = self._make_runner()
        result = runner._run_test("/tmp", None)
        assert result.passed is True
        assert result.total == 0

    @patch("test_runner._exec", return_value=("10 passed\n", "", 0))
    def test_all_passed(self, mock_exec):
        runner = self._make_runner()
        result = runner._run_test("/tmp/ws", "pytest")
        assert result.passed is True
        assert result.passed_count == 10
        assert result.total == 10

    @patch("test_runner._exec", return_value=("8 passed, 2 failed\n", "", 1))
    def test_some_failed(self, mock_exec):
        runner = self._make_runner()
        result = runner._run_test("/tmp/ws", "pytest")
        assert result.passed is False
        assert result.failed_count == 2
        assert result.total == 10


# ── TestRunner.run (full orchestration) ──────────────────


class TestRunnerRun:
    def test_workspace_not_found(self):
        config = MagicMock()
        config.get_workspace.return_value = None
        runner = TestRunner(config=config)
        result = runner.run("nonexistent")
        assert result["ok"] is False
        assert "not found" in result["error"]

    @patch("test_runner._exec", return_value=("5 passed\n", "", 0))
    def test_full_run_success(self, mock_exec, tmp_path):
        ws_path = str(tmp_path)
        (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n")

        config = MagicMock()
        config.get_workspace.return_value = {"path": ws_path}

        runner = TestRunner(config=config)
        result = runner.run("ws0")

        assert result["ok"] is True
        assert result["data"]["overall"] == "passed"
        # Artifact file should be written
        report_file = tmp_path / ".coding-master" / "test_report.json"
        assert report_file.exists()
        report = json.loads(report_file.read_text())
        assert report["overall"] == "passed"

    @patch("test_runner._exec")
    def test_lint_fails_overall_fails(self, mock_exec, tmp_path):
        ws_path = str(tmp_path)
        (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n")

        # First call is lint (fails), second call is test (passes)
        mock_exec.side_effect = [
            ("", "lint error", 1),     # lint
            ("3 passed\n", "", 0),     # test
        ]

        config = MagicMock()
        config.get_workspace.return_value = {"path": ws_path}

        runner = TestRunner(config=config)
        result = runner.run("ws0")

        assert result["ok"] is True
        assert result["data"]["overall"] == "failed"
        assert result["data"]["lint"]["passed"] is False
        assert result["data"]["test"]["passed"] is True
