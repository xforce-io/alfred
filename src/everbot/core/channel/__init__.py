"""Channel abstraction layer for multi-frontend support."""

from .core_service import ChannelCoreService
from .models import ChannelCapabilities, InboundMessage, OutboundMessage
from .protocol import Channel
from .session_resolver import ChannelSessionResolver

__all__ = [
    "Channel",
    "ChannelCapabilities",
    "ChannelCoreService",
    "ChannelSessionResolver",
    "InboundMessage",
    "OutboundMessage",
]
