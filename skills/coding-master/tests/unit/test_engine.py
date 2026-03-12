"""Unit tests for engine.py — CodingEngine abstraction and ClaudeCodeEngine."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure the scripts module is importable
_scripts_dir = str(Path(__file__).resolve().parents[2] / "scripts")
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from engine import (
    ClaudeCodeEngine,
    CodingEngine,
    EngineResult,
    get_engine,
    MODE_PROMPTS,
    MODE_TOOLS,
)


# ===========================================================================
# EngineResult
# ===========================================================================


class TestEngineResult:
    def test_defaults(self):
        r = EngineResult()
        assert r.ok is True
        assert r.summary == ""
        assert r.files_analyzed == []
        assert r.files_changed == []
        assert r.findings == []
        assert r.error == ""
        assert r.engine == ""
        assert r.turns_used == 0

    def test_to_dict(self):
        r = EngineResult(
            ok=True,
            summary="All good",
            files_analyzed=["a.py", "b.py"],
            findings=[{"file": "a.py", "severity": "info", "description": "ok"}],
            engine="claude-code",
            turns_used=5,
        )
        d = r.to_dict()
        assert d["ok"] is True
        assert d["summary"] == "All good"
        assert len(d["files_analyzed"]) == 2
        assert len(d["findings"]) == 1
        assert d["engine"] == "claude-code"
        assert d["turns_used"] == 5
        assert d["files_changed"] == []
        assert d["error"] == ""

    def test_error_result(self):
        r = EngineResult(ok=False, error="timeout", engine="claude-code")
        d = r.to_dict()
        assert d["ok"] is False
        assert d["error"] == "timeout"


# ===========================================================================
# ClaudeCodeEngine
# ===========================================================================


class TestClaudeCodeEngine:
    def test_name(self):
        e = ClaudeCodeEngine()
        assert e.name() == "claude-code"

    @patch("shutil.which", return_value="/usr/local/bin/claude")
    def test_is_available_true(self, mock_which):
        e = ClaudeCodeEngine()
        assert e.is_available() is True
        mock_which.assert_called_with("claude")

    @patch("shutil.which", return_value=None)
    def test_is_available_false(self, mock_which):
        e = ClaudeCodeEngine()
        assert e.is_available() is False

    @patch("shutil.which", return_value=None)
    def test_run_not_available(self, mock_which):
        e = ClaudeCodeEngine()
        result = e.run("test prompt", "/tmp/repo")
        assert result.ok is False
        assert "not found" in result.error

    @patch("subprocess.run")
    @patch("shutil.which", return_value="/usr/local/bin/claude")
    def test_run_success(self, mock_which, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({
                "result": json.dumps({
                    "summary": "No issues found",
                    "findings": [],
                    "files_analyzed": ["main.py"],
                }),
                "num_turns": 3,
            }),
            stderr="",
        )
        e = ClaudeCodeEngine()
        result = e.run("review this", "/tmp/repo", mode="review")
        assert result.ok is True
        assert result.summary == "No issues found"
        assert result.files_analyzed == ["main.py"]
        assert result.turns_used == 3

    @patch("subprocess.run")
    @patch("shutil.which", return_value="/usr/local/bin/claude")
    def test_run_timeout(self, mock_which, mock_run):
        import subprocess as sp
        mock_run.side_effect = sp.TimeoutExpired(cmd="claude", timeout=600)
        e = ClaudeCodeEngine()
        result = e.run("review", "/tmp/repo", timeout=600)
        assert result.ok is False
        assert "timed out" in result.error

    @patch("subprocess.run")
    @patch("shutil.which", return_value="/usr/local/bin/claude")
    def test_run_nonzero_exit(self, mock_which, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Error: something went wrong",
        )
        e = ClaudeCodeEngine()
        result = e.run("review", "/tmp/repo")
        assert result.ok is False
        assert "exited with code 1" in result.error

    @patch("subprocess.run")
    @patch("shutil.which", return_value="/usr/local/bin/claude")
    def test_run_plain_text_output(self, mock_which, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="This is plain text, not JSON",
            stderr="",
        )
        e = ClaudeCodeEngine()
        result = e.run("review", "/tmp/repo")
        assert result.ok is True
        assert "plain text" in result.summary

    @patch("subprocess.run")
    @patch("shutil.which", return_value="/usr/local/bin/claude")
    def test_run_uses_correct_tools(self, mock_which, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout='{"result": "ok"}', stderr=""
        )
        e = ClaudeCodeEngine()
        e.run("test", "/tmp/repo", mode="review")

        call_args = mock_run.call_args[0][0]
        tools_idx = call_args.index("--allowedTools")
        assert call_args[tools_idx + 1] == "Read,Glob,Grep"

    @patch("subprocess.run")
    @patch("shutil.which", return_value="/usr/local/bin/claude")
    def test_run_debug_mode_tools(self, mock_which, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout='{"result": "ok"}', stderr=""
        )
        e = ClaudeCodeEngine()
        e.run("test", "/tmp/repo", mode="debug")

        call_args = mock_run.call_args[0][0]
        tools_idx = call_args.index("--allowedTools")
        assert "Bash" in call_args[tools_idx + 1]


# ===========================================================================
# get_engine factory
# ===========================================================================


class TestGetEngine:
    def test_valid_engine(self):
        engine = get_engine("claude-code")
        assert isinstance(engine, ClaudeCodeEngine)

    def test_invalid_engine(self):
        with pytest.raises(ValueError, match="Unknown engine"):
            get_engine("nonexistent-engine")

    def test_default_engine(self):
        engine = get_engine()
        assert isinstance(engine, ClaudeCodeEngine)


# ===========================================================================
# Mode configuration
# ===========================================================================


class TestModeConfig:
    def test_all_modes_have_prompts(self):
        for mode in ("review", "analyze", "debug", "deliver"):
            assert mode in MODE_PROMPTS

    def test_all_modes_have_tools(self):
        for mode in ("review", "analyze", "debug", "deliver"):
            assert mode in MODE_TOOLS

    def test_review_tools_readonly(self):
        tools = MODE_TOOLS["review"]
        assert "Edit" not in tools
        assert "Write" not in tools
        assert "Read" in tools
