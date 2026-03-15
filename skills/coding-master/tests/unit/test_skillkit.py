"""Unit tests for CodingMasterSkillkit — v5.0 two-tier architecture."""

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
# _createSkills — v5.0: 7 agent-facing tools only
# ===========================================================================


class TestCreateSkills:
    def test_creates_expected_skills(self):
        sk = CodingMasterSkillkit(agent_id="test")
        skills = sk._createSkills()
        names = {s.get_function_name() for s in skills}

        # v5.0 agent-facing tools
        assert "_cm_next" in names       # workflow autopilot
        assert "_cm_edit" in names       # file editing
        assert "_cm_read" in names       # read files
        assert "_cm_find" in names       # find files
        assert "_cm_grep" in names       # search content
        assert "_cm_status" in names     # status + progress + repos
        assert "_cm_doctor" in names     # diagnose + fix

    def test_internal_tools_not_exposed(self):
        """Internal pipeline tools must NOT be registered as agent-facing tools."""
        sk = CodingMasterSkillkit(agent_id="test")
        skills = sk._createSkills()
        names = {s.get_function_name() for s in skills}

        internal_tools = [
            "_cm_session_start", "_cm_session_lock", "_cm_session_unlock",
            "_cm_feat_claim", "_cm_feat_dev", "_cm_feat_test",
            "_cm_feat_done", "_cm_feat_reopen",
            "_cm_session_integrate", "_cm_session_submit",
            "_cm_dev_git", "_cm_dev_journal",
            "_cm_review_scope", "_cm_review_engine", "_cm_review_report",
            "_cm_regression", "_cm_change_summary", "_cm_progress",
            "_cm_repos",
        ]
        for tool in internal_tools:
            assert tool not in names, f"{tool} should be internal, not agent-facing"

    def test_skill_count(self):
        """v5.0: exactly 7 agent-facing tools."""
        sk = CodingMasterSkillkit(agent_id="test")
        skills = sk._createSkills()
        assert len(skills) == 7

    def test_no_bash_or_python(self):
        sk = CodingMasterSkillkit(agent_id="test")
        skills = sk._createSkills()
        names = {s.get_function_name() for s in skills}
        assert "_bash" not in names
        assert "_python" not in names

    def test_get_name(self):
        sk = CodingMasterSkillkit()
        assert sk.getName() == "coding_master"


# ===========================================================================
# _cm_next delegation
# ===========================================================================


class TestCmNext:
    @pytest.fixture
    def sk(self):
        return CodingMasterSkillkit(agent_id="test-agent")

    @patch("coding_master_skillkit._get_tools")
    def test_cm_next_delegates_to_cmd_next(self, mock_tools, sk):
        mock_tools.return_value.cmd_next.return_value = {
            "ok": True,
            "breakpoint": "write_plan",
            "instruction": "Create PLAN.md",
        }
        result = json.loads(sk._cm_next(repo="myrepo"))
        assert result["ok"] is True
        assert result["breakpoint"] == "write_plan"
        call_args = mock_tools.return_value.cmd_next.call_args[0][0]
        assert call_args.repo == "myrepo"

    @patch("coding_master_skillkit._get_tools")
    def test_cm_next_passes_intent(self, mock_tools, sk):
        mock_tools.return_value.cmd_next.return_value = {
            "ok": False, "breakpoint": "fix_code", "test_output": "FAILED"
        }
        result = json.loads(sk._cm_next(repo="myrepo", intent="test"))
        call_args = mock_tools.return_value.cmd_next.call_args[0][0]
        assert call_args.intent == "test"

    @patch("coding_master_skillkit._get_tools")
    def test_cm_next_passes_mode(self, mock_tools, sk):
        mock_tools.return_value.cmd_next.return_value = {
            "ok": True, "breakpoint": "define_scope"
        }
        sk._cm_next(repo="myrepo", mode="review")
        call_args = mock_tools.return_value.cmd_next.call_args[0][0]
        assert call_args.mode == "review"


# ===========================================================================
# _cm_edit delegation
# ===========================================================================


class TestCmEdit:
    @pytest.fixture
    def sk(self):
        return CodingMasterSkillkit(agent_id="test-agent")

    @patch("coding_master_skillkit._get_tools")
    def test_cm_edit_delegates_to_cmd_edit(self, mock_tools, sk):
        mock_tools.return_value.cmd_edit.return_value = {
            "ok": True, "data": {"file": ".coding-master/PLAN.md", "replacements": 1}
        }
        result = json.loads(sk._cm_edit(
            repo="myrepo", file=".coding-master/PLAN.md",
            old_text="", new_text="# Plan\n"
        ))
        assert result["ok"] is True
        call_args = mock_tools.return_value.cmd_edit.call_args[0][0]
        assert call_args.file == ".coding-master/PLAN.md"
        assert call_args.old_text == ""
        assert call_args.new_text == "# Plan\n"


# ===========================================================================
# _cm_status (merged status + progress + repos)
# ===========================================================================


