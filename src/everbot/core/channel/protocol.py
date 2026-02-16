"""Channel protocol definition."""

from __future__ import annotations

from typing import Protocol

from .models import ChannelCapabilities, OutboundMessage


class Channel(Protocol):
    """前端渠道协议。每种渠道（Web、Telegram、Discord）实现此接口。"""

    @property
    def channel_type(self) -> str:
        """渠道类型标识，如 "web"、"telegram"、"discord"。"""
        ...

    @property
    def capabilities(self) -> ChannelCapabilities:
        """声明此渠道的能力。"""
        ...

    async def start(self) -> None:
        """启动渠道（建立连接、开始轮询等）。"""
        ...

    async def stop(self) -> None:
        """停止渠道并清理资源。"""
        ...

    async def send(self, message: OutboundMessage) -> None:
        """向指定 channel_session 发送一条消息。"""
        ...

    async def broadcast_to_agent(self, agent_name: str, message: OutboundMessage) -> None:
        """向某 agent 的所有活跃 channel session 广播消息。"""
        ...

    def is_connected(self, channel_session_id: str) -> bool:
        """检查指定 channel session 是否仍然活跃。"""
        ...
