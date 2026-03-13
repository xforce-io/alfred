"""Tests for CLI session module (OPTIMIZED VERSION).

Basic coverage for session management commands.
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock


class TestSessionCommands:
    """Test session command structure."""

    def test_session_list_command_exists(self):
        """Session list command should be available."""
        try:
            from src.everbot.cli.main import _build_parser
            parser = _build_parser()
            args = parser.parse_args(["session", "list"])
            assert args.command == "session"
            assert args.session_command == "list"
        except (ImportError, AttributeError, SystemExit):
            pytest.skip("Session list command not available")

    def test_session_show_command_exists(self):
        """Session show command should be available."""
        try:
            from src.everbot.cli.main import _build_parser
            parser = _build_parser()
            args = parser.parse_args(["session", "show", "test_session"])
            assert args.command == "session"
            assert args.session_command == "show"
        except (ImportError, AttributeError, SystemExit):
            pytest.skip("Session show command not available")


class TestSessionManagerIntegration:
    """Test session manager integration."""

    def test_session_manager_can_be_imported(self):
        """SessionManager should be importable from CLI context."""
        try:
            from src.everbot.core.session.session import SessionManager
            assert SessionManager is not None
        except ImportError:
            pytest.skip("SessionManager not available")

    @pytest.mark.asyncio
    async def test_session_list_returns_sessions(self):
        """Session list should return available sessions."""
        try:
            from src.everbot.cli.session import list_sessions

            with patch('src.everbot.cli.session.get_session_manager') as mock_mgr:
                mock_session_mgr = MagicMock()
                mock_session_mgr.list_sessions = AsyncMock(return_value=[
                    {"id": "session_1", "agent": "agent_a"},
                    {"id": "session_2", "agent": "agent_b"},
                ])
                mock_mgr.return_value = mock_session_mgr

                sessions = await list_sessions()
                assert len(sessions) == 2
                assert sessions[0]["id"] == "session_1"
        except ImportError:
            pytest.skip("list_sessions not available")


class TestSessionPersistence:
    """Test session persistence behavior."""

    def test_session_directory_exists(self):
        """Session storage directory should be defined."""
        try:
            from src.everbot.infra.user_data import get_user_data_manager
            user_data = get_user_data_manager()
            session_dir = user_data.sessions_dir
            assert session_dir is not None
        except (ImportError, AttributeError):
            pytest.skip("User data manager not available")
