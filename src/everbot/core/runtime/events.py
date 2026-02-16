"""
Global Event Emitter for EverBot.
Allows background tasks (like heartbeats) to broadcast events to the UI
when they are in the same process.

Supports envelope metadata for agent-scoped routing.
"""

from typing import Callable, Any, Dict, List, Optional
import asyncio
import logging
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
EVENT_ENVELOPE_SCHEMA = "everbot.event"
EVENT_ENVELOPE_SCHEMA_VERSION = 1

# List of async callbacks: (session_id, data) -> None
_subscribers: List[Callable[[str, Dict[str, Any]], Any]] = []

def subscribe(callback: Callable[[str, Dict[str, Any]], Any]):
    """Subscribe to global events."""
    if callback not in _subscribers:
        _subscribers.append(callback)

def unsubscribe(callback: Callable[[str, Dict[str, Any]], Any]):
    """Unsubscribe from global events."""
    if callback in _subscribers:
        _subscribers.remove(callback)

async def emit(
    session_id: str,
    data: Dict[str, Any],
    *,
    agent_name: Optional[str] = None,
    scope: str = "session",
    source_type: Optional[str] = None,
    run_id: Optional[str] = None,
):
    """Emit an event to all subscribers.

    The function enriches *data* with envelope fields before dispatching.
    Backward compatible — existing callers without keyword args default to
    ``scope="session"``.
    """
    # Shallow-copy to avoid mutating the caller's dict
    envelope = dict(data)
    envelope["event_id"] = envelope.get("event_id") or f"evt_{uuid.uuid4().hex[:12]}"
    envelope["timestamp"] = envelope.get("timestamp") or datetime.now(timezone.utc).isoformat()
    envelope["schema"] = envelope.get("schema") or EVENT_ENVELOPE_SCHEMA
    envelope["schema_version"] = int(envelope.get("schema_version") or EVENT_ENVELOPE_SCHEMA_VERSION)
    envelope["session_id"] = session_id
    envelope["scope"] = scope
    deliver_value = envelope.get("deliver", True)
    envelope["deliver"] = True if deliver_value is None else bool(deliver_value)
    if agent_name is not None:
        envelope["agent_name"] = agent_name
    if source_type is not None:
        envelope["source_type"] = source_type
    if run_id is not None:
        envelope["run_id"] = run_id

    if not _subscribers:
        return

    tasks = []
    for callback in _subscribers:
        try:
            res = callback(session_id, envelope)
            if asyncio.iscoroutine(res):
                tasks.append(res)
        except Exception as e:
            logger.error(f"Error in event subscriber: {e}")

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