class TestCmStatus:
    @pytest.fixture
    def sk(self):
        return CodingMasterSkillkit(agent_id="test-agent")

    @patch("coding_master_skillkit._get_tools")
    def test_cm_status_no_repo_lists_repos(self, mock_tools, sk):
        mock_tools.return_value.cmd_combined_status.return_value = {
            "ok": True,
            "data": {"mode": "list", "repos": {"myrepo": {"path": "/path"}}},
        }
        result = json.loads(sk._cm_status())
        assert result["ok"] is True
        call_args = mock_tools.return_value.cmd_combined_status.call_args[0][0]
        assert not call_args.repo  # empty string or None → list mode

    @patch("coding_master_skillkit._get_tools")
    def test_cm_status_with_repo_shows_detail(self, mock_tools, sk):
        mock_tools.return_value.cmd_combined_status.return_value = {
            "ok": True,
            "data": {"mode": "detail", "repo": "myrepo", "session": {}, "progress": {}},
        }
        result = json.loads(sk._cm_status(repo="myrepo"))
        assert result["ok"] is True
        call_args = mock_tools.return_value.cmd_combined_status.call_args[0][0]
        assert call_args.repo == "myrepo"


# ===========================================================================
# _cm_dev_git validation (internal tool, still tested directly)
# ===========================================================================


class TestCmGit:
    @pytest.fixture
    def sk(self):
        return CodingMasterSkillkit(agent_id="test")

    def test_forbidden_subcmd(self, sk):
        result = json.loads(sk._cm_dev_git(subcmd="reflog"))
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
        result = json.loads(sk._cm_dev_git(subcmd="status"))
        assert result["ok"] is True
        assert "On branch main" in result["data"]["stdout"]

    def test_no_session_returns_error(self, sk):
        sk._resolve_session_cwd = MagicMock(return_value=None)
        result = json.loads(sk._cm_dev_git(subcmd="status"))
        assert result["ok"] is False
        assert "No active session" in result["error"]


# ===========================================================================
# Guard: hints/errors must only reference the 7 exposed agent-facing tools
# ===========================================================================


class TestHintToolConsistency:
    """Ensure all _cm_xxx() references in hints, errors, and next_action fields
    map to an actual tool in the v5.0 skillkit (7 tools only).

    This prevents the agent from being guided toward internal/non-existent tools,
    which was the root cause of infinite retry loops in v4.x.
    """

    @pytest.fixture
    def exposed_commands(self):
        """Return the set of command suffixes exposed via _cm_* methods."""
        sk = CodingMasterSkillkit(agent_id="test")
        skills = sk._createSkills()
        names = {s.get_function_name() for s in skills}
        commands = set()
        for name in names:
            if name.startswith("_cm_"):
                # _cm_next → "next", _cm_dev_edit → "dev-edit"
                cmd = name[4:].replace("_", "-")
                commands.add(cmd)
        return commands

    def test_hint_flow_maps_reference_exposed_tools(self, exposed_commands):
        """All _hint() command strings in _FLOW_* maps must reference exposed _cm_* tools."""
        import re
        _tools_dir = Path(__file__).resolve().parents[2] / "scripts"
        if str(_tools_dir) not in sys.path:
            sys.path.insert(0, str(_tools_dir))
        import tools as tools_mod

        flow_maps = [
            tools_mod._FLOW_AFTER_LOCK,
            {"_": tools_mod._FLOW_AFTER_SCOPE},
            tools_mod._FLOW_AFTER_ENGINE,
            {"_": tools_mod._FLOW_AFTER_REPORT},
        ]
        cm_tool_pattern = re.compile(r"_cm_([\w]+)")
        missing = []
        for flow in flow_maps:
            for key, hint in flow.items():
                if isinstance(hint, dict) and "command" in hint:
                    cmd_str = hint["command"]
                    for match in cm_tool_pattern.finditer(cmd_str):
                        tool_suffix = match.group(1).replace("_", "-")
                        full_name = f"_cm_{match.group(1)}"
                        if tool_suffix not in exposed_commands:
                            missing.append(
                                f"_FLOW hint references '{full_name}' "
                                f"but it is not in v5.0 skillkit (exposed: {sorted(exposed_commands)})"
                            )
        assert not missing, "\n".join(missing)

    def test_error_hints_reference_exposed_tools(self, exposed_commands):
        """All _cm_xxx() references in hint/error strings must use exposed agent-facing tools."""
        import re
        source = (Path(__file__).resolve().parents[2] / "scripts" / "tools.py").read_text()
        skillkit_source = (Path(__file__).resolve().parents[2]
                           / "coding_master_skillkit.py").read_text()

        pattern = re.compile(r"_cm_([\w]+)\s*\(")
        missing = []
        for src_name, src in [("tools.py", source), ("skillkit.py", skillkit_source)]:
            for i, line in enumerate(src.splitlines(), 1):
                if '"' not in line and "'" not in line:
                    continue
                stripped = line.strip()
                if stripped.startswith("def ") or stripped.startswith("from ") or stripped.startswith("import "):
                    continue
                for match in pattern.finditer(line):
                    func_name = match.group(1)
                    cmd = func_name.replace("_", "-")
                    if cmd not in exposed_commands:
                        pos = match.start()
                        before = line[:pos]
                        if before.count('"') % 2 == 1 or before.count("'") % 2 == 1:
                            missing.append(
                                f"{src_name}:{i} references '_cm_{func_name}()' "
                                f"but '_cm_{func_name}' is not an exposed v5.0 tool"
                            )
        assert not missing, "\n".join(missing)
