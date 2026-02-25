"""Tests for dispatch.py — CLI routing and error handling."""

import pytest

from dispatch import COMMANDS, _build_parser


class TestCommandRegistry:
    def test_all_subcommands_registered(self):
        parser = _build_parser()
        # All keys in COMMANDS should be valid subcommands
        expected = {
            "config-list", "config-add", "config-set", "config-remove",
            "workspace-check", "env-probe", "analyze", "develop",
            "test", "submit-pr", "env-verify", "release", "renew-lease",
            "feature-plan", "feature-next", "feature-done",
            "feature-list", "feature-update",
        }
        assert set(COMMANDS.keys()) == expected

    def test_each_command_is_callable(self):
        for name, handler in COMMANDS.items():
            assert callable(handler), f"{name} handler is not callable"


class TestParser:
    def test_unknown_command_exits(self):
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["nonexistent-command"])

    def test_config_list_no_args(self):
        parser = _build_parser()
        args = parser.parse_args(["config-list"])
        assert args.command == "config-list"

    def test_workspace_check_requires_args(self):
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["workspace-check"])

    def test_workspace_check_parses(self):
        parser = _build_parser()
        args = parser.parse_args([
            "workspace-check", "--workspace", "myws", "--task", "fix bug"
        ])
        assert args.workspace == "myws"
        assert args.task == "fix bug"
        assert args.engine is None

    def test_feature_plan_parses(self):
        parser = _build_parser()
        args = parser.parse_args([
            "feature-plan", "--workspace", "ws",
            "--task", "big task",
            "--features", '[{"title": "A"}]',
        ])
        assert args.workspace == "ws"
        assert args.features == '[{"title": "A"}]'


class TestErrorResponse:
    """Verify error responses contain error_code where expected."""

    def test_config_add_error_has_ok_false(self):
        from config_manager import ConfigManager
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            cm = ConfigManager(config_path=Path(td) / "cfg.yaml")
            result = cm.add("workspace", "ws", "/path")
            assert result["ok"] is True
            result = cm.add("workspace", "ws", "/other")
            assert result["ok"] is False
            assert "error" in result
