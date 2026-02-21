"""Pure-logic memory merger — scoring, decay, and merge operations."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from .models import MemoryEntry, new_id

# Importance → initial score mapping
_IMPORTANCE_SCORES = {
    "high": 0.8,
    "medium": 0.6,
    "low": 0.4,
}

# Decay parameters
_PROTECTION_DAYS = 7
_DECAY_RATE = 0.99


@dataclass
class MergeResult:
    """Result of a merge operation."""

    entries: List[MemoryEntry]
    new_count: int = 0
    updated_count: int = 0


class MemoryMerger:
    """Stateless memory merge logic — no I/O."""

    def create_entry(
        self,
        content: str,
        category: str,
        importance: str = "medium",
        source_session: str = "",
    ) -> MemoryEntry:
        """Create a new MemoryEntry with initial score based on importance."""
        now = datetime.now(timezone.utc).isoformat()
        score = _IMPORTANCE_SCORES.get(importance, 0.6)
        return MemoryEntry(
            id=new_id(),
            content=content,
            category=category,
            score=score,
            created_at=now,
            last_activated=now,
            activation_count=1,
            source_session=source_session,
        )

    def reinforce(self, entry: MemoryEntry) -> MemoryEntry:
        """Reinforce an existing entry: boost score with diminishing returns."""
        entry.score = entry.score + (1.0 - entry.score) * 0.2
        entry.activation_count += 1
        entry.last_activated = datetime.now(timezone.utc).isoformat()
        return entry

    def apply_decay(
        self, entries: List[MemoryEntry], now: Optional[datetime] = None
    ) -> List[MemoryEntry]:
        """Apply time-based decay to all entries. 7-day protection period."""
        if now is None:
            now = datetime.now(timezone.utc)

        for entry in entries:
            try:
                last = datetime.fromisoformat(entry.last_activated)
                # Make timezone-aware if needed
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                days = (now - last).total_seconds() / 86400.0
                if days > _PROTECTION_DAYS:
                    decay_days = days - _PROTECTION_DAYS
                    entry.score = entry.score * (_DECAY_RATE ** decay_days)
            except (ValueError, TypeError):
                pass  # Skip entries with unparseable dates

        return entries

    def merge(
        self,
        existing: List[MemoryEntry],
        new_extractions: List[dict],
        reinforcements: List[str],
        source_session: str = "",
    ) -> MergeResult:
        """Merge new extractions and reinforcements into existing entries.

        Args:
            existing: Current memory entries (already decayed).
            new_extractions: List of dicts with content/category/importance.
            reinforcements: List of existing entry IDs to reinforce.
            source_session: Session ID for provenance.

        Returns:
            MergeResult with merged entries and stats.
        """
        entry_map: Dict[str, MemoryEntry] = {e.id: e for e in existing}
        updated_count = 0

        # Apply reinforcements
        for rid in reinforcements:
            if rid in entry_map:
                self.reinforce(entry_map[rid])
                updated_count += 1

        # Create new entries
        new_count = 0
        for ext in new_extractions:
            entry = self.create_entry(
                content=ext.get("content", ""),
                category=ext.get("category", "fact"),
                importance=ext.get("importance", "medium"),
                source_session=source_session,
            )
            entry_map[entry.id] = entry
            new_count += 1

        return MergeResult(
            entries=list(entry_map.values()),
            new_count=new_count,
            updated_count=updated_count,
        )
