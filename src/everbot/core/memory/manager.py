"""High-level memory manager — orchestrates profile and event memory.

The manager is the only public entry point for memory operations. It
hides the two-layer split (profile vs event) from callers and runs both
extraction pipelines on the same conversation slice. Failures in one
layer never block the other.
"""

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .event_extractor import EventExtractor
from .event_store import EventStore
from .merger import MemoryMerger
from .models import MemoryEntry
from .profile_extractor import ProfileExtractor
from .profile_store import ProfileStore

logger = logging.getLogger(__name__)

# System-managed files that should not appear in prompt-injected memories.
# Mentioning these files reinforces the LLM's tendency to read/write them
# directly, which conflicts with agent-level prohibitions.
# Note: Python 3 treats CJK chars as \w, so \b won't fire between "md" and
# a Chinese char.  We use a lookahead that accepts either a non-alnum ASCII
# char, a CJK char, or end-of-string as the right boundary.
_BOUNDARY = r"(?=[^a-zA-Z0-9_]|$)"
_INTERNAL_FILE_PATTERN = re.compile(
    rf"HEARTBEAT\.md{_BOUNDARY}"
    rf"|MEMORY\.md{_BOUNDARY}"
    rf"|AGENTS\.md{_BOUNDARY}"
    rf"|USER\.md{_BOUNDARY}",
    re.IGNORECASE,
)


_INTERNAL_CONTENT_PATTERN = re.compile(
    r"智能体长期记忆系统"
    r"|知识网络的核心功能"
    r"|记忆提取器"
    r"|记忆合并器"
    r"|heartbeat.*记忆"
    r"|memory.*merger"
    r"|memory.*extractor",
    re.IGNORECASE,
)


def _is_internal_content(content: str) -> bool:
    """Check if memory content references internal files or architecture."""
    if _INTERNAL_FILE_PATTERN.search(content):
        return True
    if _INTERNAL_CONTENT_PATTERN.search(content):
        return True
    return False


# Score thresholds for prompt injection — kept as module-level constants so
# they're easy to tune without hunting through method bodies.
_PROFILE_INJECT_THRESHOLD = 0.5
_EVENT_INJECT_THRESHOLD = 0.3
_EVENT_INJECT_WINDOW_DAYS = 30


class IntegrityError(Exception):
    """Raised when a memory review operation violates entropy constraints."""
    pass


