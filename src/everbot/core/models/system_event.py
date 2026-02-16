"""System event model for cross-session mailbox delivery."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import uuid


@dataclass
class SystemEvent:
    """Structured event stored in primary-session mailbox."""

    schema: str
    schema_version: int
    event_id: str
    event_type: str
    source_session_id: str
    timestamp: str
    summary: str
    detail: Optional[str] = None
    artifacts: List[str] = None
    priority: int = 0
    suppress_if_stale: bool = False
    dedupe_key: Optional[str] = None

    def __post_init__(self) -> None:
        if self.artifacts is None:
            self.artifacts = []

    def to_dict(self) -> Dict[str, Any]:
        """Convert to JSON-serializable dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SystemEvent":
        """Build a SystemEvent from persisted dictionary data."""
        return cls(
            schema=str(data.get("schema") or "everbot.system_event"),
            schema_version=int(data.get("schema_version") or 1),
            event_id=str(data.get("event_id") or f"evt_{uuid.uuid4().hex[:12]}"),
            event_type=str(data.get("event_type") or "system_update"),
            source_session_id=str(data.get("source_session_id") or ""),
            timestamp=str(data.get("timestamp") or datetime.now(timezone.utc).isoformat()),
            summary=str(data.get("summary") or ""),
            detail=data.get("detail"),
            artifacts=list(data.get("artifacts") or []),
            priority=int(data.get("priority") or 0),
            suppress_if_stale=bool(data.get("suppress_if_stale", False)),
            dedupe_key=data.get("dedupe_key"),
        )


def build_system_event(
    *,
    event_type: str,
    source_session_id: str,
    summary: str,
    detail: Optional[str] = None,
    artifacts: Optional[List[str]] = None,
    priority: int = 0,
    suppress_if_stale: bool = False,
    dedupe_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a serialized SystemEvent dictionary."""
    event = SystemEvent(
        schema="everbot.system_event",
        schema_version=1,
        event_id=f"evt_{uuid.uuid4().hex[:12]}",
        event_type=event_type,
        source_session_id=source_session_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        summary=summary,
        detail=detail,
        artifacts=list(artifacts or []),
        priority=priority,
        suppress_if_stale=suppress_if_stale,
        dedupe_key=dedupe_key,
    )
    return event.to_dict()
