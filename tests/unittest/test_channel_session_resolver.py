"""Tests for ChannelSessionResolver."""

from src.everbot.core.channel.session_resolver import ChannelSessionResolver


class TestResolve:
    def test_web_empty_id(self):
        """Web with empty channel_session_id returns primary session id."""
        sid = ChannelSessionResolver.resolve("web", "my_agent", "")
        assert sid == "web_session_my_agent"

    def test_web_full_id(self):
        """Web with existing full session_id passes through."""
        full_id = "web_session_my_agent__1234_abc"
        sid = ChannelSessionResolver.resolve("web", "my_agent", full_id)
        assert sid == full_id

    def test_telegram(self):
        sid = ChannelSessionResolver.resolve("telegram", "bot", "12345")
        assert sid == "tg_session_bot__12345"

    def test_telegram_agent_with_underscore(self):
        """Agent names containing underscores are handled correctly."""
        sid = ChannelSessionResolver.resolve("telegram", "daily_insight", "12345")
        assert sid == "tg_session_daily_insight__12345"

    def test_discord(self):
        sid = ChannelSessionResolver.resolve("discord", "helper", "ch999")
        assert sid == "discord_session_helper__ch999"

    def test_unknown_channel_type(self):
        """Unknown channel types use generic prefix pattern."""
        sid = ChannelSessionResolver.resolve("slack", "agent1", "C0001")
        assert sid == "slack_session_agent1__C0001"


class TestExtractChannelType:
    def test_web_prefix(self):
        assert ChannelSessionResolver.extract_channel_type("web_session_my_agent") == "web"

    def test_web_sub_session(self):
        assert ChannelSessionResolver.extract_channel_type("web_session_a__123_x") == "web"

    def test_telegram_prefix(self):
        assert ChannelSessionResolver.extract_channel_type("tg_session_bot__12345") == "telegram"

    def test_discord_prefix(self):
        assert ChannelSessionResolver.extract_channel_type("discord_session_h__ch1") == "discord"

    def test_unknown_prefix_fallback(self):
        """Unknown prefixes fall back to 'web' for backward compatibility."""
        assert ChannelSessionResolver.extract_channel_type("heartbeat_session_a") == "web"
        assert ChannelSessionResolver.extract_channel_type("job_123") == "web"
        assert ChannelSessionResolver.extract_channel_type("random_id") == "web"


class TestExtractAgentName:
    def test_telegram_simple(self):
        assert ChannelSessionResolver.extract_agent_name("tg_session_bot__12345") == "bot"

    def test_telegram_underscore_agent(self):
        assert ChannelSessionResolver.extract_agent_name("tg_session_daily_insight__12345") == "daily_insight"

    def test_discord(self):
        assert ChannelSessionResolver.extract_agent_name("discord_session_helper__ch999") == "helper"

    def test_web_returns_empty(self):
        """Web session IDs are not parsed by extract_agent_name."""
        assert ChannelSessionResolver.extract_agent_name("web_session_my_agent") == ""

    def test_unknown_prefix_returns_empty(self):
        assert ChannelSessionResolver.extract_agent_name("random_id") == ""

    def test_no_separator_returns_remainder(self):
        """If no __ separator, entire remainder is treated as agent_name."""
        assert ChannelSessionResolver.extract_agent_name("tg_session_bot") == "bot"


class TestExtractChannelSessionId:
    def test_telegram(self):
        assert ChannelSessionResolver.extract_channel_session_id("tg_session_bot__12345") == "12345"

    def test_telegram_underscore_agent(self):
        assert ChannelSessionResolver.extract_channel_session_id("tg_session_daily_insight__12345") == "12345"

    def test_discord(self):
        assert ChannelSessionResolver.extract_channel_session_id("discord_session_helper__ch999") == "ch999"

    def test_no_separator_returns_empty(self):
        assert ChannelSessionResolver.extract_channel_session_id("tg_session_bot") == ""

    def test_web_returns_empty(self):
        assert ChannelSessionResolver.extract_channel_session_id("web_session_my_agent") == ""
