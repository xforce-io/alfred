"""High-level memory manager — orchestrates the memory lifecycle."""

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

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
