"""S4 test: Telegram send failure must attempt mailbox deposit; both-failed raises.

Tests that when Telegram send fails, mailbox deposit is still attempted.
When both fail, DeliveryFailed is raised so events.emit / cron mark FAILED.
"""
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.everbot.core.runtime import events
from src.everbot.core.runtime.cron import CronExecutor
from src.everbot.core.runtime.cron_delivery import CronDelivery
from src.everbot.core.tasks.routine_manager import RoutineManager


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


def _make_executor(tmp_path: Path, **overrides) -> CronExecutor:
    sm = AsyncMock()
    sm.get_primary_session_id.return_value = "web_session_test"
    sm.get_heartbeat_session_id.return_value = "heartbeat_session_test"
    sm.inject_history_message = AsyncMock(return_value=True)
    sm.deposit_mailbox_event = AsyncMock(return_value=True)
    delivery = CronDelivery(
        session_manager=sm,
        primary_session_id="web_session_test",
        heartbeat_session_id="heartbeat_session_test",
        agent_name="test_agent",
        realtime_push=True,
    )
    defaults = dict(
        agent_name="test_agent",
        workspace_path=tmp_path,
        session_manager=sm,
        agent_factory=AsyncMock(),
        routine_manager=RoutineManager(tmp_path),
        delivery=delivery,
    )
    defaults.update(overrides)
    return CronExecutor(**defaults)


def _seed_task(tmp_path: Path, **task_overrides):
    mgr = RoutineManager(tmp_path)
    defaults = dict(
        title="Test task",
        schedule="1h",
        next_run_at="2026-03-01T11:00:00+00:00",
        now=datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc),
    )
    defaults.update(task_overrides)
    mgr.add_routine(**defaults)
    return mgr


def _telegram_channel(*, deposit_ok: bool):
    from src.everbot.channels.telegram_channel import TelegramChannel

    mock_sm = AsyncMock()
    mock_sm.deposit_mailbox_event = AsyncMock(return_value=deposit_ok)
    channel = TelegramChannel(
        bot_token="123:FAKE_TOKEN",
        session_manager=mock_sm,
        default_agent="test_agent",
    )
    channel._bindings = {"12345": "test_agent"}
    return channel, mock_sm


@pytest.mark.asyncio
async def test_emit_reraises_telegram_both_fail():
    """S4: _emit_realtime → events.emit → Telegram handler both-fail must raise."""
    events._subscribers.clear()
    try:
        channel, mock_sm = _telegram_channel(deposit_ok=False)
        events.subscribe(channel._on_background_event)
        with patch.object(channel, "_send_split_with_entities", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = (0, 1)
            with patch.object(channel, "_maybe_attach_projection", new_callable=AsyncMock, return_value=False):
                with patch.object(channel, "_convert_markdown", return_value=("test text", [])):
                    with patch.object(channel, "_should_defer", return_value=False):
                        with pytest.raises(
                            events.DeliveryFailed,
                            match="Both Telegram delivery and mailbox deposit failed for heartbeat_delivery",
                        ):
                            await events.emit(
                                "web_session_test",
                                {"detail": "test message content", "deliver": True},
                                agent_name="test_agent",
                                scope="agent",
                                source_type="heartbeat_delivery",
                                run_id="test_run_123",
                            )
        mock_send.assert_called_once()
        mock_sm.deposit_mailbox_event.assert_called_once()
    finally:
        events._subscribers.clear()


@pytest.mark.asyncio
async def test_cron_marks_failed_when_telegram_emit_both_fails(tmp_path):
    """S4: cron realtime emit both-fail must mark the isolated task failed."""
    events._subscribers.clear()
    try:
        mgr = _seed_task(
            tmp_path,
            title="S4 emit test",
            execution_mode="isolated",
            job="skill-evaluate",
        )
        executor = _make_executor(tmp_path, routine_manager=mgr)
        channel, _mock_sm = _telegram_channel(deposit_ok=False)
        events.subscribe(channel._on_background_event)

        with patch.object(executor, "_invoke_job", new_callable=AsyncMock, return_value="test result"):
            with patch.object(channel, "_send_split_with_entities", new_callable=AsyncMock, return_value=(0, 1)):
                with patch.object(channel, "_maybe_attach_projection", new_callable=AsyncMock, return_value=False):
                    with patch.object(channel, "_convert_markdown", return_value=("test text", [])):
                        with patch.object(channel, "_should_defer", return_value=False):
                            result = await executor.tick(
                                mgr.load_task_list(),
                                run_agent=AsyncMock(),
                                inject_context=AsyncMock(),
                                run_id="test_run",
                            )

        assert result.failed == 1
        assert result.executed == 0
        assert result.results[0].status == "failed"
        assert "Both Telegram delivery and mailbox deposit failed" in result.results[0].error
    finally:
        events._subscribers.clear()


if __name__ == "__main__":
    # Run tests with: PYTHONPATH=src python -m pytest tests/integration/test_s4_telegram_mailbox_fallback.py -v
    pytest.main([__file__, "-v"])
