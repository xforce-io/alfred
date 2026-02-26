"""Pure-logic memory merger — scoring, decay, and merge operations."""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Set, Tuple

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

# Dedup parameters
_SIMILARITY_THRESHOLD = 0.65


def _tokenize(text: str) -> Set[str]:
    """Tokenize text for similarity comparison.

    Chinese characters are treated as individual tokens;
    Latin words are split on whitespace/punctuation.
    """
    tokens: Set[str] = set()
    buf: list[str] = []
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            # Flush any buffered Latin word
            if buf:
                tokens.add("".join(buf).lower())
                buf.clear()
            tokens.add(ch)
        elif ch.isalnum() or ch == "_":
            buf.append(ch)
        else:
            if buf:
                tokens.add("".join(buf).lower())
                buf.clear()
    if buf:
        tokens.add("".join(buf).lower())
    return tokens


def token_similarity(a: str, b: str) -> float:
    """Jaccard similarity between two strings based on token overlap."""
    ta, tb = _tokenize(a), _tokenize(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


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

        # Create new entries (with content-level dedup)
        new_count = 0
        for ext in new_extractions:
            content = ext.get("content", "")
            category = ext.get("category", "fact")

            # Check against all existing entries for near-duplicates
            dup_entry = self._find_duplicate(content, category, entry_map.values())
            if dup_entry is not None:
                self.reinforce(dup_entry)
                updated_count += 1
                continue

            entry = self.create_entry(
                content=content,
                category=category,
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

    @staticmethod
    def _find_duplicate(
        content: str,
        category: str,
        candidates: "Iterable[MemoryEntry]",
    ) -> Optional[MemoryEntry]:
        """Find the best matching duplicate among candidates.

        Returns the highest-similarity entry if above threshold, else None.
        Only matches within the same category.
        """
        best_entry: Optional[MemoryEntry] = None
        best_sim = 0.0
        for entry in candidates:
            if entry.category != category:
                continue
            sim = token_similarity(content, entry.content)
            if sim >= _SIMILARITY_THRESHOLD and sim > best_sim:
                best_sim = sim
                best_entry = entry
        return best_entry
