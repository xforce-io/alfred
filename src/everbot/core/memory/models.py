"""Memory entry data model."""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional
import uuid


@dataclass
class MemoryEntry:
    """A single structured memory entry.

    Two kinds share this type:
      * ``profile`` — long-lived user portrait (preference / fact / workflow / ...)
      * ``event`` — time-anchored occurrence (decision / todo / incident / ...)

    ``kind`` is set by the loading store from the file path, never parsed
    from the markdown header. ``event_at`` is meaningful only for events.
    """

    id: str
    content: str
    category: str
    score: float
    created_at: str
    last_activated: str
    activation_count: int
    source_session: str
    kind: str = "profile"
    event_at: Optional[str] = None
    due_at: Optional[str] = None
    status: str = "active"
    supersedes: list[str] = field(default_factory=list)
    superseded_by: list[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to JSON-serializable dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MemoryEntry":
        """Build from persisted dictionary data."""
        event_at_raw = data.get("event_at")
        due_at_raw = data.get("due_at")
        return cls(
            id=str(data.get("id") or new_id()),
            content=str(data.get("content", "")),
            category=str(data.get("category", "fact")),
            score=float(data.get("score", 0.5)),
            created_at=str(data.get("created_at") or datetime.now(timezone.utc).isoformat()),
            last_activated=str(data.get("last_activated") or datetime.now(timezone.utc).isoformat()),
            activation_count=int(data.get("activation_count", 0)),
            source_session=str(data.get("source_session", "")),
            kind=str(data.get("kind", "profile")),
            event_at=str(event_at_raw) if event_at_raw else None,
            due_at=str(due_at_raw) if due_at_raw else None,
            status=(
                str(data.get("status", "active"))
                if str(data.get("status", "active")) in {"active", "superseded"}
                else "active"
            ),
            supersedes=_relation_list(data.get("supersedes", [])),
            superseded_by=_relation_list(data.get("superseded_by", [])),
        )


def new_id() -> str:
    """Generate a short uuid4 ID (6 chars)."""
    return uuid.uuid4().hex[:6]


def _relation_list(raw: Any) -> list[str]:
    if isinstance(raw, str):
        return [value.strip() for value in raw.split(",") if value.strip()]
    if isinstance(raw, (list, tuple, set)):
        return [str(value) for value in raw if value]
    return []
