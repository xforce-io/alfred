"""Unit tests for CronDelivery result delivery logic."""

from unittest.mock import AsyncMock

import pytest

from src.everbot.core.runtime.cron_delivery import CronDelivery


def _make_delivery(**overrides) -> CronDelivery:
    defaults = dict(
        session_manager=AsyncMock(),
        primary_session_id="web_session_test",
        heartbeat_session_id="heartbeat_session_test",
        agent_name="test_agent",
        ack_max_chars=300,
        broadcast_scope="agent",
        realtime_push=False,
    )
    defaults.update(overrides)
    return CronDelivery(**defaults)


class TestShouldDeliver:
    def test_no_heartbeat_ok_delivers(self):
        d = _make_delivery()
        assert d.should_deliver("Task completed successfully") is True

    def test_heartbeat_ok_only_suppresses(self):
        d = _make_delivery()
        assert d.should_deliver("HEARTBEAT_OK") is False

    def test_heartbeat_ok_at_start_short_remaining_suppresses(self):
        d = _make_delivery(ack_max_chars=300)
        assert d.should_deliver("HEARTBEAT_OK all good") is False

    def test_heartbeat_ok_at_end_short_remaining_suppresses(self):
        d = _make_delivery(ack_max_chars=300)
        assert d.should_deliver("all good HEARTBEAT_OK") is False

    def test_heartbeat_ok_at_start_long_remaining_delivers(self):
        d = _make_delivery(ack_max_chars=10)
        assert d.should_deliver("HEARTBEAT_OK " + "x" * 100) is True

    def test_heartbeat_ok_at_end_long_remaining_delivers(self):
        d = _make_delivery(ack_max_chars=10)
        assert d.should_deliver("x" * 100 + " HEARTBEAT_OK") is True

    def test_heartbeat_ok_in_middle_delivers(self):
        d = _make_delivery()
        assert d.should_deliver("prefix HEARTBEAT_OK suffix") is True

    def test_empty_string_delivers(self):
        d = _make_delivery()
        assert d.should_deliver("") is True

    def test_whitespace_heartbeat_ok_suppresses(self):
        d = _make_delivery()
        assert d.should_deliver("  HEARTBEAT_OK  ") is False


class TestDeliverResult:
    @pytest.mark.asyncio
    async def test_suppressed_result_returns_false(self):
        d = _make_delivery()
        result = await d.deliver_result("HEARTBEAT_OK", "run_1")
        assert result is False
        d.session_manager.inject_history_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_delivered_result_injects_and_deposits(self):
        sm = AsyncMock()
        sm.inject_history_message.return_value = True
        sm.deposit_mailbox_event.return_value = True
        d = _make_delivery(session_manager=sm)

        result = await d.deliver_result("Task done: summary here", "run_2")
        assert result is True
        sm.inject_history_message.assert_called_once()
        sm.deposit_mailbox_event.assert_called_once()


class TestDepositJobEvent:
    @pytest.mark.asyncio
    async def test_deposits_with_correct_dedupe_key(self):
        sm = AsyncMock()
        sm.deposit_mailbox_event.return_value = True
        d = _make_delivery(session_manager=sm)

        await d.deposit_job_event(
            event_type="job_result",
            source_session_id="job_abc_123",
            summary="done",
            detail="full detail",
            run_id="run_3",
        )
        sm.deposit_mailbox_event.assert_called_once()
        event = sm.deposit_mailbox_event.call_args[0][1]
        assert "job_result:test_agent:run_3:job_abc_123" in event["dedupe_key"]


class TestRealtimeEmit:
    @pytest.mark.asyncio
    async def test_session_scope_emit_sets_target_session_id(self, monkeypatch):
        emitted = []
        d = _make_delivery(broadcast_scope="session")

        async def _fake_emit(source_session_id, data, **kwargs):
            emitted.append((source_session_id, data, kwargs))

        monkeypatch.setattr("src.everbot.core.runtime.cron_delivery.emit", _fake_emit, raising=False)
        monkeypatch.setattr("src.everbot.core.runtime.events.emit", _fake_emit)

        await d._emit_realtime("hello", "run_4")

        assert len(emitted) == 1
        source_session_id, _data, kwargs = emitted[0]
        assert source_session_id == "web_session_test"
        assert kwargs["scope"] == "session"
        assert kwargs["target_session_id"] == "web_session_test"
