"""Pure-logic memory merger — scoring, decay, and merge operations.

Decay strategies differ by memory kind:

* **profile**: 7-day protection window, then 1% daily geometric decay
  (``score *= 0.99^(days - 7)``). Anchored on ``last_activated``.
* **event**: 30-day half-life from the event's natural anchor —
  ``due_at`` for unfinished todos, otherwise ``event_at``. No protection
  period because most decay-relevant events have months of useful life.

The two strategies live as separate methods (``apply_profile_decay`` /
``apply_event_decay``) because their inputs and curves are different
enough that branching inside a single function would be confusing.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional, Set

from .models import MemoryEntry, new_id

# Importance → initial score mapping
_IMPORTANCE_SCORES = {
    "high": 0.8,
    "medium": 0.6,
    "low": 0.4,
}

# Profile decay parameters
_PROFILE_PROTECTION_DAYS = 7
_PROFILE_DECAY_RATE = 0.99

# Event decay parameters
_EVENT_HALF_LIFE_DAYS = 30.0

# Dedup parameters
_SIMILARITY_THRESHOLD = 0.35


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

    def apply_profile_decay(
        self, entries: List[MemoryEntry], now: Optional[datetime] = None
    ) -> List[MemoryEntry]:
        """Apply profile decay — ``score *= 0.99^(days_since_activated - 7)``.

        Anchored on ``last_activated``. Entries within the 7-day protection
        window are unchanged. Entries with unparseable timestamps are
        silently skipped (their score is left as-is).
        """
        if now is None:
            now = datetime.now(timezone.utc)

        for entry in entries:
            try:
                last = datetime.fromisoformat(entry.last_activated)
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                days = (now - last).total_seconds() / 86400.0
                if days > _PROFILE_PROTECTION_DAYS:
                    decay_days = days - _PROFILE_PROTECTION_DAYS
                    entry.score = entry.score * (_PROFILE_DECAY_RATE ** decay_days)
            except (ValueError, TypeError):
                pass

        return entries

    def apply_event_decay(
        self, entries: List[MemoryEntry], now: Optional[datetime] = None
    ) -> List[MemoryEntry]:
        """Apply event decay — 30-day half-life from each entry's anchor.

        Anchor selection:
          * todo with parseable ``due_at`` → ``due_at`` (decay starts only
            after the deadline; before the deadline the entry stays at
            full score, modeling "still pending, still relevant")
          * everything else → ``event_at``

        Profile entries (``kind != "event"``) are left untouched so this
        method can be called on a mixed list without filtering first.
        Entries whose anchor cannot be parsed are skipped silently.
        """
        if now is None:
            now = datetime.now(timezone.utc)

        for entry in entries:
            if entry.kind != "event":
                continue
            anchor = self._event_decay_anchor(entry)
            if anchor is None:
                continue
            days_past_anchor = (now - anchor).total_seconds() / 86400.0
            if days_past_anchor <= 0:
                continue  # protected: anchor is in the future
            entry.score = entry.score * (0.5 ** (days_past_anchor / _EVENT_HALF_LIFE_DAYS))

        return entries

    @staticmethod
    def _event_decay_anchor(entry: MemoryEntry) -> Optional[datetime]:
        """Pick the timestamp that decay measures from."""
        candidates: List[Optional[str]] = []
        if entry.category == "todo" and entry.due_at:
            candidates.append(entry.due_at)
        candidates.append(entry.event_at)
        for raw in candidates:
            if not raw:
                continue
            try:
                dt = datetime.fromisoformat(raw)
            except ValueError:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        return None

    def merge(
        self,
        existing: List[MemoryEntry],
        new_extractions: List[dict],
        reinforcements: List[str],
        source_session: str = "",
        content_filter: Optional[Callable[[str], bool]] = None,
    ) -> MergeResult:
        """Merge new extractions and reinforcements into existing entries.

        Args:
            existing: Current memory entries (already decayed).
            new_extractions: List of dicts with content/category/importance.
            reinforcements: List of existing entry IDs to reinforce.
            source_session: Session ID for provenance.
            content_filter: Optional predicate — if it returns *True* for an
                entry's content the entry is considered "internal" and will be
                blocked (new), skipped (reinforce) or suppressed (existing).

        Returns:
            MergeResult with merged entries and stats.
        """
        entry_map: Dict[str, MemoryEntry] = {e.id: e for e in existing}
        updated_count = 0

        # Apply reinforcements (skip entries that match the content filter)
        for rid in reinforcements:
            if rid in entry_map:
                if content_filter and content_filter(entry_map[rid].content):
                    continue
                self.reinforce(entry_map[rid])
                updated_count += 1

        # Create new entries (with content-level dedup)
        new_count = 0
        for ext in new_extractions:
            content = ext.get("content", "")
            category = ext.get("category", "fact")

            # Block new entries that match the content filter
            if content_filter and content_filter(content):
                continue

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

        # Suppress existing entries that match the content filter (accelerate decay)
        if content_filter:
            for entry in entry_map.values():
                if content_filter(entry.content):
                    entry.score *= 0.5

        return MergeResult(
            entries=list(entry_map.values()),
            new_count=new_count,
            updated_count=updated_count,
        )

    def merge_entries(
        self,
        entry_a: MemoryEntry,
        entry_b: MemoryEntry,
        merged_content: str,
    ) -> MemoryEntry:
        """Merge two entries into one new entry.

        The new entry gets:
        - score = max(a, b)
        - activation_count = sum(a, b)
        - category from the higher-scored entry
        - new ID
        """
        now = datetime.now(timezone.utc).isoformat()
        return MemoryEntry(
            id=new_id(),
            content=merged_content,
            category=entry_a.category if entry_a.score >= entry_b.score else entry_b.category,
            score=max(entry_a.score, entry_b.score),
            created_at=min(entry_a.created_at, entry_b.created_at),
            last_activated=now,
            activation_count=entry_a.activation_count + entry_b.activation_count,
            source_session=entry_a.source_session or entry_b.source_session,
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