class MemoryManager:
    """Unified entry point for both profile and event memory.

    Usage::

        mm = MemoryManager(memory_path, context=ctx)
        await mm.process_session_end(messages, session_id)

        # For prompt injection (no LLM context needed):
        mm = MemoryManager(memory_path)
        prompt = mm.get_prompt_memories()
    """

    def __init__(
        self,
        memory_path: Path,
        context: Any = None,
        events_dir: Optional[Path] = None,
    ):
        memory_path = Path(memory_path)
        self.store = ProfileStore(memory_path)
        self._event_store = EventStore(
            Path(events_dir) if events_dir else memory_path.parent / "events"
        )
        self.merger = MemoryMerger()
        self._context = context

    # =================================================================
    # Session-end pipeline
    # =================================================================

    async def process_session_end(
        self,
        messages: List[Dict[str, Any]],
        session_id: str,
    ) -> Dict[str, Any]:
        """Run profile + event extraction on the new-message slice.

        Returns a stats dict shaped as ``{"profile": {...}, "event": {...}}``.
        Either sub-dict may carry ``new_count == 0`` if extraction was
        skipped (no LLM context, no new messages, or extractor failure).
        """
        existing = self.store.load()
        empty_stats = {
            "profile": {"new_count": 0, "updated_count": 0, "total": len(existing)},
            "event": {"new_count": 0},
        }

        if self._context is None:
            logger.warning("No LLM context; skipping extraction")
            return empty_stats

        sliced = self._slice_new_messages(messages)
        if not sliced:
            logger.debug("No new messages since last extraction; skipping")
            return empty_stats

        new_messages, total_messages = sliced

        profile_stats = await self._process_profile(
            new_messages, existing, session_id, total_messages
        )
        event_stats = await self._process_events(new_messages, session_id)

        stats = {"profile": profile_stats, "event": event_stats}
        logger.info("Memory processing complete: %s", stats)
        return stats

    def _slice_new_messages(
        self, messages: List[Dict[str, Any]]
    ) -> Optional[Tuple[List[Dict[str, Any]], int]]:
        """Return (new_messages, total) or None if nothing to process.

        If ``last_processed_count`` is 0 or >= len(messages), the entire
        list is treated as new (a new session, not a continuation).
        """
        last_processed = self.store.last_processed_count
        if 0 < last_processed < len(messages):
            new_messages = messages[last_processed:]
        else:
            new_messages = messages
        if not new_messages:
            return None
        return new_messages, len(messages)

    async def _process_profile(
        self,
        new_messages: List[Dict[str, Any]],
        existing: List[MemoryEntry],
        session_id: str,
        total_messages: int,
    ) -> Dict[str, Any]:
        """Profile pipeline: extract → decay → merge → save."""
        extractor = ProfileExtractor(self._context)
        extract_result = await extractor.extract(new_messages, existing)

        existing = self.merger.apply_profile_decay(existing)

        merge_result = self.merger.merge(
            existing=existing,
            new_extractions=extract_result.new_memories,
            reinforcements=extract_result.reinforced_ids,
            source_session=session_id,
            content_filter=_is_internal_content,
        )

        # Save advances the watermark even when extraction produced
        # nothing new — otherwise the same messages would be re-extracted
        # on the next call.
        self.store.save(merge_result.entries, last_processed_count=total_messages)

        return {
            "new_count": merge_result.new_count,
            "updated_count": merge_result.updated_count,
            "total": len(merge_result.entries),
        }

    async def _process_events(
        self,
        new_messages: List[Dict[str, Any]],
        session_id: str,
    ) -> Dict[str, Any]:
        """Event pipeline: extract → append. Failures degrade gracefully."""
        extractor = EventExtractor(self._context)
        try:
            result = await extractor.extract(new_messages, session_id=session_id)
        except Exception:
            logger.warning("Event extraction failed", exc_info=True)
            return {"new_count": 0}

        if not result.new_events:
            return {"new_count": 0}

        try:
            written = self._event_store.append(result.new_events)
        except Exception:
            logger.warning("Event store append failed", exc_info=True)
            return {"new_count": 0}

        return {"new_count": written}

    # =================================================================
    # Prompt injection
    # =================================================================

    def get_prompt_memories(
        self,
        top_k: int = 20,
        event_top_k: int = 10,
        event_window_days: int = _EVENT_INJECT_WINDOW_DAYS,
    ) -> str:
        """Return formatted memory text for system prompt injection.

        The output is two optional sections joined by a blank line:

        * ``# 历史记忆`` — high-scoring profile entries (always-on)
        * ``# 近期事件`` — recent decayed events (within the time window)

        Either section may be omitted when empty; if both are empty the
        return value is ``""`` so the caller can no-op trivially.
        """
        sections = []
        profile_block = self._format_profile_block(top_k)
        if profile_block:
            sections.append(profile_block)
        event_block = self._format_event_block(event_top_k, event_window_days)
        if event_block:
            sections.append(event_block)
        return "\n\n".join(sections)

    def _format_profile_block(self, top_k: int) -> str:
        """High-scoring profile entries, deduplicated by token similarity."""
        from .merger import token_similarity, _SIMILARITY_THRESHOLD

        entries = self.store.load()
        if not entries:
            return ""

        candidates = sorted(
            [
                e for e in entries
                if e.score >= _PROFILE_INJECT_THRESHOLD
                and not _is_internal_content(e.content)
            ],
            key=lambda e: e.score,
            reverse=True,
        )
        if not candidates:
            return ""

        selected: List[MemoryEntry] = []
        for entry in candidates:
            if len(selected) >= top_k:
                break
            is_dup = any(
                token_similarity(entry.content, s.content) >= _SIMILARITY_THRESHOLD
                for s in selected
            )
            if not is_dup:
                selected.append(entry)

        if not selected:
            return ""

        lines = ["# 历史记忆", "", "关于用户的关键信息：", ""]
        for entry in selected:
            lines.append(f"- [{entry.category}] {entry.content}")
        return "\n".join(lines)

    def _format_event_block(self, top_k: int, days: int) -> str:
        """Recent events that survived decay, sorted by current score."""
        entries = self._event_store.load_recent(days=days)
        if not entries:
            return ""

        # Apply decay to the loaded copies (does not touch on-disk score —
        # event files are append-only and never rewritten).
        self.merger.apply_event_decay(entries)

        candidates = [
            e for e in entries
            if e.score >= _EVENT_INJECT_THRESHOLD
            and not _is_internal_content(e.content)
        ]
        if not candidates:
            return ""

        candidates.sort(key=lambda e: e.score, reverse=True)
        selected = candidates[:top_k]

        lines = ["# 近期事件", "", "最近的关键事件：", ""]
        for entry in selected:
            date_str = (entry.event_at or "")[:10]
            head = f"- [{date_str} {entry.category}]" if date_str else f"- [{entry.category}]"
            line = f"{head} {entry.content}"
            if entry.due_at:
                line += f" (due: {entry.due_at[:10]})"
            lines.append(line)
        return "\n".join(lines)

    # =================================================================
    # Recall (keyword search)
    # =================================================================

    def recall(
        self,
        query: str,
        kind: str = "both",
        top_k: int = 5,
        days: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Keyword search over memory using BM25-lite.

        Args:
            query: search keywords or short phrase.
            kind: ``"profile"`` / ``"event"`` / ``"both"``.
            top_k: maximum number of results to return.
            days: when searching events, restrict to the past N days.
                ``None`` means search all months. Has no effect on profile
                entries.

        Returns:
            List of dicts shaped as ``MemoryEntry.to_dict()`` plus a
            ``rank_score`` field carrying the BM25 score (rounded).
        """
        from ._recall import bm25_rank

        if kind not in ("profile", "event", "both"):
            raise ValueError(
                f"kind must be 'profile' | 'event' | 'both', got {kind!r}"
            )

        pool: List[MemoryEntry] = []
        if kind in ("profile", "both"):
            pool.extend(self.store.load())
        if kind in ("event", "both"):
            if days is not None:
                pool.extend(self._event_store.load_recent(days=days))
            else:
                pool.extend(self._event_store.load_all())

        if not pool:
            return []

        ranked = bm25_rank(query, pool)
        results: List[Dict[str, Any]] = []
        for entry, rank_score in ranked[:top_k]:
            payload = entry.to_dict()
            payload["rank_score"] = round(rank_score, 4)
            results.append(payload)
        return results

    # =================================================================
    # Misc accessors / maintenance
    # =================================================================

    def load_entries(self) -> List[MemoryEntry]:
        """Load all profile entries (events are queried via ``recall``)."""
        return self.store.load()

    def apply_review(self, review: dict) -> dict:
        """Apply a review result from memory-review skill.

        The review dict may contain:
        - merge_pairs: list of {id_a, id_b, merged_content}
        - deprecate_ids: list of entry IDs to deprecate (score *= 0.3)
        - reinforce_ids: list of entry IDs to reinforce
        - refined_entries: list of {id, content} for in-place content updates

        Returns stats dict. Raises IntegrityError if entries increase.

        Note: review currently operates on profile entries only — event
        memory is append-only by design and is not subject to review.
        """
        import fcntl  # Unix-only; project targets Linux/macOS

        lock_path = self.store.memory_path.with_suffix(".md.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        stats = {"merged": 0, "deprecated": 0, "reinforced": 0, "refined": 0}

        with open(lock_path, "w") as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                entries = self.store.load()
                entries_before = len(entries)
                entry_map = {e.id: e for e in entries}

                # 1. Merge pairs
                for pair in review.get("merge_pairs", []):
                    id_a = pair.get("id_a", "")
                    id_b = pair.get("id_b", "")
                    merged_content = pair.get("merged_content", "")
                    if id_a not in entry_map or id_b not in entry_map:
                        logger.warning("Merge pair references missing ID: %s, %s", id_a, id_b)
                        continue
                    merged = self.merger.merge_entries(
                        entry_map[id_a], entry_map[id_b], merged_content
                    )
                    del entry_map[id_a]
                    del entry_map[id_b]
                    entry_map[merged.id] = merged
                    stats["merged"] += 1

                # 2. Deprecate
                for eid in review.get("deprecate_ids", []):
                    if eid not in entry_map:
                        logger.warning("Deprecate references missing ID: %s", eid)
                        continue
                    entry_map[eid].score *= 0.3
                    stats["deprecated"] += 1

                # 3. Reinforce
                for eid in review.get("reinforce_ids", []):
                    if eid not in entry_map:
                        logger.warning("Reinforce references missing ID: %s", eid)
                        continue
                    self.merger.reinforce(entry_map[eid])
                    stats["reinforced"] += 1

                # 4. Refine (in-place content update)
                for item in review.get("refined_entries", []):
                    eid = item.get("id", "")
                    content = item.get("content", "")
                    if eid not in entry_map:
                        logger.warning("Refine references missing ID: %s", eid)
                        continue
                    entry_map[eid].content = content
                    stats["refined"] += 1

                result_entries = list(entry_map.values())

                # Integrity check: entries should not increase
                if len(result_entries) > entries_before:
                    raise IntegrityError(
                        f"Review should not increase entries: {entries_before} → {len(result_entries)}"
                    )

                self.store.save(result_entries)
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)

        return stats
