"""Memory review skill — consolidate and optimize agent memory.

Silent execution, no user notification.
Strategy: supplement first (re-extract missed sessions), then consolidate.
"""

import logging
from typing import List

from ..runtime.skill_context import SkillContext
from ..scanners.session_scanner import SessionScanner
from ..scanners.reflection_state import ReflectionState
from .llm_utils import parse_json_response

logger = logging.getLogger(__name__)


async def run(context: SkillContext) -> str:
    """Execute memory review: supplement missed extractions, then consolidate."""
    scanner = SessionScanner(context.sessions_dir)
    state = ReflectionState.load(context.workspace_path)

    # 1. Get sessions: reuse gate result if available, otherwise query directly
    skill_wm = state.get_watermark("memory-review")
    if context.scan_result and context.scan_result.payload:
        sessions = context.scan_result.payload
    else:
        sessions = scanner.get_reviewable_sessions(skill_wm, agent_name=context.agent_name)
    if not sessions:
        return "No sessions to review"

    # 2. Extract digests, skip failed sessions
    digests, digest_session_ids = [], []
    last_successful_session = None
    for s in sessions:
        try:
            digests.append(scanner.extract_digest(s.path))
            digest_session_ids.append(s.id)
            last_successful_session = s
        except Exception as e:
            logger.warning("Failed to extract session %s: %s, skipping", s.id, e)
            continue

    if not digests:
        return "All sessions failed to extract"

    # 3. Detect missed sessions (lightweight LLM call)
    reextract_count = 0
    existing = context.memory_manager.load_entries()
    missed = await _detect_missed_sessions(context.llm, digests, digest_session_ids, existing)
    # Only allow session IDs that were actually scanned (guard against LLM hallucination)
    valid_sids = set(digest_session_ids)
    missed = [sid for sid in missed if sid in valid_sids]
    for sid in missed[:2]:  # Re-extract limit: 2
        try:
            msgs = scanner.load_session_messages(sid)
            # process_session_end needs context (LLM) — create a temporary manager
            from ..memory.manager import MemoryManager
            mm = MemoryManager(context.workspace_path / "MEMORY.md", context=context.llm)
            await mm.process_session_end(msgs, sid)
            reextract_count += 1
        except Exception as e:
            logger.warning("Re-extract failed for %s: %s, skipping", sid, e)

    # 4. Consolidation analysis (single LLM call)
    existing = context.memory_manager.load_entries()  # Reload after re-extraction
    review = await _analyze_memory_consolidation(context.llm, digests, existing)

    # 5. Apply consolidation + post-validation
    entries_before = len(existing)
    from ..memory.manager import IntegrityError
    try:
        review_stats = context.memory_manager.apply_review(review)
    except IntegrityError as e:
        logger.error("Memory consolidation integrity violation: %s", e)
        return f"IntegrityError: {e}"

    # Defense against concurrent writes: apply_review holds flock but another
    # process_session_end could have inserted entries between our load and save.
    entries_after = len(context.memory_manager.load_entries())
    if entries_after > entries_before:
        logger.warning(
            "Entries increased after review (likely concurrent write): %d → %d",
            entries_before, entries_after,
        )

    # 6. Advance watermark
    if last_successful_session:
        state.set_watermark("memory-review", last_successful_session.updated_at)
        state.save(context.workspace_path)

    return f"Memory review: {review_stats}, re-extracted: {reextract_count}"


async def _detect_missed_sessions(
    llm, digests: List[str], session_ids: List[str], existing_entries
) -> List[str]:
    """Detect sessions that may have missed memory extraction.

    Returns list of session_ids that should be re-extracted.
    """
    if not digests or not existing_entries:
        return []

    existing_summary = "\n".join(
        f"- [{e.category}] {e.content}" for e in existing_entries[:30]
    )
    sessions_text = ""
    for sid, digest in zip(session_ids, digests):
        sessions_text += f"\n--- Session {sid} ---\n{digest[:1000]}\n"

    prompt = f"""Analyze these conversation sessions and existing memories.
Identify sessions where important user information was mentioned but NOT captured in existing memories.

## Existing Memories
{existing_summary}

## Recent Sessions
{sessions_text}

## Task
Return a JSON object with session_ids that should be re-processed for memory extraction.
Only include sessions where significant user preferences, facts, or decisions were missed.
Be conservative — only flag truly missed information.

Output format:
```json
{{"session_ids": ["session_id_1", "session_id_2"]}}
```"""

    try:
        response = await llm.complete(prompt, system="You are a memory analysis engine. Output valid JSON only.")
        result = parse_json_response(response)
        return result.get("session_ids", [])
    except Exception as e:
        logger.warning("Missed session detection failed: %s", e)
        return []


async def _analyze_memory_consolidation(llm, digests: List[str], existing_entries) -> dict:
    """Analyze memory entries for consolidation opportunities.

    Returns dict with: merge_pairs, deprecate_ids, reinforce_ids, refined_entries.
    """
    if not existing_entries:
        return {}

    existing_text = "\n".join(
        f"- [{e.id}] [{e.category}] (score={e.score:.2f}, count={e.activation_count}) {e.content}"
        for e in existing_entries
    )
    context_text = "\n".join(d[:500] for d in digests[:3])

    prompt = f"""Analyze these memory entries for consolidation.

## Current Memory Entries
{existing_text}

## Recent Conversation Context
{context_text}

## Tasks
1. Find pairs of entries that can be merged (similar or overlapping content)
2. Find entries that are outdated or no longer relevant (deprecate)
3. Find entries reinforced by recent conversations (reinforce)
4. Find entries whose content can be refined for clarity (refine, in-place update only)

## Constraints
- merge: creates 1 new entry, removes 2 → net -1
- deprecate: reduces score (accelerates natural decay)
- reinforce: boosts score of existing entry
- refine: updates content in-place, no score change
- Total effect must be entropy-reducing: merge_count + deprecate_count >= reinforce_count

Output format:
```json
{{
  "merge_pairs": [{{"id_a": "...", "id_b": "...", "merged_content": "..."}}],
  "deprecate_ids": ["..."],
  "reinforce_ids": ["..."],
  "refined_entries": [{{"id": "...", "content": "..."}}]
}}
```"""

    try:
        response = await llm.complete(prompt, system="You are a memory consolidation engine. Output valid JSON only.")
        result = parse_json_response(response)

        # Validate entropy constraint
        merge_count = len(result.get("merge_pairs", []))
        deprecate_count = len(result.get("deprecate_ids", []))
        reinforce_count = len(result.get("reinforce_ids", []))
        if merge_count + deprecate_count < reinforce_count:
            logger.warning(
                "Entropy constraint violated: merge=%d + deprecate=%d < reinforce=%d, trimming reinforcements",
                merge_count, deprecate_count, reinforce_count,
            )
            allowed = merge_count + deprecate_count
            result["reinforce_ids"] = result.get("reinforce_ids", [])[:allowed]

        return result
    except Exception as e:
        logger.error("Memory consolidation analysis failed: %s", e)
        return {}

