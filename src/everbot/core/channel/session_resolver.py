"""Channel session ID resolver."""

from __future__ import annotations


class ChannelSessionResolver:
    """Channel session ID <-> EverBot session ID mapping.

    Non-web channels use ``__`` (double underscore) as the separator between
    agent_name and channel_session_id so that agent names containing single
    underscores (e.g. ``daily_insight``) can be parsed unambiguously.

    Format: ``{prefix}{agent_name}__{channel_session_id}``
    Example: ``tg_session_daily_insight__12345``
    """

    # Separator between agent_name and channel_session_id in non-web session IDs.
    _SEP = "__"

    _PREFIX_MAP = {
        "web": "web_session_",
        "telegram": "tg_session_",
        "discord": "discord_session_",
    }

    @classmethod
    def resolve(cls, channel_type: str, agent_name: str, channel_session_id: str) -> str:
        """Map a channel-side session identifier to an EverBot session_id.

        Args:
            channel_type: Channel type (``"web"``, ``"telegram"``, …).
            agent_name: Target agent name.
            channel_session_id: Channel-internal session identifier.
                - web: optional; empty means primary session id.
                - telegram: Telegram chat_id.
                - discord: Discord channel_id.

        Returns:
            EverBot session_id, e.g. ``"tg_session_daily_insight__12345"``.
        """
        prefix = cls._PREFIX_MAP.get(channel_type, f"{channel_type}_session_")
        if channel_type == "web":
            if not channel_session_id:
                return f"{prefix}{agent_name}"
            return channel_session_id  # already a full session_id
        return f"{prefix}{agent_name}{cls._SEP}{channel_session_id}"

    @classmethod
    def extract_channel_type(cls, session_id: str) -> str:
        """Infer channel_type from session_id prefix.

        Returns:
            Channel type string; falls back to ``"web"`` for unknown prefixes.
        """
        for channel_type, prefix in cls._PREFIX_MAP.items():
            if session_id.startswith(prefix):
                return channel_type
        return "web"

    @classmethod
    def extract_agent_name(cls, session_id: str) -> str:
        """Extract agent_name from a non-web session_id.

        Example::

            "tg_session_daily_insight__12345" -> "daily_insight"
        """
        for _channel_type, prefix in cls._PREFIX_MAP.items():
            if _channel_type == "web":
                continue
            if session_id.startswith(prefix):
                remainder = session_id[len(prefix):]
                idx = remainder.find(cls._SEP)
                if idx > 0:
                    return remainder[:idx]
                return remainder  # no separator found, whole remainder is agent_name
        return ""

    @classmethod
    def extract_channel_session_id(cls, session_id: str) -> str:
        """Extract the channel-side session identifier from a non-web session_id.

        Example::

            "tg_session_daily_insight__12345" -> "12345"
        """
        for _channel_type, prefix in cls._PREFIX_MAP.items():
            if _channel_type == "web":
                continue
            if session_id.startswith(prefix):
                remainder = session_id[len(prefix):]
                idx = remainder.find(cls._SEP)
                if idx > 0:
                    return remainder[idx + len(cls._SEP):]
                return ""
        return ""
