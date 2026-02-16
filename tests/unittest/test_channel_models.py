"""Tests for channel message models and capabilities."""

from src.everbot.core.channel.models import (
    ChannelCapabilities,
    InboundMessage,
    OutboundMessage,
)


class TestInboundMessage:
    def test_required_fields(self):
        msg = InboundMessage(
            channel_type="telegram",
            channel_session_id="tg_session_bot_123",
            agent_name="my_agent",
            text="hello",
        )
        assert msg.channel_type == "telegram"
        assert msg.channel_session_id == "tg_session_bot_123"
        assert msg.agent_name == "my_agent"
        assert msg.text == "hello"

    def test_defaults(self):
        msg = InboundMessage(
            channel_type="web",
            channel_session_id="s1",
            agent_name="a",
            text="hi",
        )
        assert msg.user_id is None
        assert msg.metadata == {}

    def test_optional_fields(self):
        msg = InboundMessage(
            channel_type="web",
            channel_session_id="s1",
            agent_name="a",
            text="hi",
            user_id="u1",
            metadata={"reply_to": 42},
        )
        assert msg.user_id == "u1"
        assert msg.metadata == {"reply_to": 42}


class TestOutboundMessage:
    def test_required_fields(self):
        msg = OutboundMessage(channel_session_id="s1", content="world")
        assert msg.channel_session_id == "s1"
        assert msg.content == "world"

    def test_defaults(self):
        msg = OutboundMessage(channel_session_id="s1", content="x")
        assert msg.msg_type == "text"
        assert msg.metadata == {}

    def test_msg_types(self):
        for t in ("text", "delta", "status", "error", "end"):
            msg = OutboundMessage(channel_session_id="s1", content="c", msg_type=t)
            assert msg.msg_type == t


class TestChannelCapabilities:
    def test_defaults(self):
        cap = ChannelCapabilities()
        assert cap.streaming is False
        assert cap.text_chunk_limit == 0

    def test_custom_values(self):
        cap = ChannelCapabilities(streaming=True, text_chunk_limit=4096)
        assert cap.streaming is True
        assert cap.text_chunk_limit == 4096
