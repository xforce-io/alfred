"""High-level memory manager — orchestrates the memory lifecycle."""

import logging
import re
from pathlib import Path
from typing import Any, Dict, List

from .extractor import MemoryExtractor
from .merger import MemoryMerger
from .models import MemoryEntry
from .store import MemoryStore

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


class IntegrityError(Exception):
    """Raised when a memory review operation violates entropy constraints."""
    pass


class MemoryManager:
    """Unified interface for memory operations.

    Usage:
        mm = MemoryManager(memory_path, context)
        await mm.process_session_end(messages, session_id)

        # For prompt injection (no LLM context needed):
        mm = MemoryManager(memory_path)
        prompt = mm.get_prompt_memories(top_k=20)
    """

    def __init__(self, memory_path: Path, context: Any = None):
        self.store = MemoryStore(memory_path)
        self.merger = MemoryMerger()
        self._context = context

    async def process_session_end(
        self,
        messages: List[Dict[str, Any]],
        session_id: str,
    ) -> Dict[str, Any]:
        """Full memory lifecycle: load → extract → decay → merge → save.

        Args:
            messages: Conversation history from the session.
            session_id: Source session identifier.

        Returns:
            Stats dict with new_count, updated_count, total.
        """
        # 1. Load existing
        existing = self.store.load()
        last_processed = self.store.last_processed_count

        # 2. Extract via LLM
        if self._context is None:
            logger.warning("No LLM context; skipping extraction")
            return {"new_count": 0, "updated_count": 0, "total": len(existing)}

        # Only send new (unprocessed) messages to LLM.
        # If last_processed >= len(messages), this is a new session (not a
        # continuation of the same one), so process all messages.
        if 0 < last_processed < len(messages):
            new_messages = messages[last_processed:]
        else:
            new_messages = messages
        if not new_messages:
            logger.debug("No new messages since last extraction; skipping")
            return {"new_count": 0, "updated_count": 0, "total": len(existing)}

        extractor = MemoryExtractor(self._context)
        extract_result = await extractor.extract(new_messages, existing)

        # 3. Apply decay
        existing = self.merger.apply_decay(existing)

        # 4. Merge
        merge_result = self.merger.merge(
            existing=existing,
            new_extractions=extract_result.new_memories,
            reinforcements=extract_result.reinforced_ids,
            source_session=session_id,
            content_filter=_is_internal_content,
        )

        # 5. Save (record how many messages we've processed)
        self.store.save(merge_result.entries, last_processed_count=len(messages))

        stats = {
            "new_count": merge_result.new_count,
            "updated_count": merge_result.updated_count,
            "total": len(merge_result.entries),
        }
        logger.info("Memory processing complete: %s", stats)
        return stats

    def load_entries(self) -> List[MemoryEntry]:
        """Load all memory entries from store."""
        return self.store.load()

    def apply_review(self, review: dict) -> dict:
        """Apply a review result from memory-review skill.

        The review dict may contain:
        - merge_pairs: list of {id_a, id_b, merged_content}
        - deprecate_ids: list of entry IDs to deprecate (score *= 0.3)
        - reinforce_ids: list of entry IDs to reinforce
        - refined_entries: list of {id, content} for in-place content updates

        Returns stats dict. Raises IntegrityError if entries increase.
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

    def get_prompt_memories(self, top_k: int = 20) -> str:
        """Return formatted memory text for system prompt injection.

        Only includes entries with score >= 0.5, sorted by score descending.
        Near-duplicate entries are collapsed via greedy dedup so that the
        top-k slots contain diverse information.
        """
        from .merger import token_similarity, _SIMILARITY_THRESHOLD

        entries = self.store.load()
        if not entries:
            return ""

        # Filter and sort
        candidates = sorted(
            [e for e in entries if e.score >= 0.5
             and not _is_internal_content(e.content)],
            key=lambda e: e.score,
            reverse=True,
        )

        if not candidates:
            return ""

        # Greedy dedup: pick highest-score first, skip near-duplicates
        selected: list[MemoryEntry] = []
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
