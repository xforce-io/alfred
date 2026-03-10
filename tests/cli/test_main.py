"""Tests for CLI main module (OPTIMIZED VERSION).

Basic coverage for command parsing and entry points.
"""

import pytest
from unittest.mock import patch, MagicMock


class TestCLIEntryPoints:
    """Test CLI entry point behavior."""

    def test_main_imports_without_error(self):
        """Main module should import without errors."""
        from src.everbot.cli.main import main
        assert callable(main)

    def test_cli_module_exports_main(self):
        """CLI package should export main function."""
        from src.everbot.cli import main
        assert callable(main)


class TestCLIArgumentParsing:
    """Test CLI argument parsing behavior."""

    def test_parser_creation(self):
        """Argument parser should create without errors."""
        from src.everbot.cli.main import _build_parser
        parser = _build_parser()
        assert parser is not None

    def test_help_flag_parsing(self):
        """--help should trigger help message."""
        from src.everbot.cli.main import _build_parser
        parser = _build_parser()

        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["--help"])
        # 0 = successful help exit
        assert exc_info.value.code == 0


class TestCLISubcommands:
    """Test CLI subcommand structure."""

    def test_start_subcommand_exists(self):
        """Start subcommand should be available."""
        from src.everbot.cli.main import _build_parser
        parser = _build_parser()

        args = parser.parse_args(["start"])
        assert args.command == "start"

    def test_stop_subcommand_exists(self):
        """Stop subcommand should be available."""
        from src.everbot.cli.main import _build_parser
        parser = _build_parser()

        args = parser.parse_args(["stop"])
        assert args.command == "stop"

    def test_status_subcommand_exists(self):
        """Status subcommand should be available."""
        from src.everbot.cli.main import _build_parser
        parser = _build_parser()

        args = parser.parse_args(["status"])
        assert args.command == "status"


class TestCLIDefaultBehavior:
    """Test CLI default behavior."""

    def test_no_args_defaults_to_no_command(self):
        """CLI without args should have no command set."""
        from src.everbot.cli.main import _build_parser
        parser = _build_parser()

        args = parser.parse_args([])
        assert args.command is None
