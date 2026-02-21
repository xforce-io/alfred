"""Memory entry data model."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional
import uuid


@dataclass
class MemoryEntry:
    """A single structured memory entry."""

    id: str
    content: str
    category: str
    score: float
    created_at: str
    last_activated: str
    activation_count: int
    source_session: str

    def to_dict(self) -> Dict[str, Any]:
        """Convert to JSON-serializable dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MemoryEntry":
        """Build from persisted dictionary data."""
        return cls(
            id=str(data.get("id") or new_id()),
            content=str(data.get("content", "")),
            category=str(data.get("category", "fact")),
            score=float(data.get("score", 0.5)),
            created_at=str(data.get("created_at") or datetime.now(timezone.utc).isoformat()),
            last_activated=str(data.get("last_activated") or datetime.now(timezone.utc).isoformat()),
            activation_count=int(data.get("activation_count", 0)),
            source_session=str(data.get("source_session", "")),
        )


def new_id() -> str:
    """Generate a short uuid4 ID (6 chars)."""
    return uuid.uuid4().hex[:6]
