"""High-level memory manager — orchestrates profile and event memory.

The manager is the only public entry point for memory operations. It
hides the two-layer split (profile vs event) from callers and runs both
extraction pipelines on the same conversation slice. Failures in one
layer never block the other.
"""

import logging
import re
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .event_extractor import EventExtractor
from .event_store import EventStore
from .merger import MemoryMerger
from .models import MemoryEntry, new_id
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

        superseded = [entry for entry in existing if entry.status == "superseded"]
        active_existing = self.merger.apply_profile_decay(
            [entry for entry in existing if entry.status == "active"]
        )

        merge_result = self.merger.merge(
            existing=active_existing,
            new_extractions=extract_result.new_memories,
            reinforcements=extract_result.reinforced_ids,
            source_session=session_id,
            content_filter=_is_internal_content,
        )

        # Save advances the watermark even when extraction produced
        # nothing new — otherwise the same messages would be re-extracted
        # on the next call.
        self.store.save(
            merge_result.entries + superseded,
            last_processed_count=total_messages,
        )

        return {
            "new_count": merge_result.new_count,
            "updated_count": merge_result.updated_count,
            "total": len(merge_result.entries) + len(superseded),
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
                if e.status == "active"
                and e.score >= _PROFILE_INJECT_THRESHOLD
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
            pool.extend(entry for entry in self.store.load() if entry.status == "active")
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

    def preview_review(
        self,
        review: dict,
        existing_entries: Optional[List[MemoryEntry]] = None,
    ) -> tuple[List[MemoryEntry], dict]:
        """Project a review without writing, including correction/split relations."""
        entries = existing_entries if existing_entries is not None else self.store.load()
        entry_map = {
            entry.id: MemoryEntry.from_dict(entry.to_dict())
            for entry in entries
        }
        active_before = sum(1 for entry in entry_map.values() if entry.status == "active")
        stats = {
            "merged": 0,
            "deprecated": 0,
            "reinforced": 0,
            "refined": 0,
            "corrected": 0,
            "split": 0,
        }

        # Explicit corrections have priority over ordinary consolidation.
        for item in review.get("corrections", []):
            if not isinstance(item, dict):
                continue
            content = str(item.get("content", "")).strip()
            category = str(item.get("category", "fact")).strip()
            source_ids = list(dict.fromkeys(
                str(value) for value in item.get("supersedes_ids", []) if value
            ))
            sources = [
                entry_map[source_id]
                for source_id in source_ids
                if source_id in entry_map and entry_map[source_id].status == "active"
            ]
            if not content or category not in _PROFILE_CATEGORIES or not sources:
                logger.warning("Skipping invalid memory correction for IDs %s", source_ids)
                continue
            if any(
                entry.status == "active"
                and _normalize_fact(entry.content) == _normalize_fact(content)
                and set(source_ids).issubset(set(entry.supersedes))
                for entry in entry_map.values()
            ):
                continue
            replacement = _new_review_entry(
                content=content,
                category=category,
                importance="high",
                source_session=str(item.get("source_session", "")),
                supersedes=[source.id for source in sources],
            )
            entry_map[replacement.id] = replacement
            for source in sources:
                source.status = "superseded"
                source.superseded_by = [replacement.id]
                source.score = min(source.score, 0.19)
            stats["corrected"] += 1

        # Split giant mixed entries into independently invalidatable facts.
        for item in review.get("split_entries", []):
            if not isinstance(item, dict):
                continue
            source_id = str(item.get("id", ""))
            source = entry_map.get(source_id)
            raw_children = item.get("entries", [])
            if source is None or source.status != "active" or not isinstance(raw_children, list):
                continue
            children: List[MemoryEntry] = []
            seen_content: set[str] = set()
            for child in raw_children[:8]:
                if not isinstance(child, dict):
                    continue
                content = str(child.get("content", "")).strip()
                category = str(child.get("category", "fact")).strip()
                normalized = _normalize_fact(content)
                if (
                    not content
                    or category not in _PROFILE_CATEGORIES
                    or normalized in seen_content
                    or _is_internal_content(content)
                ):
                    continue
                seen_content.add(normalized)
                children.append(_new_review_entry(
                    content=content,
                    category=category,
                    importance=str(child.get("importance", "medium")),
                    source_session=str(child.get("source_session") or source.source_session),
                    supersedes=[source.id],
                ))
            if not children:
                continue
            source.status = "superseded"
            source.superseded_by = [child.id for child in children]
            source.score = min(source.score, 0.19)
            for child in children:
                entry_map[child.id] = child
            stats["split"] += 1

        # Existing consolidation operations remain backward compatible.
        consumed_merge_ids = set()
        for pair in review.get("merge_pairs", []):
            id_a = pair.get("id_a", "")
            id_b = pair.get("id_b", "")
            merged_content = pair.get("merged_content", "")
            if id_a == id_b or id_a in consumed_merge_ids or id_b in consumed_merge_ids:
                continue
            if id_a not in entry_map or id_b not in entry_map:
                continue
            if entry_map[id_a].status != "active" or entry_map[id_b].status != "active":
                continue
            merged = self.merger.merge_entries(entry_map[id_a], entry_map[id_b], merged_content)
            del entry_map[id_a]
            del entry_map[id_b]
            consumed_merge_ids.update((id_a, id_b))
            entry_map[merged.id] = merged
            stats["merged"] += 1

        for entry_id in review.get("deprecate_ids", []):
            entry = entry_map.get(entry_id)
            if entry is None or entry.status != "active":
                continue
            entry.score *= 0.3
            stats["deprecated"] += 1

        for entry_id in review.get("reinforce_ids", []):
            entry = entry_map.get(entry_id)
            if entry is None or entry.status != "active":
                continue
            self.merger.reinforce(entry)
            stats["reinforced"] += 1

        for item in review.get("refined_entries", []):
            entry = entry_map.get(item.get("id", "")) if isinstance(item, dict) else None
            content = str(item.get("content", "")).strip() if isinstance(item, dict) else ""
            if entry is None or entry.status != "active" or not content:
                continue
            entry.content = content
            stats["refined"] += 1

        result_entries = list(entry_map.values())
        active_after = sum(1 for entry in result_entries if entry.status == "active")
        allowed_growth = sum(
            max(0, len(item.get("entries", [])) - 1)
            for item in review.get("split_entries", [])
            if isinstance(item, dict)
        )
        if active_after > active_before + allowed_growth:
            raise IntegrityError(
                f"Review unexpectedly increased active entries: {active_before} → {active_after}"
            )
        return result_entries, stats

    @staticmethod
    def entries_fingerprint(entries: List[MemoryEntry]) -> str:
        """Stable optimistic-concurrency fingerprint for profile entries."""
        # Fingerprint only fields persisted by ProfileStore. created_at and
        # source_session are absent from legacy MEMORY.md and are synthesized
        # on load, so including them would create false concurrency failures.
        payload = [
            {
                "id": entry.id,
                "content": entry.content,
                "category": entry.category,
                "score": round(entry.score, 2),
                "last_activated": entry.last_activated[:10],
                "activation_count": entry.activation_count,
                "source_session": entry.source_session,
                "status": entry.status,
                "supersedes": entry.supersedes,
                "superseded_by": entry.superseded_by,
            }
            for entry in sorted(entries, key=lambda item: item.id)
        ]
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def commit_review(
        self,
        projected_entries: List[MemoryEntry],
        *,
        expected_fingerprint: str,
        lock_already_held: bool = False,
    ) -> None:
        """Commit a precomputed review only if MEMORY has not changed."""
        if lock_already_held:
            self._commit_review_unlocked(projected_entries, expected_fingerprint)
            return
        with self.review_lock():
            self._commit_review_unlocked(projected_entries, expected_fingerprint)

    def _commit_review_unlocked(
        self,
        projected_entries: List[MemoryEntry],
        expected_fingerprint: str,
    ) -> None:
        current = self.store.load()
        if self.entries_fingerprint(current) != expected_fingerprint:
            raise IntegrityError("Memory changed concurrently; review must retry")
        self.store.save(projected_entries)

    @contextmanager
    def review_lock(self):
        """Serialize the short MEMORY/USER/watermark review commit boundary."""
        import fcntl

        lock_path = self.store.memory_path.with_suffix(".md.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)

    def apply_review(self, review: dict) -> dict:
        """Apply a review result from memory-review skill.

        The review dict may contain:
        - corrections: replacement facts plus superseded active IDs
        - split_entries: atomic children replacing one mixed active entry
        - merge_pairs: list of {id_a, id_b, merged_content}
        - deprecate_ids: list of entry IDs to deprecate (score *= 0.3)
        - reinforce_ids: list of entry IDs to reinforce
        - refined_entries: list of {id, content} for in-place content updates

        Returns stats dict. Raises IntegrityError on unexpected active growth
        or a concurrent MEMORY change.

        Note: review currently operates on profile entries only — event
        memory is append-only by design and is not subject to review.
        """
        existing = self.store.load()
        projected, stats = self.preview_review(review, existing)
        self.commit_review(
            projected,
            expected_fingerprint=self.entries_fingerprint(existing),
        )
        return stats


_PROFILE_CATEGORIES = {"preference", "fact", "workflow", "decision", "experience"}
_IMPORTANCE_SCORES = {"high": 0.9, "medium": 0.7, "low": 0.5}


def _normalize_fact(content: str) -> str:
    return "".join(content.lower().split())


def _new_review_entry(
    *,
    content: str,
    category: str,
    importance: str,
    source_session: str,
    supersedes: List[str],
) -> MemoryEntry:
    now = datetime.now(timezone.utc).isoformat()
    return MemoryEntry(
        id=new_id(),
        content=content,
        category=category,
        score=_IMPORTANCE_SCORES.get(importance, 0.7),
        created_at=now,
        last_activated=now,
        activation_count=1,
        source_session=source_session,
        status="active",
        supersedes=supersedes,
    )
