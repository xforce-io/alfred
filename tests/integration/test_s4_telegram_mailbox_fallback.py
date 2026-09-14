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
    from src.everbot.channels.telegram_channel import TelegramChannel
    
    # Mock session manager
    mock_sm = AsyncMock()
    mock_sm.deposit_mailbox_event = AsyncMock(return_value=True)
    
    # Create channel with real constructor signature
    channel = TelegramChannel(
        bot_token="123:FAKE_TOKEN",
        session_manager=mock_sm,
        default_agent="test_agent",
    )
    
    # Mock _send_split_with_entities to fail (ok_count=0, total=1 → sent=False)
    with patch.object(channel, '_send_split_with_entities', new_callable=AsyncMock) as mock_send:
        mock_send.return_value = (0, 1)  # Failed to send
        
        # Mock _maybe_attach_projection (not relevant for this test)
        with patch.object(channel, '_maybe_attach_projection', new_callable=AsyncMock) as mock_proj:
            mock_proj.return_value = False
            
            # Mock _convert_markdown
            with patch.object(channel, '_convert_markdown') as mock_md:
                mock_md.return_value = ("test text", [])
                
                # Mock _should_defer to return False (not deferring pushes)
                with patch.object(channel, '_should_defer', return_value=False):
                    
                    # Set up bindings (agent → chat_id, use string chat_id)
                    channel._bindings = {"12345": "test_agent"}
                    
                    # Create realistic event data (scope=agent to pass routing)
                    data = {
                        "scope": "agent",  # Required for routing.deliver=True without session id
                        "source_type": "heartbeat_delivery",
                        "agent_name": "test_agent",
                        "detail": "test message content",
                        "run_id": "test_run_123",
                    }
                    
                    # Call handler (should not raise, mailbox succeeds)
                    await channel._on_background_event("heartbeat_session_test", data)
                    
                    # Verify Telegram send was attempted
                    mock_send.assert_called_once()
                    # Verify mailbox deposit was attempted (S4: always attempt)
                    mock_sm.deposit_mailbox_event.assert_called_once()


@pytest.mark.asyncio
async def test_both_telegram_and_mailbox_fail_raises():
    """S4: When both Telegram send AND mailbox deposit fail, task must fail."""
    from src.everbot.channels.telegram_channel import TelegramChannel
    
    # Mock session manager with deposit failure
    mock_sm = AsyncMock()
    mock_sm.deposit_mailbox_event = AsyncMock(return_value=False)  # Mailbox fails
    
    # Create channel
    channel = TelegramChannel(
        bot_token="123:FAKE_TOKEN",
        session_manager=mock_sm,
        default_agent="test_agent",
    )
    
    # Mock _send_split_with_entities to fail
    with patch.object(channel, '_send_split_with_entities', new_callable=AsyncMock) as mock_send:
        mock_send.return_value = (0, 1)  # Telegram send failed
        
        with patch.object(channel, '_maybe_attach_projection', new_callable=AsyncMock) as mock_proj:
            mock_proj.return_value = False
            
            with patch.object(channel, '_convert_markdown') as mock_md:
                mock_md.return_value = ("test text", [])
                
                with patch.object(channel, '_should_defer', return_value=False):
                    
                    channel._bindings = {"12345": "test_agent"}
                    
                    data = {
                        "scope": "agent",  # Required for routing.deliver=True
                        "source_type": "heartbeat_delivery",
                        "agent_name": "test_agent",
                        "detail": "test message content",
                        "run_id": "test_run_123",
                    }
                    
                    # Call handler and expect RuntimeError (both failed)
                    with pytest.raises(RuntimeError, match="Both Telegram delivery and mailbox deposit failed for heartbeat_delivery"):
                        await channel._on_background_event("heartbeat_session_test", data)
                    
                    # Verify both were attempted
                    mock_send.assert_called_once()
                    mock_sm.deposit_mailbox_event.assert_called_once()


