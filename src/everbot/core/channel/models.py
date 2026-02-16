"""Channel message models and capability declarations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class InboundMessage:
    """从前端渠道接收到的用户消息。"""

    channel_type: str  # "web", "telegram", "discord"
    channel_session_id: str  # Channel 内部的会话标识（如 Telegram chat_id）
    agent_name: str  # 目标 agent
    text: str  # 用户输入文本
    user_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OutboundMessage:
    """向前端渠道发送的消息。"""

    channel_session_id: str  # 目标 Channel session
    content: str  # 文本内容
    msg_type: str = "text"  # "text" | "delta" | "status" | "error" | "end"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ChannelCapabilities:
    """声明 Channel 的能力，供 ChannelManager 决定投递策略。"""

    streaming: bool = False  # 是否支持流式 delta 推送
    text_chunk_limit: int = 0  # 单条消息最大字符数，0 表示无限制
