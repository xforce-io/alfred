"""High-level memory manager — orchestrates the memory lifecycle."""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from .extractor import MemoryExtractor
from .merger import MemoryMerger
from .models import MemoryEntry
from .store import MemoryStore

logger = logging.getLogger(__name__)


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

        # Only send new (unprocessed) messages to LLM
        new_messages = messages[last_processed:] if last_processed > 0 else messages
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
        """
        entries = self.store.load()
        if not entries:
            return ""

        # Filter and sort
        qualified = sorted(
            [e for e in entries if e.score >= 0.5],
            key=lambda e: e.score,
            reverse=True,
        )[:top_k]

        if not qualified:
            return ""

        lines = ["# 历史记忆", "", "关于用户的关键信息：", ""]
        for entry in qualified:
            lines.append(f"- [{entry.category}] {entry.content}")

        return "\n".join(lines)
