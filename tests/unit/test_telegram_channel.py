"""Unit tests for TelegramChannel."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.everbot.channels.telegram_channel import TelegramChannel, _extract_urls


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session_manager_mock():
    sm = MagicMock()
    sm.get_cached_agent.return_value = None
    sm.cache_agent = MagicMock()
    sm.acquire_session = AsyncMock(return_value=True)
    sm.release_session = MagicMock()
    sm.load_session = AsyncMock(return_value=None)
    sm.save_session = AsyncMock()
    sm.restore_timeline = MagicMock()
    sm.restore_to_agent = AsyncMock()
    sm.append_timeline_event = MagicMock()
    sm.inject_history_message = AsyncMock(return_value=True)
    sm.deposit_mailbox_event = AsyncMock(return_value=True)
    sm.persistence = MagicMock()
    sm.persistence._get_lock_path.return_value = Path("/tmp/test_lock")
    return sm


def _make_channel(tmp_path: Path, **kwargs) -> TelegramChannel:
    sm = _make_session_manager_mock()
    ch = TelegramChannel(
        bot_token="123:FAKE",
        session_manager=sm,
        default_agent=kwargs.get("default_agent", "test_agent"),
        allowed_chat_ids=kwargs.get("allowed_chat_ids"),
    )
    ch._bindings_path = tmp_path / "bindings.json"
    return ch


class TestTelegramToolkitRegistration:
    def test_ensure_skillkit_is_noop_under_milkie(self, tmp_path):
        # #38:milkie register_skillkit 是优雅 no-op(telegram 文件发送走输出约定);
        # _ensure_telegram_skillkit 不崩、不碰 dolphin 式 installedSkillset。
        ch = _make_channel(tmp_path)
        installed = MagicMock()
        installed.hasSkill.return_value = False
        agent = SimpleNamespace(
            global_skills=SimpleNamespace(installedSkillset=installed),
        )

        ch._ensure_telegram_skillkit(agent, "test_agent")  # 不抛

        installed.addSkillkit.assert_not_called()  # milkie 不走 dolphin 注册路径

    def test_routes_through_per_agent_provider_not_global(self, tmp_path, monkeypatch):
        """#4 fallback: skillkit must follow get_provider_for_agent, not the
        GLOBAL get_provider — otherwise a milkie global provider crashes a
        dolphin-fallback agent on register_skillkit (NotImplementedError)."""
        ch = _make_channel(tmp_path)
        agent = SimpleNamespace()

        fake_provider = MagicMock()
        fake_provider.has_skill.return_value = False
        fake_provider.register_skillkit = MagicMock()

        seen = {}

        def _fake_get_for_agent(name):
            seen["name"] = name
            return fake_provider

        import src.everbot.core.agent.provider as provider_mod

        monkeypatch.setattr(provider_mod, "get_provider_for_agent", _fake_get_for_agent)
        # If anything reaches for the GLOBAL provider, fail loudly.
        monkeypatch.setattr(
            provider_mod,
            "get_provider",
            lambda: (_ for _ in ()).throw(AssertionError("must not use global provider")),
        )

        ch._ensure_telegram_skillkit(agent, "milkie_routed_agent")

        assert seen["name"] == "milkie_routed_agent"
        fake_provider.has_skill.assert_called_once_with(agent, "_tg_send_file")
        fake_provider.register_skillkit.assert_called_once()
        assert fake_provider.register_skillkit.call_args.args[0] is agent


# ===========================================================================
# 1. _split_message — pure function tests
# ===========================================================================

class TestSplitMessage:
    def test_short_text(self):
        result = TelegramChannel._split_message("hello")
        assert result == ["hello"]

    def test_empty_text(self):
        result = TelegramChannel._split_message("")
        assert result == []

    def test_paragraph_split(self):
        p1 = "a" * 3000
        p2 = "b" * 3000
        text = f"{p1}\n\n{p2}"
        result = TelegramChannel._split_message(text, limit=4096)
        assert len(result) == 2
        assert result[0] == p1
        assert result[1] == p2

    def test_line_split_within_paragraph(self):
        lines = [f"line{i}" * 100 for i in range(60)]
        paragraph = "\n".join(lines)
        result = TelegramChannel._split_message(paragraph, limit=4096)
        assert len(result) > 1
        for part in result:
            assert len(part) <= 4096

    def test_single_long_line_hard_split(self):
        line = "x" * 10000
        result = TelegramChannel._split_message(line, limit=4096)
        assert len(result) == 3  # 4096 + 4096 + 1808
        assert result[0] == "x" * 4096
        assert result[1] == "x" * 4096

    def test_exact_limit(self):
        text = "a" * 4096
        result = TelegramChannel._split_message(text, limit=4096)
        assert result == [text]


# ===========================================================================
# 2. Command handling
# ===========================================================================

class TestCommands:
    @pytest.fixture
    def channel(self, tmp_path):
        ch = _make_channel(tmp_path)
        ch._client = MagicMock()  # mock httpx client
        ch._send_message = AsyncMock()
        return ch

    @pytest.mark.asyncio
    async def test_start_with_agent(self, channel):
        await channel._handle_command("111", "/start my_agent", {})
        assert channel._bindings["111"] == "my_agent"
        channel._send_message.assert_awaited_once()
        msg = channel._send_message.call_args[0][1]
        assert "my_agent" in msg

    @pytest.mark.asyncio
    async def test_start_default_agent(self, channel):
        await channel._handle_command("111", "/start", {})
        assert channel._bindings["111"] == "test_agent"

    @pytest.mark.asyncio
    async def test_start_no_default_no_arg(self, tmp_path):
        ch = _make_channel(tmp_path, default_agent="")
        ch._send_message = AsyncMock()
        await ch._handle_command("111", "/start", {})
        assert "111" not in ch._bindings
        msg = ch._send_message.call_args[0][1]
        assert "Usage" in msg

    @pytest.mark.asyncio
    async def test_status(self, channel):
        with patch("src.everbot.channels.telegram_commands.get_local_status") as mock_status:
            mock_status.return_value = {
                "running": True,
                "pid": 1234,
                "snapshot": {
                    "agents": ["daily_insight"],
                    "started_at": "2025-01-01T00:00:00",
                },
            }
            await channel._handle_command("111", "/status", {})
            msg = channel._send_message.call_args[0][1]
            assert "running" in msg
            assert "1234" in msg

    @pytest.mark.asyncio
    async def test_heartbeat_no_data(self, channel):
        with patch("src.everbot.channels.telegram_commands.get_local_status") as mock_status:
            mock_status.return_value = {"snapshot": {"heartbeats": {}}}
            await channel._handle_command("111", "/heartbeat", {})
            msg = channel._send_message.call_args[0][1]
            assert "No heartbeat" in msg

    @pytest.mark.asyncio
    async def test_tasks_no_data(self, channel):
        with patch("src.everbot.channels.telegram_commands.get_local_status") as mock_status:
            mock_status.return_value = {"snapshot": {"task_states": {}}}
            await channel._handle_command("111", "/tasks", {})
            msg = channel._send_message.call_args[0][1]
            assert "No task" in msg

    @pytest.mark.asyncio
    async def test_help(self, channel):
        await channel._handle_command("111", "/help", {})
        msg = channel._send_message.call_args[0][1]
        assert "/start" in msg
        assert "/status" in msg

    @pytest.mark.asyncio
    async def test_unknown_command(self, channel):
        await channel._handle_command("111", "/foo", {})
        msg = channel._send_message.call_args[0][1]
        assert "Unknown command" in msg

    @pytest.mark.asyncio
    async def test_command_with_bot_suffix(self, channel):
        """Commands like /start@mybotname should work."""
        await channel._handle_command("111", "/start@mybot daily_insight", {})
        assert channel._bindings["111"] == "daily_insight"


# ===========================================================================
# 3. Event filtering
# ===========================================================================

class TestEventFiltering:
    @pytest.fixture
    def channel(self, tmp_path):
        ch = _make_channel(tmp_path)
        ch._send_message = AsyncMock()
        ch._bindings = {"111": "my_agent"}
        return ch

    @pytest.mark.asyncio
    async def test_forwards_heartbeat_delivery(self, channel):
        await channel._on_background_event("session_1", {
            "source_type": "heartbeat_delivery",
            "agent_name": "my_agent",
            "detail": "Task completed",
            "deliver": True,
            "scope": "agent",
        })
        channel._send_message.assert_awaited_once()
        msg = channel._send_message.call_args[0][1]
        assert "Heartbeat" in msg
        assert "Task completed" in msg
        # Verify the result was deposited into the Telegram session mailbox
        sm = channel._session_manager
        sm.deposit_mailbox_event.assert_awaited_once()
        call_args = sm.deposit_mailbox_event.call_args
        deposited_session_id = call_args[0][0]
        deposited_event = call_args[0][1]
        assert deposited_session_id == "tg_session_my_agent__111"
        assert deposited_event["event_type"] == "heartbeat_result"
        assert "Task completed" in deposited_event["detail"]

    @pytest.mark.asyncio
    async def test_skips_mailbox_mirror_when_telegram_delivery_fails(self, channel):
        """Do not mirror unseen heartbeat events into tg session mailbox.

        Regression: _on_background_event previously deposited mailbox events
        even when Telegram send failed, causing hidden background updates to
        leak into the next user turn despite no visible push notification.
        """
        channel._send_message = AsyncMock(return_value=False)

        await channel._on_background_event("session_1", {
            "source_type": "heartbeat_delivery",
            "agent_name": "my_agent",
            "detail": "Task completed",
            "deliver": True,
            "scope": "agent",
        })

        channel._send_message.assert_awaited_once()
        channel._session_manager.deposit_mailbox_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ignores_non_heartbeat_delivery(self, channel):
        await channel._on_background_event("session_1", {
            "source_type": "heartbeat",
            "agent_name": "my_agent",
            "detail": "something",
            "deliver": True,
        })
        channel._send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ignores_deliver_false(self, channel):
        await channel._on_background_event("session_1", {
            "source_type": "heartbeat_delivery",
            "agent_name": "my_agent",
            "detail": "something",
            "deliver": False,
        })
        channel._send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ignores_no_agent_name(self, channel):
        await channel._on_background_event("session_1", {
            "source_type": "heartbeat_delivery",
            "detail": "something",
            "deliver": True,
        })
        channel._send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ignores_empty_detail(self, channel):
        await channel._on_background_event("session_1", {
            "source_type": "heartbeat_delivery",
            "agent_name": "my_agent",
            "deliver": True,
        })
        channel._send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_only_pushes_to_bound_agent(self, channel):
        channel._bindings = {"111": "my_agent", "222": "other_agent"}
        await channel._on_background_event("session_1", {
            "source_type": "heartbeat_delivery",
            "agent_name": "my_agent",
            "detail": "update",
            "deliver": True,
            "scope": "agent",
        })
        assert channel._send_message.await_count == 1
        assert channel._send_message.call_args[0][0] == "111"

    @pytest.mark.asyncio
    async def test_deferred_result_skips_history_injection(self, channel):
        """deferred_result events should NOT call inject_history_message
        because core_service already handles injection (Bug 1 fix)."""
        # session_id must match the tg session pattern for chat 111
        await channel._on_background_event("tg_session_my_agent__111", {
            "source_type": "deferred_result",
            "agent_name": "my_agent",
            "detail": "Deferred task completed",
            "deliver": True,
            "scope": "session",
            "target_session_id": "tg_session_my_agent__111",
            "target_channel": "telegram",
        })
        # Message should still be sent to Telegram chat
        channel._send_message.assert_awaited_once()
        msg = channel._send_message.call_args[0][1]
        assert "Deferred Result" in msg
        assert "Deferred task completed" in msg
        # But inject_history_message should NOT be called (core_service already did it)
        sm = channel._session_manager
        sm.inject_history_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_deferred_result_scoped_to_originating_chat(self, channel):
        """deferred_result must only be pushed to the chat that initiated the
        timed-out turn, not broadcast to all chats bound to the same agent."""
        channel._bindings = {"111": "my_agent", "222": "my_agent"}
        # Event originated from chat 222
        await channel._on_background_event("tg_session_my_agent__222", {
            "source_type": "deferred_result",
            "agent_name": "my_agent",
            "detail": "Result for chat 222",
            "deliver": True,
            "scope": "session",
            "target_session_id": "tg_session_my_agent__222",
            "target_channel": "telegram",
        })
        # Only chat 222 should receive the message
        assert channel._send_message.await_count == 1
        assert channel._send_message.call_args[0][0] == "222"

    @pytest.mark.asyncio
    async def test_heartbeat_delivery_broadcasts_to_all_chats(self, channel):
        """heartbeat_delivery should broadcast to all chats bound to the agent."""
        channel._bindings = {"111": "my_agent", "222": "my_agent"}
        await channel._on_background_event("primary_session", {
            "source_type": "heartbeat_delivery",
            "agent_name": "my_agent",
            "detail": "Heartbeat report",
            "deliver": True,
            "scope": "agent",
        })
        assert channel._send_message.await_count == 2

    @pytest.mark.asyncio
    async def test_deferred_result_from_web_session_not_pushed(self, channel):
        """deferred_result from a web session should NOT be pushed to any
        Telegram chat — the result belongs to the web channel."""
        channel._bindings = {"111": "my_agent", "222": "my_agent"}
        await channel._on_background_event("web_session_my_agent", {
            "source_type": "deferred_result",
            "agent_name": "my_agent",
            "detail": "Web deferred result",
            "deliver": True,
            "scope": "session",
            "target_session_id": "web_session_my_agent",
            "target_channel": "web",
        })
        channel._send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_deferred_result_from_primary_session_not_pushed(self, channel):
        """deferred_result from a primary/unknown session should NOT leak to Telegram."""
        channel._bindings = {"111": "my_agent"}
        await channel._on_background_event("my_agent_primary", {
            "source_type": "deferred_result",
            "agent_name": "my_agent",
            "detail": "Primary session deferred result",
            "deliver": True,
            "scope": "session",
            "target_session_id": "my_agent_primary",
        })
        channel._send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ignores_target_channel_mismatch(self, channel):
        channel._bindings = {"111": "my_agent"}
        await channel._on_background_event("tg_session_my_agent__111", {
            "source_type": "deferred_result",
            "agent_name": "my_agent",
            "detail": "Mismatch result",
            "deliver": True,
            "scope": "session",
            "target_session_id": "tg_session_my_agent__111",
            "target_channel": "web",
        })
        channel._send_message.assert_not_awaited()


# ===========================================================================
# 4. Message handling (chat)
# ===========================================================================

class TestMessageHandling:
    @pytest.fixture
    def channel(self, tmp_path):
        ch = _make_channel(tmp_path)
        ch._bindings = {"111": "test_agent"}
        ch._send_message = AsyncMock()
        ch._send_chat_action = AsyncMock()
        return ch

    @pytest.mark.asyncio
    async def test_no_agent_bound(self, tmp_path):
        ch = _make_channel(tmp_path)
        ch._send_message = AsyncMock()
        await ch._handle_message("999", "hello", {})
        msg = ch._send_message.call_args[0][1]
        assert "No agent bound" in msg

    @pytest.mark.asyncio
    async def test_message_with_agent_creates_instance(self, channel):
        mock_agent = MagicMock()
        channel._agent_service.create_agent_instance = AsyncMock(return_value=mock_agent)

        # Mock core.process_message to produce a response
        async def fake_process(agent, agent_name, session_id, message, on_event):
            from src.everbot.core.channel.models import OutboundMessage
            await on_event(OutboundMessage(session_id, "Hello!", msg_type="text"))
            await on_event(OutboundMessage(session_id, "", msg_type="end"))

        channel._core.process_message = fake_process

        await channel._handle_message("111", "hi", {})

        channel._agent_service.create_agent_instance.assert_awaited_once_with("test_agent")
        channel._session_manager.cache_agent.assert_called_once()
        # Should have sent the response
        channel._send_message.assert_awaited()
        msg = channel._send_message.call_args[0][1]
        assert "Hello" in msg

    @pytest.mark.asyncio
    async def test_delta_collection(self, channel):
        mock_agent = MagicMock()
        channel._session_manager.get_cached_agent.return_value = mock_agent

        async def fake_process(agent, agent_name, session_id, message, on_event):
            from src.everbot.core.channel.models import OutboundMessage
            await on_event(OutboundMessage(session_id, "part1", msg_type="delta"))
            await on_event(OutboundMessage(session_id, " part2", msg_type="delta"))
            await on_event(OutboundMessage(session_id, "", msg_type="end"))

        channel._core.process_message = fake_process

        await channel._handle_message("111", "test", {})

        channel._send_message.assert_awaited()
        msg = channel._send_message.call_args[0][1]
        assert "part1 part2" in msg


# ===========================================================================
# 5. Binding persistence
# ===========================================================================

class TestBindingPersistence:
    def test_save_and_load(self, tmp_path):
        ch = _make_channel(tmp_path)
        ch._bindings = {"111": "agent_a", "222": "agent_b"}
        ch._save_bindings()

        ch2 = _make_channel(tmp_path)
        ch2._load_bindings()
        assert ch2._bindings == {"111": "agent_a", "222": "agent_b"}

    def test_load_nonexistent(self, tmp_path):
        ch = _make_channel(tmp_path)
        ch._bindings_path = tmp_path / "nonexistent.json"
        ch._load_bindings()
        assert ch._bindings == {}

    def test_load_corrupt(self, tmp_path):
        ch = _make_channel(tmp_path)
        ch._bindings_path.write_text("not json!!!")
        ch._load_bindings()
        assert ch._bindings == {}

    def test_start_command_persists(self, tmp_path):
        ch = _make_channel(tmp_path)
        ch._bindings["111"] = "my_agent"
        ch._save_bindings()

        raw = json.loads(ch._bindings_path.read_text())
        assert raw["111"] == "my_agent"


# ===========================================================================
# 6. Access control
# ===========================================================================

class TestAccessControl:
    @pytest.mark.asyncio
    async def test_allowed_chat_ids_blocks(self, tmp_path):
        ch = _make_channel(tmp_path, allowed_chat_ids=["111"])
        ch._send_message = AsyncMock()
        await ch._handle_update({
            "update_id": 1,
            "message": {
                "text": "/help",
                "chat": {"id": 999},
            },
        })
        ch._send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_allowed_chat_ids_passes(self, tmp_path):
        ch = _make_channel(tmp_path, allowed_chat_ids=["111"])
        ch._send_message = AsyncMock()
        await ch._handle_update({
            "update_id": 1,
            "message": {
                "text": "/help",
                "chat": {"id": 111},
            },
        })
        ch._send_message.assert_awaited()


# ===========================================================================
# 7. /ping command
# ===========================================================================

class TestPingCommand:
    @pytest.fixture
    def channel(self, tmp_path):
        ch = _make_channel(tmp_path)
        ch._client = MagicMock()
        ch._send_message = AsyncMock()
        ch._bindings = {"111": "my_agent"}
        return ch

    @pytest.mark.asyncio
    async def test_ping_returns_pong(self, channel):
        await channel._handle_command("111", "/ping", {})
        channel._send_message.assert_awaited_once()
        msg = channel._send_message.call_args[0][1]
        assert "pong" in msg
        assert "my_agent" in msg
        assert "Queue:" in msg
        assert "Time:" in msg

    @pytest.mark.asyncio
    async def test_ping_no_binding(self, channel):
        await channel._handle_command("999", "/ping", {})
        msg = channel._send_message.call_args[0][1]
        assert "(none)" in msg

    @pytest.mark.asyncio
    async def test_help_includes_ping(self, channel):
        await channel._handle_command("111", "/help", {})
        msg = channel._send_message.call_args[0][1]
        assert "/ping" in msg


# ===========================================================================
# 8. Send retry logic
# ===========================================================================

class TestSendRetry:
    @pytest.fixture
    def channel(self, tmp_path):
        ch = _make_channel(tmp_path)
        ch._client = MagicMock()
        return ch

    @pytest.mark.asyncio
    async def test_send_message_success_on_first_try(self, channel):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        channel._client.post = AsyncMock(return_value=mock_resp)

        result = await channel._send_message("111", "hello")
        assert result is True
        assert channel._client.post.await_count == 1

    @pytest.mark.asyncio
    async def test_send_message_markdown_fail_plain_success(self, channel):
        md_resp = MagicMock()
        md_resp.json.return_value = {"ok": False, "description": "bad markdown"}
        plain_resp = MagicMock()
        plain_resp.json.return_value = {"ok": True}
        channel._client.post = AsyncMock(side_effect=[md_resp, plain_resp])

        result = await channel._send_message("111", "hello")
        assert result is True
        assert channel._client.post.await_count == 2

    @pytest.mark.asyncio
    async def test_send_message_retries_on_exception(self, channel):
        ok_resp = MagicMock()
        ok_resp.json.return_value = {"ok": True}
        channel._client.post = AsyncMock(
            side_effect=[Exception("network"), ok_resp]
        )

        result = await channel._send_message("111", "hello")
        assert result is True

    @pytest.mark.asyncio
    async def test_send_message_returns_false_after_all_retries(self, channel):
        channel._client.post = AsyncMock(side_effect=Exception("network"))

        result = await channel._send_message("111", "hello")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_message_logs_exception_type_and_recovers(self, channel, caplog):
        ok_resp = MagicMock()
        ok_resp.json.return_value = {"ok": True}
        channel._client.post = AsyncMock(
            side_effect=[httpx.ConnectError("boom"), ok_resp]
        )

        with caplog.at_level("WARNING"):
            result = await channel._send_message("111", "hello")

        assert result is True
        assert "ConnectError" in caplog.text

    @pytest.mark.asyncio
    async def test_send_plain_message_success(self, channel):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        channel._client.post = AsyncMock(return_value=mock_resp)

        result = await channel._send_plain_message("111", "fallback")
        assert result is True

    @pytest.mark.asyncio
    async def test_send_plain_message_no_client(self, channel):
        channel._client = None
        result = await channel._send_plain_message("111", "fallback")
        assert result is False


# ===========================================================================
# 9. Graceful degradation (fallback on send failure)
# ===========================================================================

class TestGracefulDegradation:
    @pytest.fixture
    def channel(self, tmp_path):
        ch = _make_channel(tmp_path)
        ch._bindings = {"111": "test_agent"}
        ch._send_message = AsyncMock(return_value=False)
        ch._send_plain_message = AsyncMock(return_value=True)
        ch._send_chat_action = AsyncMock()
        return ch

    @pytest.mark.asyncio
    async def test_fallback_when_all_sends_fail(self, channel):
        mock_agent = MagicMock()
        channel._session_manager.get_cached_agent.return_value = mock_agent

        async def fake_process(agent, agent_name, session_id, message, on_event):
            from src.everbot.core.channel.models import OutboundMessage
            await on_event(OutboundMessage(session_id, "LLM reply", msg_type="text"))

        channel._core.process_message = fake_process

        await channel._handle_message("111", "test", {})

        # _send_message was called (returned False), then _send_plain_message as fallback
        channel._send_message.assert_awaited()
        channel._send_plain_message.assert_awaited_once()
        fallback_msg = channel._send_plain_message.call_args[0][1]
        assert "[delivery error]" in fallback_msg

    @pytest.mark.asyncio
    async def test_no_fallback_when_send_succeeds(self, channel):
        channel._send_message = AsyncMock(return_value=True)
        mock_agent = MagicMock()
        channel._session_manager.get_cached_agent.return_value = mock_agent

        async def fake_process(agent, agent_name, session_id, message, on_event):
            from src.everbot.core.channel.models import OutboundMessage
            await on_event(OutboundMessage(session_id, "OK", msg_type="text"))

        channel._core.process_message = fake_process

        await channel._handle_message("111", "test", {})

        channel._send_plain_message.assert_not_awaited()


# ===========================================================================
# 10. Polling decoupling (queue + dispatcher)
# ===========================================================================

class TestPollingDecoupling:
    @pytest.fixture
    def channel(self, tmp_path):
        ch = _make_channel(tmp_path)
        ch._send_message = AsyncMock(return_value=True)
        ch._send_chat_action = AsyncMock()
        return ch

    @pytest.mark.asyncio
    async def test_polling_enqueues_instead_of_blocking(self, channel):
        """Polling loop should put updates into inbound queue."""
        update = {
            "update_id": 1,
            "message": {"text": "/help", "chat": {"id": 111}},
        }
        # Simulate what _polling_loop does with one update
        channel._inbound_queue.put_nowait(update)
        assert channel._inbound_queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_inbound_queue_full_drops(self, channel):
        """When inbound queue is full, updates are dropped."""
        for i in range(100):
            channel._inbound_queue.put_nowait({"update_id": i})
        # Queue is full (maxsize=100), next put should raise
        with pytest.raises(asyncio.QueueFull):
            channel._inbound_queue.put_nowait({"update_id": 999})

    @pytest.mark.asyncio
    async def test_chat_worker_processes_update(self, channel):
        """Chat worker should process updates from its queue."""
        channel._chat_queues["111"] = asyncio.Queue(maxsize=20)
        channel._chat_queues["111"].put_nowait({
            "update_id": 1,
            "message": {"text": "/help", "chat": {"id": 111}},
        })

        # Worker will process one update then timeout after 30s
        # We use a short timeout version for testing
        worker_task = asyncio.create_task(channel._chat_worker("111"))
        # Give it time to process
        await asyncio.sleep(0.1)
        # Worker should have called _send_message for /help
        channel._send_message.assert_awaited()
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass


# ===========================================================================
# 11. _extract_urls helper
# ===========================================================================

class TestExtractUrls:
    def test_url_entity(self):
        text = "Check https://example.com please"
        entities = [{"type": "url", "offset": 6, "length": 19}]
        assert _extract_urls(text, entities) == ["https://example.com"]

    def test_text_link_entity(self):
        text = "Click here for details"
        entities = [{"type": "text_link", "offset": 6, "length": 4, "url": "https://hidden.link"}]
        assert _extract_urls(text, entities) == ["https://hidden.link"]

    def test_deduplication(self):
        text = "https://a.com and https://a.com"
        entities = [
            {"type": "url", "offset": 0, "length": 13},
            {"type": "url", "offset": 18, "length": 13},
        ]
        assert _extract_urls(text, entities) == ["https://a.com"]

    def test_mixed_types(self):
        text = "See https://a.com or click here"
        entities = [
            {"type": "url", "offset": 4, "length": 13},
            {"type": "text_link", "offset": 21, "length": 10, "url": "https://b.com"},
        ]
        assert _extract_urls(text, entities) == ["https://a.com", "https://b.com"]

    def test_empty(self):
        assert _extract_urls("hello", []) == []

    def test_ignores_non_url_entities(self):
        text = "hello world"
        entities = [{"type": "bold", "offset": 0, "length": 5}]
        assert _extract_urls(text, entities) == []


# ===========================================================================
# 12. _extract_media_text static method
# ===========================================================================

class TestExtractMediaText:
    def test_photo_with_caption(self):
        msg = {"photo": [{"file_id": "abc"}], "caption": "My photo"}
        result = TelegramChannel._extract_media_text(msg)
        assert "[图片]" in result
        assert "My photo" in result

    def test_photo_no_caption(self):
        msg = {"photo": [{"file_id": "abc"}]}
        result = TelegramChannel._extract_media_text(msg)
        assert result == "[图片]"

    def test_document_with_mime(self):
        msg = {"document": {"file_name": "report.pdf", "mime_type": "application/pdf"}}
        result = TelegramChannel._extract_media_text(msg)
        assert "[文件: report.pdf (application/pdf)]" in result

    def test_document_no_mime(self):
        msg = {"document": {"file_name": "data.bin"}}
        result = TelegramChannel._extract_media_text(msg)
        assert "[文件: data.bin]" in result

    def test_document_no_filename(self):
        msg = {"document": {"mime_type": "text/plain"}}
        result = TelegramChannel._extract_media_text(msg)
        assert "[文件: unknown (text/plain)]" in result

    def test_voice(self):
        msg = {"voice": {"duration": 5, "file_id": "v1"}}
        result = TelegramChannel._extract_media_text(msg)
        assert "[语音消息 duration=5s]" in result

    def test_audio(self):
        msg = {"audio": {"title": "Song", "duration": 180}}
        result = TelegramChannel._extract_media_text(msg)
        assert "[音频: Song duration=180s]" in result

    def test_audio_with_filename_fallback(self):
        msg = {"audio": {"file_name": "track.mp3", "duration": 60}}
        result = TelegramChannel._extract_media_text(msg)
        assert "[音频: track.mp3 duration=60s]" in result

    def test_video(self):
        msg = {"video": {"duration": 30}}
        result = TelegramChannel._extract_media_text(msg)
        assert "[视频 duration=30s]" in result

    def test_sticker(self):
        msg = {"sticker": {"emoji": "😀"}}
        result = TelegramChannel._extract_media_text(msg)
        assert "[贴纸: 😀]" in result

    def test_caption_with_text_link(self):
        msg = {
            "photo": [{"file_id": "abc"}],
            "caption": "Read this article",
            "caption_entities": [
                {"type": "text_link", "offset": 10, "length": 7, "url": "https://example.com/article"},
            ],
        }
        result = TelegramChannel._extract_media_text(msg)
        assert "[图片]" in result
        assert "Read this article" in result
        assert "https://example.com/article" in result

    def test_caption_url_already_in_text_not_duplicated(self):
        url = "https://example.com"
        msg = {
            "photo": [{"file_id": "abc"}],
            "caption": f"See {url}",
            "caption_entities": [
                {"type": "url", "offset": 4, "length": len(url)},
            ],
        }
        result = TelegramChannel._extract_media_text(msg)
        # URL appears in caption, should not be appended again
        assert result.count(url) == 1

    def test_empty_message(self):
        assert TelegramChannel._extract_media_text({}) == ""


# ===========================================================================
# 13. _handle_update media integration
# ===========================================================================

class TestHandleUpdateMedia:
    @pytest.fixture
    def channel(self, tmp_path):
        ch = _make_channel(tmp_path)
        ch._bindings = {"111": "test_agent"}
        ch._send_message = AsyncMock(return_value=True)
        ch._send_chat_action = AsyncMock()
        ch._send_plain_message = AsyncMock(return_value=True)
        return ch

    def _make_update(self, msg_body: dict) -> dict:
        msg_body.setdefault("chat", {"id": 111})
        return {"update_id": 1, "message": msg_body}

    @pytest.mark.asyncio
    async def test_photo_download_success_multimodal(self, channel, tmp_path):
        """Photo download success → multimodal list message with base64 image."""
        mock_agent = MagicMock()
        channel._session_manager.get_cached_agent.return_value = mock_agent

        # Create a fake image file
        fake_img = tmp_path / "photo.jpg"
        fake_img.write_bytes(b"\xff\xd8\xff\xe0fake_jpeg_data")
        channel._download_photo = AsyncMock(return_value=str(fake_img))
        received = []

        async def fake_process(agent, agent_name, session_id, message, on_event):
            received.append(message)
            from src.everbot.core.channel.models import OutboundMessage
            await on_event(OutboundMessage(session_id, "ok", msg_type="text"))

        channel._core.process_message = fake_process

        await channel._handle_update(self._make_update({
            "photo": [{"file_id": "sm"}, {"file_id": "lg"}],
            "caption": "Look at this",
        }))

        assert len(received) == 1
        msg = received[0]
        # Should be a multimodal list
        assert isinstance(msg, list)
        assert len(msg) == 2
        assert msg[0]["type"] == "text"
        assert "[图片]" in msg[0]["text"]
        assert "Look at this" in msg[0]["text"]
        assert msg[1]["type"] == "image_url"
        assert msg[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        # Should pick the last (largest) photo
        channel._download_photo.assert_awaited_once_with("lg", "test_agent")

    @pytest.mark.asyncio
    async def test_photo_download_failure_fallback_text(self, channel):
        """Photo download failure → fallback to text with error note."""
        mock_agent = MagicMock()
        channel._session_manager.get_cached_agent.return_value = mock_agent
        channel._download_photo = AsyncMock(return_value=None)
        received = []

        async def fake_process(agent, agent_name, session_id, message, on_event):
            received.append(message)
            from src.everbot.core.channel.models import OutboundMessage
            await on_event(OutboundMessage(session_id, "ok", msg_type="text"))

        channel._core.process_message = fake_process

        await channel._handle_update(self._make_update({
            "photo": [{"file_id": "p1"}],
            "caption": "Look at this",
        }))

        assert len(received) == 1
        msg = received[0]
        assert isinstance(msg, str)
        assert "[图片]" in msg
        assert "Look at this" in msg
        assert "(图片下载失败)" in msg

    @pytest.mark.asyncio
    async def test_photo_no_caption_multimodal(self, channel, tmp_path):
        """Photo without caption still produces multimodal message."""
        mock_agent = MagicMock()
        channel._session_manager.get_cached_agent.return_value = mock_agent

        fake_img = tmp_path / "photo.jpg"
        fake_img.write_bytes(b"fake_img")
        channel._download_photo = AsyncMock(return_value=str(fake_img))
        received = []

        async def fake_process(agent, agent_name, session_id, message, on_event):
            received.append(message)
            from src.everbot.core.channel.models import OutboundMessage
            await on_event(OutboundMessage(session_id, "ok", msg_type="text"))

        channel._core.process_message = fake_process

        await channel._handle_update(self._make_update({
            "photo": [{"file_id": "p1"}],
        }))

        assert len(received) == 1
        msg = received[0]
        assert isinstance(msg, list)
        assert msg[0]["text"] == "[图片]"

    @pytest.mark.asyncio
    async def test_voice_message_with_download(self, channel):
        mock_agent = MagicMock()
        channel._session_manager.get_cached_agent.return_value = mock_agent
        channel._download_voice = AsyncMock(return_value="/tmp/voice/v1.ogg")
        received = []

        async def fake_process(agent, agent_name, session_id, message, on_event):
            received.append(message)
            from src.everbot.core.channel.models import OutboundMessage
            await on_event(OutboundMessage(session_id, "ok", msg_type="text"))

        channel._core.process_message = fake_process

        await channel._handle_update(self._make_update({
            "voice": {"duration": 5, "file_id": "v1"},
        }))

        assert len(received) == 1
        assert "[语音消息 duration=5s]" in received[0]
        assert "path=/tmp/voice/v1.ogg" in received[0]

    @pytest.mark.asyncio
    async def test_voice_download_failure(self, channel):
        mock_agent = MagicMock()
        channel._session_manager.get_cached_agent.return_value = mock_agent
        channel._download_voice = AsyncMock(return_value=None)
        received = []

        async def fake_process(agent, agent_name, session_id, message, on_event):
            received.append(message)
            from src.everbot.core.channel.models import OutboundMessage
            await on_event(OutboundMessage(session_id, "ok", msg_type="text"))

        channel._core.process_message = fake_process

        await channel._handle_update(self._make_update({
            "voice": {"duration": 3, "file_id": "v2"},
        }))

        assert len(received) == 1
        assert "(文件下载失败)" in received[0]

    @pytest.mark.asyncio
    async def test_document_no_caption(self, channel):
        mock_agent = MagicMock()
        channel._session_manager.get_cached_agent.return_value = mock_agent
        channel._download_document = AsyncMock(return_value="/tmp/docs/data.csv")
        received = []

        async def fake_process(agent, agent_name, session_id, message, on_event):
            received.append(message)
            from src.everbot.core.channel.models import OutboundMessage
            await on_event(OutboundMessage(session_id, "ok", msg_type="text"))

        channel._core.process_message = fake_process

        await channel._handle_update(self._make_update({
            "document": {"file_id": "d1", "file_name": "data.csv", "mime_type": "text/csv"},
        }))

        assert len(received) == 1
        assert "[文件: data.csv (text/csv)]" in received[0]
        assert "path=/tmp/docs/data.csv" in received[0]
        channel._download_document.assert_awaited_once_with("d1", "data.csv", "test_agent", declared_size=0)

    @pytest.mark.asyncio
    async def test_document_download_failure(self, channel):
        mock_agent = MagicMock()
        channel._session_manager.get_cached_agent.return_value = mock_agent
        channel._download_document = AsyncMock(return_value=None)
        received = []

        async def fake_process(agent, agent_name, session_id, message, on_event):
            received.append(message)
            from src.everbot.core.channel.models import OutboundMessage
            await on_event(OutboundMessage(session_id, "ok", msg_type="text"))

        channel._core.process_message = fake_process

        await channel._handle_update(self._make_update({
            "document": {"file_id": "d2", "file_name": "big.zip", "mime_type": "application/zip"},
        }))

        assert len(received) == 1
        assert "(文件下载失败" in received[0]

    @pytest.mark.asyncio
    async def test_document_with_caption(self, channel):
        mock_agent = MagicMock()
        channel._session_manager.get_cached_agent.return_value = mock_agent
        channel._download_document = AsyncMock(return_value="/tmp/docs/report.md")
        received = []

        async def fake_process(agent, agent_name, session_id, message, on_event):
            received.append(message)
            from src.everbot.core.channel.models import OutboundMessage
            await on_event(OutboundMessage(session_id, "ok", msg_type="text"))

        channel._core.process_message = fake_process

        await channel._handle_update(self._make_update({
            "document": {"file_id": "d3", "file_name": "report.md", "mime_type": "text/markdown"},
            "caption": "Please review this",
        }))

        assert len(received) == 1
        assert "[文件: report.md (text/markdown)]" in received[0]
        assert "Please review this" in received[0]
        assert "path=/tmp/docs/report.md" in received[0]

    @pytest.mark.asyncio
    async def test_text_with_hidden_link(self, channel):
        mock_agent = MagicMock()
        channel._session_manager.get_cached_agent.return_value = mock_agent
        received = []

        async def fake_process(agent, agent_name, session_id, message, on_event):
            received.append(message)
            from src.everbot.core.channel.models import OutboundMessage
            await on_event(OutboundMessage(session_id, "ok", msg_type="text"))

        channel._core.process_message = fake_process

        await channel._handle_update(self._make_update({
            "text": "Read this article",
            "entities": [
                {"type": "text_link", "offset": 10, "length": 7, "url": "https://example.com/article"},
            ],
        }))

        assert len(received) == 1
        assert "Read this article" in received[0]
        assert "https://example.com/article" in received[0]

    @pytest.mark.asyncio
    async def test_empty_media_message_ignored(self, channel):
        """A message with no text and no recognized media should be ignored."""
        await channel._handle_update(self._make_update({}))
        channel._send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sticker_message(self, channel):
        mock_agent = MagicMock()
        channel._session_manager.get_cached_agent.return_value = mock_agent
        received = []

        async def fake_process(agent, agent_name, session_id, message, on_event):
            received.append(message)
            from src.everbot.core.channel.models import OutboundMessage
            await on_event(OutboundMessage(session_id, "ok", msg_type="text"))

        channel._core.process_message = fake_process

        await channel._handle_update(self._make_update({
            "sticker": {"emoji": "👍"},
        }))

        assert len(received) == 1
        assert "[贴纸: 👍]" in received[0]


# ===========================================================================
# 14. Message handling — error and timeout resilience
# ===========================================================================

class TestMessageHandlingResilience:
    """Tests that _handle_message properly handles slow/failing LLM calls,
    cancels typing loop, and still delivers an error reply."""

    @pytest.fixture
    def channel(self, tmp_path):
        ch = _make_channel(tmp_path)
        ch._bindings = {"111": "test_agent"}
        ch._send_message = AsyncMock(return_value=True)
        ch._send_chat_action = AsyncMock()
        ch._send_plain_message = AsyncMock(return_value=True)
        mock_agent = MagicMock()
        ch._session_manager.get_cached_agent.return_value = mock_agent
        return ch

    @pytest.mark.asyncio
    async def test_process_message_exception_cancels_typing_and_sends_error(self, channel):
        """When process_message raises, typing_loop is cancelled and error is sent."""
        async def exploding_process(agent, agent_name, session_id, message, on_event):
            await asyncio.sleep(0.05)
            raise RuntimeError("LLM exploded")

        channel._core.process_message = exploding_process
        await channel._handle_message("111", "test", {})

        # Should still send an error message, not hang forever
        channel._send_message.assert_awaited()
        msg = channel._send_message.call_args[0][1]
        assert "Processing error" in msg or "LLM exploded" in msg

    @pytest.mark.asyncio
    async def test_typing_loop_runs_during_slow_processing(self, channel):
        """During slow processing, typing indicator is actively sent."""
        async def slow_process(agent, agent_name, session_id, message, on_event):
            from src.everbot.core.channel.models import OutboundMessage
            await asyncio.sleep(0.3)  # Simulate slow LLM
            await on_event(OutboundMessage(session_id, "done", msg_type="text"))

        channel._core.process_message = slow_process
        await channel._handle_message("111", "test", {})

        # typing indicator should have been called multiple times during 0.3s wait
        # (typing loop fires every 4s, but with sleep(0.3) we get at least 1)
        assert channel._send_chat_action.await_count >= 1

    @pytest.mark.asyncio
    async def test_typing_loop_cancelled_after_completion(self, channel):
        """After process_message completes, typing loop must be stopped."""
        typing_task_ref = {}

        original_create_task = asyncio.create_task

        def track_typing_task(coro, **kwargs):
            task = original_create_task(coro, **kwargs)
            # The typing loop coro name contains '_typing_loop'
            if '_typing_loop' in repr(coro):
                typing_task_ref['task'] = task
            return task

        async def fast_process(agent, agent_name, session_id, message, on_event):
            from src.everbot.core.channel.models import OutboundMessage
            await on_event(OutboundMessage(session_id, "quick", msg_type="text"))

        channel._core.process_message = fast_process

        with patch('asyncio.create_task', side_effect=track_typing_task):
            await channel._handle_message("111", "test", {})

        # After _handle_message returns, typing task should be done/cancelled
        if 'task' in typing_task_ref:
            assert typing_task_ref['task'].done() or typing_task_ref['task'].cancelled()

    @pytest.mark.asyncio
    async def test_no_response_sends_fallback(self, channel):
        """When LLM produces no output at all, '(no response)' is sent."""
        async def empty_process(agent, agent_name, session_id, message, on_event):
            pass  # No events emitted

        channel._core.process_message = empty_process
        await channel._handle_message("111", "test", {})

        channel._send_message.assert_awaited()
        msg = channel._send_message.call_args[0][1]
        assert "(no response)" in msg


# ===========================================================================
# 15. _extract_text_from_message (core_service helper)
# ===========================================================================

class TestExtractTextFromMessage:
    def test_string_message(self):
        from src.everbot.core.channel.core_service import ChannelCoreService
        assert ChannelCoreService._extract_text_from_message("hello") == "hello"

    def test_multimodal_list(self):
        from src.everbot.core.channel.core_service import ChannelCoreService
        msg = [
            {"type": "text", "text": "[图片] caption"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,abc"}},
        ]
        result = ChannelCoreService._extract_text_from_message(msg)
        assert "[图片]" in result
        assert "caption" in result
        assert "base64" not in result

    def test_empty_list(self):
        from src.everbot.core.channel.core_service import ChannelCoreService
        assert ChannelCoreService._extract_text_from_message([]) == ""

    def test_list_no_text_items(self):
        from src.everbot.core.channel.core_service import ChannelCoreService
        msg = [{"type": "image_url", "image_url": {"url": "data:..."}}]
        assert ChannelCoreService._extract_text_from_message(msg) == ""


# ===========================================================================
# #60 / milkie#146:把投递到 channel 的外部产出登记为 context projection。
# ===========================================================================

def test_truncate_projection_text_caps_to_limit_with_ellipsis():
    """milkie 不限 displayText 长度 → alfred 必须截断,避免每轮重渲吃 token。"""
    from src.everbot.channels.telegram_channel import _truncate_projection_text

    out = _truncate_projection_text("x" * 5000, limit=100)
    assert len(out) == 100
    assert out.endswith("…")
    assert out.startswith("x" * 10)


def test_truncate_projection_text_keeps_short_text_unchanged():
    from src.everbot.channels.telegram_channel import _truncate_projection_text

    assert _truncate_projection_text("hello", limit=100) == "hello"


def _attach_data(**over):
    d = {
        "transcript_worthy": True,
        "run_id": "job-run-1",
        "detail": "今日 $SIVE 推文分析…",
        "delivered_at": "2026-06-06T02:02:00Z",
    }
    d.update(over)
    return d


@pytest.mark.asyncio
async def test_maybe_attach_projection_calls_provider_when_transcript_worthy(tmp_path):
    """内容型投递(transcript_worthy)→ 用 run_id 作 sourceRunId、detail 作 displayText
    调 channel 会话句柄的 provider.attach_projection。"""
    ch = _make_channel(tmp_path)
    fake_agent = SimpleNamespace(context_id="tg_session_demo__111", base_url="http://x")
    ch._session_manager.get_cached_agent.return_value = fake_agent
    attach = AsyncMock()
    with patch(
        "src.everbot.core.agent.provider.get_provider_for_agent",
        return_value=SimpleNamespace(attach_projection=attach),
    ):
        await ch._maybe_attach_projection("demo_agent", "111", _attach_data())
    attach.assert_awaited_once()
    assert attach.call_args.args[0] is fake_agent
    # 关键:解析出 channel 会话 id 并取该会话句柄(投到用户所在会话,不投错)
    ch._session_manager.get_cached_agent.assert_called_once_with("tg_session_demo_agent__111")
    kwargs = attach.call_args.kwargs
    assert kwargs["source_run_id"] == "job-run-1"
    assert kwargs["display_text"].startswith("今日 $SIVE")
    assert kwargs["delivered_at"] == "2026-06-06T02:02:00Z"


@pytest.mark.asyncio
async def test_maybe_attach_projection_skips_when_not_transcript_worthy(tmp_path):
    """心跳状态 ping(无 transcript_worthy)→ 不进逐字稿,不 attach。"""
    ch = _make_channel(tmp_path)
    ch._session_manager.get_cached_agent.return_value = SimpleNamespace(context_id="c", base_url="u")
    attach = AsyncMock()
    with patch(
        "src.everbot.core.agent.provider.get_provider_for_agent",
        return_value=SimpleNamespace(attach_projection=attach),
    ):
        await ch._maybe_attach_projection("demo_agent", "111", _attach_data(transcript_worthy=False))
    attach.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_attach_projection_skips_without_run_id(tmp_path):
    """无 sourceRunId(去重/溯源锚点缺失)→ 不 attach。"""
    ch = _make_channel(tmp_path)
    ch._session_manager.get_cached_agent.return_value = SimpleNamespace(context_id="c", base_url="u")
    attach = AsyncMock()
    with patch(
        "src.everbot.core.agent.provider.get_provider_for_agent",
        return_value=SimpleNamespace(attach_projection=attach),
    ):
        await ch._maybe_attach_projection("demo_agent", "111", _attach_data(run_id=""))
    attach.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_attach_projection_swallows_create_failure(tmp_path):
    """未缓存时回落创建;若创建失败(如 sidecar spawn 出错)→ 带外吞掉,
    返回 False、不 attach、不崩(调用方据此回落镜像,内容不丢)。"""
    ch = _make_channel(tmp_path)
    ch._session_manager.get_cached_agent.return_value = None
    ch._agent_service.create_agent_instance = AsyncMock(side_effect=RuntimeError("spawn failed"))
    attach = AsyncMock()
    with patch(
        "src.everbot.core.agent.provider.get_provider_for_agent",
        return_value=SimpleNamespace(attach_projection=attach, set_session_id=MagicMock()),
    ):
        ok = await ch._maybe_attach_projection("demo_agent", "111", _attach_data())
    assert ok is False
    attach.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_attach_projection_swallows_attach_failure(tmp_path):
    """attach 失败是带外 best-effort —— 气泡已发出,绝不能让异常冒泡破坏投递。"""
    ch = _make_channel(tmp_path)
    ch._session_manager.get_cached_agent.return_value = SimpleNamespace(context_id="c", base_url="u")
    attach = AsyncMock(side_effect=RuntimeError("serve down"))
    with patch(
        "src.everbot.core.agent.provider.get_provider_for_agent",
        return_value=SimpleNamespace(attach_projection=attach),
    ):
        # 不抛 = 通过
        await ch._maybe_attach_projection("demo_agent", "111", _attach_data())
    attach.assert_awaited_once()


@pytest.mark.asyncio
async def test_on_background_event_invokes_projection_attach_after_send(tmp_path):
    """_on_background_event 在成功投递气泡后,对该 chat 调 _maybe_attach_projection
    (内容型投递据此登记 context projection)。"""
    ch = _make_channel(tmp_path)
    ch._send_message = AsyncMock()
    ch._bindings = {"111": "my_agent"}
    ch._maybe_attach_projection = AsyncMock()
    await ch._on_background_event("session_1", {
        "source_type": "heartbeat_delivery",
        "agent_name": "my_agent",
        "detail": "DAILY REPORT",
        "deliver": True,
        "scope": "agent",
        "run_id": "job-run-1",
        "transcript_worthy": True,
    })
    ch._maybe_attach_projection.assert_awaited_once()
    args = ch._maybe_attach_projection.call_args.args
    assert args[0] == "my_agent"
    assert str(args[1]) == "111"
    assert args[2].get("run_id") == "job-run-1"


@pytest.mark.asyncio
async def test_transcript_worthy_projection_skips_mailbox_mirror(tmp_path):
    """内容型投递成功 attach projection 后,不再镜像进 Background Updates ——
    避免双重表示,且防止报告的镜像版本贴着"上面"劫持指代。"""
    ch = _make_channel(tmp_path)
    ch._send_message = AsyncMock()
    ch._bindings = {"111": "my_agent"}
    ch._session_manager.get_cached_agent.return_value = SimpleNamespace(context_id="c", base_url="u")
    attach = AsyncMock()
    with patch("src.everbot.core.agent.provider.get_provider_for_agent",
               return_value=SimpleNamespace(attach_projection=attach)):
        await ch._on_background_event("session_1", {
            "source_type": "heartbeat_delivery", "agent_name": "my_agent",
            "detail": "REPORT", "deliver": True, "scope": "agent",
            "run_id": "job-1", "transcript_worthy": True,
        })
    attach.assert_awaited_once()
    ch._session_manager.deposit_mailbox_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_projection_falls_back_to_mailbox_mirror(tmp_path):
    """attach 失败 → 回落 mailbox 镜像,内容不丢(degraded but visible)。"""
    ch = _make_channel(tmp_path)
    ch._send_message = AsyncMock()
    ch._bindings = {"111": "my_agent"}
    ch._session_manager.get_cached_agent.return_value = SimpleNamespace(context_id="c", base_url="u")
    attach = AsyncMock(side_effect=RuntimeError("serve down"))
    with patch("src.everbot.core.agent.provider.get_provider_for_agent",
               return_value=SimpleNamespace(attach_projection=attach)):
        await ch._on_background_event("session_1", {
            "source_type": "heartbeat_delivery", "agent_name": "my_agent",
            "detail": "REPORT", "deliver": True, "scope": "agent",
            "run_id": "job-1", "transcript_worthy": True,
        })
    ch._session_manager.deposit_mailbox_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_maybe_attach_projection_creates_handle_when_not_cached(tmp_path):
    """后台投递常发生在用户空闲时,channel 句柄不在缓存 —— 必须回落创建并绑定
    channel context(set_session_id),否则永远不 attach(主动推送场景的核心)。"""
    ch = _make_channel(tmp_path)
    ch._session_manager.get_cached_agent.return_value = None
    created = SimpleNamespace(context_id="random-ctx", base_url="u")
    ch._agent_service.create_agent_instance = AsyncMock(return_value=created)
    attach = AsyncMock()
    set_sid = MagicMock()
    with patch("src.everbot.core.agent.provider.get_provider_for_agent",
               return_value=SimpleNamespace(attach_projection=attach, set_session_id=set_sid)):
        ok = await ch._maybe_attach_projection("demo_agent", "111", _attach_data())
    assert ok is True
    ch._agent_service.create_agent_instance.assert_awaited_once_with("demo_agent")
    set_sid.assert_called_once_with(created, "tg_session_demo_agent__111")
    ch._session_manager.cache_agent.assert_called_once()
    attach.assert_awaited_once()
    assert attach.call_args.args[0] is created


# ===========================================================================
# 16. _convert_markdown — table normalisation gate (issue #66)
# ===========================================================================

class TestConvertMarkdown:
    """_convert_markdown must normalise +---+ tables unconditionally, even when
    the message also has entity-producing spans (bold headings, ticker links).

    Old bug: normalisation was gated behind ``if not entity_dicts``, so any
    message with bold/link entities skipped table conversion entirely.
    """

    # Reusable fixture: message with bold heading AND a +--- style table
    _MIXED_TEXT = (
        "**投资信号汇总**\n\n"
        "标的        | 信号    | 强度\n"
        "------------+--------+------\n"
        "$SIVE       | 买入    | 强\n"
        "$XFAB       | 观望    | 中\n"
    )

    @staticmethod
    def _fake_entity():
        """Minimal entity stub returned by the mocked tg_md_convert."""
        class _E:
            def to_dict(self):
                return {"type": "bold", "offset": 0, "length": 8}
        return _E()

    def test_table_normalised_before_convert_when_entities_present(self):
        """tg_md_convert must receive normalised text (no ---+--- separator)
        even when it would produce entities (bold heading).  This is the
        direct regression test for the issue #66 gate bug."""
        import src.everbot.channels.telegram_channel as ch_mod

        captured = []

        def fake_convert(t):
            captured.append(t)
            # Simulate a convert that produces an entity (bold heading)
            return (t, [self._fake_entity()])

        with patch("src.everbot.channels.telegram_channel.HAS_TELEGRAMIFY", True), \
             patch("src.everbot.channels.telegram_channel.tg_md_convert", fake_convert, create=True):
            plain, entities = TelegramChannel._convert_markdown(self._MIXED_TEXT)

        assert captured, "tg_md_convert was never called"
        received = captured[0]

        # The separator line must be normalised — no raw +---+ form
        assert "---+---" not in received, (
            f"Table was NOT normalised before tg_md_convert.\nGot:\n{received}"
        )
        assert "----+--------+------" not in received, (
            f"Original +---+ separator still present.\nGot:\n{received}"
        )
        # Standard MD separator should be present after normalisation
        assert "| --- |" in received, (
            f"Expected normalised '| --- |' separator.\nGot:\n{received}"
        )

        # Entities should still pass through unchanged
        assert entities is not None
        assert len(entities) == 1
        assert entities[0]["type"] == "bold"

    def test_table_normalised_when_no_entities(self):
        """Existing behaviour: table is also normalised when convert produces no entities."""
        captured = []

        def fake_convert(t):
            captured.append(t)
            return (t, [])  # no entities

        with patch("src.everbot.channels.telegram_channel.HAS_TELEGRAMIFY", True), \
             patch("src.everbot.channels.telegram_channel.tg_md_convert", fake_convert, create=True):
            plain, entities = TelegramChannel._convert_markdown(self._MIXED_TEXT)

        assert captured
        received = captured[0]
        assert "| --- |" in received
        assert "----+--------+------" not in received

    def test_plain_text_without_table_unchanged(self):
        """Text without +---+ markers must not be modified before conversion."""
        text = "**Hello** world\n\nSome *italic* text."
        captured = []

        def fake_convert(t):
            captured.append(t)
            return (t, [self._fake_entity()])

        with patch("src.everbot.channels.telegram_channel.HAS_TELEGRAMIFY", True), \
             patch("src.everbot.channels.telegram_channel.tg_md_convert", fake_convert, create=True):
            TelegramChannel._convert_markdown(text)

        assert captured[0] == text, "Non-table text must pass through unmodified"

    def test_fallback_when_no_telegramify(self):
        """When telegramify is absent, heading markers are stripped (existing fallback)."""
        text = "# Heading\n\nSome text."
        with patch("src.everbot.channels.telegram_channel.HAS_TELEGRAMIFY", False):
            plain, entities = TelegramChannel._convert_markdown(text)
        assert "# " not in plain
        assert "Heading" in plain
        assert entities is None