@pytest.mark.asyncio
async def test_telegram_send_success_mailbox_still_attempted():
    """S4: When Telegram send succeeds, mailbox deposit is still attempted (unless projected)."""
    from src.everbot.channels.telegram_channel import TelegramChannel
    
    # Mock session manager
    mock_sm = AsyncMock()
    mock_sm.deposit_mailbox_event = AsyncMock(return_value=True)
    
    # Create channel
    channel = TelegramChannel(
        bot_token="123:FAKE_TOKEN",
        session_manager=mock_sm,
        default_agent="test_agent",
    )
    
    # Mock _send_split_with_entities to succeed (ok_count=total → sent=True)
    with patch.object(channel, '_send_split_with_entities', new_callable=AsyncMock) as mock_send:
        mock_send.return_value = (1, 1)  # Telegram send succeeded
        
        # Mock projection to fail (so mailbox is attempted)
        with patch.object(channel, '_maybe_attach_projection', new_callable=AsyncMock) as mock_proj:
            mock_proj.return_value = False  # No projection → mailbox attempted
            
            with patch.object(channel, '_convert_markdown') as mock_md:
                mock_md.return_value = ("test text", [])
                
                with patch.object(channel, '_should_defer', return_value=False):
                    
                    channel._bindings = {"12345": "test_agent"}
                    
                    data = {
                        "scope": "agent",  # Required for routing.deliver=True
                        "source_type": "heartbeat_delivery",
                        "agent_name": "test_agent",
                        "detail": "test message content",
                        "run_id": "test_run_123",
                    }
                    
                    # Call handler
                    await channel._on_background_event("heartbeat_session_test", data)
                    
                    # Verify Telegram send was called
                    mock_send.assert_called_once()
                    # S4: Mailbox is still attempted (unless projected=True)
                    mock_sm.deposit_mailbox_event.assert_called_once()


@pytest.mark.asyncio
async def test_telegram_send_success_projected_skips_mailbox():
    """S4: When Telegram send succeeds AND projection succeeds, mailbox is skipped."""
    from src.everbot.channels.telegram_channel import TelegramChannel
    
    # Mock session manager
    mock_sm = AsyncMock()
    mock_sm.deposit_mailbox_event = AsyncMock()
    
    # Create channel
    channel = TelegramChannel(
        bot_token="123:FAKE_TOKEN",
        session_manager=mock_sm,
        default_agent="test_agent",
    )
    
    # Mock _send_split_with_entities to succeed
    with patch.object(channel, '_send_split_with_entities', new_callable=AsyncMock) as mock_send:
        mock_send.return_value = (1, 1)  # Telegram send succeeded
        
        # Mock projection to succeed (so mailbox is skipped)
        with patch.object(channel, '_maybe_attach_projection', new_callable=AsyncMock) as mock_proj:
            mock_proj.return_value = True  # Projected → mailbox skipped
            
            with patch.object(channel, '_convert_markdown') as mock_md:
                mock_md.return_value = ("test text", [])
                
                with patch.object(channel, '_should_defer', return_value=False):
                    
                    channel._bindings = {"12345": "test_agent"}
                    
                    data = {
                        "scope": "agent",  # Required for routing.deliver=True
                        "source_type": "heartbeat_delivery",
                        "agent_name": "test_agent",
                        "detail": "test message content",
                        "run_id": "test_run_123",
                    }
                    
                    # Call handler
                    await channel._on_background_event("heartbeat_session_test", data)
                    
                    # Verify Telegram send was called
                    mock_send.assert_called_once()
                    # Mailbox is NOT called (projected=True)
                    mock_sm.deposit_mailbox_event.assert_not_called()


if __name__ == "__main__":
    # Run tests with: PYTHONPATH=src python -m pytest tests/integration/test_s4_telegram_mailbox_fallback.py -v
    pytest.main([__file__, "-v"])
