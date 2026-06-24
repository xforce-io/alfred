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

    @pytest.mark.asyncio
    async def test_emit_realtime_carries_transcript_worthy_flag(self, monkeypatch):
        """#60:内容型 job/agent 投递置 transcript_worthy=True,
        供 channel 侧据此把报告登记为 context projection。"""
        emitted = []
        d = _make_delivery()

        async def _fake_emit(source_session_id, data, **kwargs):
            emitted.append(data)

        monkeypatch.setattr("src.everbot.core.runtime.cron_delivery.emit", _fake_emit, raising=False)
        monkeypatch.setattr("src.everbot.core.runtime.events.emit", _fake_emit)

        await d._emit_realtime("report body", "run_x", transcript_worthy=True)

        assert emitted[0]["transcript_worthy"] is True

    @pytest.mark.asyncio
    async def test_emit_realtime_defaults_not_transcript_worthy(self, monkeypatch):
        """缺省(心跳状态路径)不置 transcript_worthy → 不入逐字稿。"""
        emitted = []
        d = _make_delivery()

        async def _fake_emit(source_session_id, data, **kwargs):
            emitted.append(data)

        monkeypatch.setattr("src.everbot.core.runtime.cron_delivery.emit", _fake_emit, raising=False)
        monkeypatch.setattr("src.everbot.core.runtime.events.emit", _fake_emit)

        await d._emit_realtime("status ping", "run_y")

        assert emitted[0].get("transcript_worthy", False) is False

    @pytest.mark.asyncio
    async def test_emit_realtime_projection_source_survives_envelope(self):
        """#122:projection 溯源 id 必须用独立键 projection_source_id —— 不能复用
        source_session_id,后者是事件信封保留字段,会被 emit() 覆盖为发出方 session
        (primary),从而丢失 job session 锚点。本测试走真实 emit + 真实订阅者,
        正是为捕捉这种字段覆盖(假 emit 会绕过信封 enrichment)。"""
        from src.everbot.core.runtime import events
        captured = []

        def _cap(src, env):
            captured.append(env)

        events.subscribe(_cap)
        try:
            d = _make_delivery()  # primary_session_id="web_session_test"
            await d._emit_realtime(
                "report body", "job_f8a5b0a67ad3", transcript_worthy=True,
                source_session_id="job_routine_38364fe6_d185ce87",
            )
        finally:
            events._subscribers.clear()

        assert len(captured) == 1
        env = captured[0]
        # 信封自有的 source_session_id 仍是发出方(primary),不被借用/覆盖。
        assert env["source_session_id"] == "web_session_test"
        # 溯源锚点用独立键,完整保留 job session(不被信封覆盖)。
        assert env["projection_source_id"] == "job_routine_38364fe6_d185ce87"

    @pytest.mark.asyncio
    async def test_emit_realtime_projection_source_defaults_none(self):
        """未提供 source_session_id 时 projection_source_id 为 None,channel 回落 run_id。"""
        from src.everbot.core.runtime import events
        captured = []
        events.subscribe(lambda src, env: captured.append(env))
        try:
            d = _make_delivery()
            await d._emit_realtime("status ping", "run_y")
        finally:
            events._subscribers.clear()

        assert captured[0].get("projection_source_id") is None
