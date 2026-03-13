"""Unit tests for CodingMasterSkillkit."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure the skillkit module is importable
_skill_dir = str(Path(__file__).resolve().parents[2])
if _skill_dir not in sys.path:
    sys.path.insert(0, _skill_dir)

from coding_master_skillkit import (
    CodingMasterSkillkit,
    _GIT_ALLOWED,
    _make_args,
)


# ===========================================================================
# _make_args helper
# ===========================================================================


class TestMakeArgs:
    def test_defaults(self):
        args = _make_args()
        assert args.repo is None
        assert args.agent is None
        assert args.mode == "deliver"
        assert args.force is False

    def test_override(self):
        args = _make_args(repo="myrepo", feature=3, mode="review")
        assert args.repo == "myrepo"
        assert args.feature == 3
        assert args.mode == "review"


# ===========================================================================
# _createSkills
# ===========================================================================


class TestCreateSkills:
    def test_creates_expected_skills(self):
        sk = CodingMasterSkillkit(agent_id="test")
        skills = sk._createSkills()
        names = {s.get_function_name() for s in skills}

        # Core tools exist
        assert "_cm_repos" in names
        assert "_cm_start" in names
        assert "_cm_lock" in names
        assert "_cm_unlock" in names
        assert "_cm_status" in names
        assert "_cm_claim" in names
        assert "_cm_dev" in names
        assert "_cm_test" in names
        assert "_cm_done" in names
        assert "_cm_reopen" in names
        assert "_cm_integrate" in names
        assert "_cm_submit" in names
        assert "_cm_scope" in names
        assert "_cm_report" in names
        assert "_cm_engine_run" in names
        assert "_cm_progress" in names
        assert "_cm_journal" in names
        assert "_cm_doctor" in names
        assert "_cm_git" in names
        assert "_cm_engine_run" in names

        # v4.5: file operation tools added
        assert "_cm_read" in names
        assert "_cm_grep" in names
        assert "_cm_find" in names
        assert "_cm_edit" in names

        # No _bash or _python tool
        assert "_bash" not in names
        assert "_python" not in names

    def test_skill_count(self):
        sk = CodingMasterSkillkit(agent_id="test")
        skills = sk._createSkills()
        assert len(skills) == 24  # 19 original + 4 file ops + change-summary

    def test_get_name(self):
        sk = CodingMasterSkillkit()
        assert sk.getName() == "coding_master"


# ===========================================================================
# _cm_git validation
# ===========================================================================


class TestCmGit:
    @pytest.fixture
    def sk(self):
        return CodingMasterSkillkit(agent_id="test")

    def test_forbidden_subcmd(self, sk):
        result = json.loads(sk._cm_git(subcmd="reflog"))
        assert result["ok"] is False
        assert "not allowed" in result["error"]

    def test_allowed_subcmds_set(self):
        for cmd in ["add", "commit", "diff", "log", "push", "status", "branch"]:
            assert cmd in _GIT_ALLOWED

    @patch("subprocess.run")
    def test_git_status_success(self, mock_run, sk):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="On branch main\n", stderr=""
        )
        sk._resolve_session_cwd = MagicMock(return_value="/tmp/repo")
        result = json.loads(sk._cm_git(subcmd="status"))
        assert result["ok"] is True
        assert "On branch main" in result["data"]["stdout"]

    def test_no_session_returns_error(self, sk):
        sk._resolve_session_cwd = MagicMock(return_value=None)
        result = json.loads(sk._cm_git(subcmd="status"))
        assert result["ok"] is False
        assert "No active session" in result["error"]

    @patch("subprocess.run")
    def test_explicit_cwd(self, mock_run, sk):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        result = json.loads(sk._cm_git(subcmd="log", args="--oneline -5", cwd="/my/repo"))
        assert result["ok"] is True
        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args
        assert call_kwargs.kwargs["cwd"] == "/my/repo"


# ===========================================================================
# Tool delegation to cmd_* functions
# ===========================================================================


class TestToolDelegation:
    @pytest.fixture
    def sk(self):
        return CodingMasterSkillkit(agent_id="test-agent")

    @patch("coding_master_skillkit._get_tools")
    def test_cm_repos(self, mock_tools, sk):
        mock_tools.return_value.cmd_repos.return_value = {"ok": True, "data": {"repos": {}}}
        result = json.loads(sk._cm_repos())
        assert result["ok"] is True

    @patch("coding_master_skillkit._get_tools")
    def test_cm_lock(self, mock_tools, sk):
        mock_tools.return_value.cmd_lock.return_value = {
            "ok": True, "data": {"branch": "dev/test", "mode": "deliver"}
        }
        result = json.loads(sk._cm_lock(repo="myrepo", mode="review"))
        assert result["ok"] is True
        call_args = mock_tools.return_value.cmd_lock.call_args[0][0]
        assert call_args.repo == "myrepo"
        assert call_args.mode == "review"
        assert call_args.agent == "test-agent"

    @patch("coding_master_skillkit._get_tools")
    def test_cm_claim(self, mock_tools, sk):
        mock_tools.return_value.cmd_claim.return_value = {
            "ok": True, "data": {"feature": 1, "branch": "feat/1-do-thing"}
        }
        result = json.loads(sk._cm_claim(repo="myrepo", feature=1))
        assert result["ok"] is True
        call_args = mock_tools.return_value.cmd_claim.call_args[0][0]
        assert call_args.feature == 1

    @patch("coding_master_skillkit._get_tools")
    def test_cm_submit(self, mock_tools, sk):
        mock_tools.return_value.cmd_submit.return_value = {
            "ok": True, "data": {"pr_url": "https://github.com/org/repo/pull/42"}
        }
        result = json.loads(sk._cm_submit(repo="myrepo", title="Add feature X"))
        assert result["ok"] is True
        call_args = mock_tools.return_value.cmd_submit.call_args[0][0]
        assert call_args.title == "Add feature X"

    @patch("coding_master_skillkit._get_tools")
    def test_cm_journal(self, mock_tools, sk):
        mock_tools.return_value.cmd_journal.return_value = {"ok": True}
        result = json.loads(sk._cm_journal(message="Started debugging", repo="myrepo"))
        assert result["ok"] is True
        call_args = mock_tools.return_value.cmd_journal.call_args[0][0]
        assert call_args.message == "Started debugging"
