"""S4 test: Telegram send failure must attempt mailbox deposit; both-failed raises.

Tests that when Telegram send fails, mailbox deposit is still attempted.
When both fail, RuntimeError is raised to fail the task and trigger retry.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_telegram_send_failure_attempts_mailbox():
    """S4: Telegram send failure must still attempt mailbox deposit."""
    from everbot.channels.telegram_channel import TelegramChannel
    
    # Mock dependencies
    mock_agent_loader = MagicMock()
    mock_skill_loader = MagicMock()
    mock_delivery = MagicMock()
    
    # Create channel
    channel = TelegramChannel(
        token="fake_token",
        agent_loader=mock_agent_loader,
        skill_loader=mock_skill_loader,
        delivery=mock_delivery,
    )
    
    # Mock Telegram bot send to fail
    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock(side_effect=Exception("Telegram API error"))
    channel._bot = mock_bot
    
    # Mock mailbox deposit to succeed
    mock_delivery.deposit_mailbox_event = AsyncMock(return_value=True)
    
    # Create mock event
    mock_event = MagicMock()
    mock_event.user_id = 12345
    mock_event.content = "test message"
    mock_event.run_id = "test_run_123"
    
    # Call handler (should not raise, mailbox succeeds)
    await channel._handle_realtime_event(mock_event)
    
    # Verify both were attempted
    mock_bot.send_message.assert_called_once()
    mock_delivery.deposit_mailbox_event.assert_called_once()


@pytest.mark.asyncio
async def test_both_telegram_and_mailbox_fail_raises():
    """S4: When both Telegram send AND mailbox deposit fail, task must fail."""
    from everbot.channels.telegram_channel import TelegramChannel
    
    # Mock dependencies
    mock_agent_loader = MagicMock()
    mock_skill_loader = MagicMock()
    mock_delivery = MagicMock()
    
    # Create channel
    channel = TelegramChannel(
        token="fake_token",
        agent_loader=mock_agent_loader,
        skill_loader=mock_skill_loader,
        delivery=mock_delivery,
    )
    
    # Mock Telegram bot send to fail
    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock(side_effect=Exception("Telegram API error"))
    channel._bot = mock_bot
    
    # Mock mailbox deposit to also fail
    mock_delivery.deposit_mailbox_event = AsyncMock(return_value=False)
    
    # Create mock event
    mock_event = MagicMock()
    mock_event.user_id = 12345
    mock_event.content = "test message"
    mock_event.run_id = "test_run_123"
    
    # Call handler and expect RuntimeError (both failed)
    with pytest.raises(RuntimeError, match="Failed to deliver message via both Telegram and mailbox"):
        await channel._handle_realtime_event(mock_event)
    
    # Verify both were attempted
    mock_bot.send_message.assert_called_once()
    mock_delivery.deposit_mailbox_event.assert_called_once()


@pytest.mark.asyncio
async def test_telegram_send_success_skips_mailbox():
    """S4: When Telegram send succeeds, mailbox deposit is NOT needed."""
    from everbot.channels.telegram_channel import TelegramChannel
    
    # Mock dependencies
    mock_agent_loader = MagicMock()
    mock_skill_loader = MagicMock()
    mock_delivery = MagicMock()
    
    # Create channel
    channel = TelegramChannel(
        token="fake_token",
        agent_loader=mock_agent_loader,
        skill_loader=mock_skill_loader,
        delivery=mock_delivery,
    )
    
    # Mock Telegram bot send to succeed
    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock(return_value=MagicMock())
    channel._bot = mock_bot
    
    # Mock mailbox deposit (should not be called)
    mock_delivery.deposit_mailbox_event = AsyncMock()
    
    # Create mock event
    mock_event = MagicMock()
    mock_event.user_id = 12345
    mock_event.content = "test message"
    mock_event.run_id = "test_run_123"
    
    # Call handler
    await channel._handle_realtime_event(mock_event)
    
    # Verify Telegram send was called
    mock_bot.send_message.assert_called_once()
    # Verify mailbox was NOT called (Telegram succeeded)
    mock_delivery.deposit_mailbox_event.assert_not_called()


if __name__ == "__main__":
    # Run tests with: python -m pytest tests/integration/test_s4_telegram_mailbox_fallback.py -v
    pytest.main([__file__, "-v"])
