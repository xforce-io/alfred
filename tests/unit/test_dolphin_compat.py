"""Tests for ensure_continue_chat_compatibility."""

from unittest.mock import patch, MagicMock

from src.everbot.infra.dolphin_compat import ensure_continue_chat_compatibility


class TestEnsureContinueChatCompatibility:

    @patch("src.everbot.infra.dolphin_compat.flags")
    def test_returns_true_when_flag_enabled(self, mock_flags):
        mock_flags.is_enabled.return_value = True
        mock_flags.EXPLORE_BLOCK_V2 = "EXPLORE_BLOCK_V2"

        result = ensure_continue_chat_compatibility()

        assert result is True
        mock_flags.is_enabled.assert_called_once_with("EXPLORE_BLOCK_V2")
        mock_flags.set_flag.assert_called_once_with("EXPLORE_BLOCK_V2", False)

    @patch("src.everbot.infra.dolphin_compat.flags")
    def test_returns_false_when_flag_disabled(self, mock_flags):
        mock_flags.is_enabled.return_value = False
        mock_flags.EXPLORE_BLOCK_V2 = "EXPLORE_BLOCK_V2"

        result = ensure_continue_chat_compatibility()

        assert result is False
        mock_flags.is_enabled.assert_called_once_with("EXPLORE_BLOCK_V2")
        mock_flags.set_flag.assert_not_called()
