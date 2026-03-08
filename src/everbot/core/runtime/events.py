"""Global event emitter and routing utilities for EverBot."""

from dataclasses import dataclass
from typing import Callable, Any, Dict, List, Optional
import asyncio
import logging
import uuid
from datetime import datetime, timezone

from ..channel.session_resolver import ChannelSessionResolver

logger = logging.getLogger(__name__)
EVENT_ENVELOPE_SCHEMA = "everbot.event"
EVENT_ENVELOPE_SCHEMA_VERSION = 1
VALID_SCOPES = frozenset({"session", "agent"})
VALID_CHANNELS = frozenset(ChannelSessionResolver.list_supported_channels())

# List of async callbacks: (session_id, data) -> None
_subscribers: List[Callable[[str, Dict[str, Any]], Any]] = []


@dataclass(frozen=True)
class RoutingDecision:
    """Normalized routing decision derived from one event envelope."""

    deliver: bool
    scope: str
    agent_name: Optional[str]
    target_session_id: Optional[str]
    target_channel: Optional[str]
    reason: Optional[str] = None

def subscribe(callback: Callable[[str, Dict[str, Any]], Any]):
    """Subscribe to global events."""
    if callback not in _subscribers:
        _subscribers.append(callback)

def unsubscribe(callback: Callable[[str, Dict[str, Any]], Any]):
    """Unsubscribe from global events."""
    if callback in _subscribers:
        _subscribers.remove(callback)


def _resolve_target_channel(target_session_id: Optional[str]) -> Optional[str]:
    """Infer channel type from the target session ID."""
    if not target_session_id:
        return None
    channel = ChannelSessionResolver.extract_channel_type(target_session_id)
    return channel or None


def resolve_routing(envelope: Dict[str, Any]) -> RoutingDecision:
    """Resolve normalized routing from an event envelope."""
    if envelope.get("deliver") is False:
        return RoutingDecision(
            deliver=False,
            scope="session",
            agent_name=envelope.get("agent_name"),
            target_session_id=None,
            target_channel=None,
            reason="suppressed_by_deliver_false",
        )

    scope = str(envelope.get("scope") or "session").strip().lower()
    if scope not in VALID_SCOPES:
        logger.warning("Unknown event scope=%r for event %s", scope, envelope.get("event_id"))
        return RoutingDecision(
            deliver=False,
            scope="session",
            agent_name=envelope.get("agent_name"),
            target_session_id=None,
            target_channel=None,
            reason="invalid_scope",
        )

    agent_name = envelope.get("agent_name")
    target_session_id = envelope.get("target_session_id")
    target_channel = envelope.get("target_channel")

    if target_channel is not None and target_channel not in VALID_CHANNELS:
        logger.warning(
            "Unknown target_channel=%r for event %s, ignoring filter",
            target_channel,
            envelope.get("event_id"),
        )
        target_channel = None

    if scope == "agent":
        return RoutingDecision(
            deliver=True,
            scope=scope,
            agent_name=agent_name,
            target_session_id=None,
            target_channel=target_channel,
            reason=None,
        )

    if target_session_id is None:
        logger.warning("Missing target_session_id for session-scoped event %s", envelope.get("event_id"))
        return RoutingDecision(
            deliver=False,
            scope=scope,
            agent_name=agent_name,
            target_session_id=None,
            target_channel=target_channel,
            reason="missing_target_session_id",
        )

    inferred_channel = _resolve_target_channel(target_session_id)
    if target_channel is not None and inferred_channel is not None and target_channel != inferred_channel:
        logger.warning(
            "Event %s target_channel mismatch: target_channel=%r inferred=%r target_session_id=%r",
            envelope.get("event_id"),
            target_channel,
            inferred_channel,
            target_session_id,
        )
        return RoutingDecision(
            deliver=False,
            scope=scope,
            agent_name=agent_name,
            target_session_id=target_session_id,
            target_channel=target_channel,
            reason="target_channel_mismatch",
        )

    return RoutingDecision(
        deliver=True,
        scope=scope,
        agent_name=agent_name,
        target_session_id=target_session_id,
        target_channel=target_channel,
        reason=None,
    )

async def emit(
    source_session_id: str,
    data: Dict[str, Any],
    *,
    agent_name: Optional[str] = None,
    scope: str = "session",
    target_session_id: Optional[str] = None,
    target_channel: Optional[str] = None,
    source_type: Optional[str] = None,
    run_id: Optional[str] = None,
):
    """Emit an event to all subscribers.

    The function enriches *data* with envelope fields before dispatching.
    """
    # Shallow-copy to avoid mutating the caller's dict
    envelope = dict(data)
    envelope["event_id"] = envelope.get("event_id") or f"evt_{uuid.uuid4().hex[:12]}"
    envelope["timestamp"] = envelope.get("timestamp") or datetime.now(timezone.utc).isoformat()
    envelope["schema"] = envelope.get("schema") or EVENT_ENVELOPE_SCHEMA
    envelope["schema_version"] = int(envelope.get("schema_version") or EVENT_ENVELOPE_SCHEMA_VERSION)
    envelope["source_session_id"] = source_session_id
    envelope["scope"] = scope
    deliver_value = envelope.get("deliver", True)
    envelope["deliver"] = True if deliver_value is None else bool(deliver_value)
    if agent_name is not None:
        envelope["agent_name"] = agent_name
    if source_type is not None:
        envelope["source_type"] = source_type
    if run_id is not None:
        envelope["run_id"] = run_id
    if target_session_id is not None:
        envelope["target_session_id"] = target_session_id
    if target_channel is not None:
        envelope["target_channel"] = target_channel

    if not _subscribers:
        return

    tasks = []
    for callback in _subscribers:
        try:
            res = callback(source_session_id, envelope)
            if asyncio.iscoroutine(res):
                tasks.append(res)
        except Exception as e:
            logger.error("Error in event subscriber: %s", e)

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
