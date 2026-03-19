"""Memory review skill — consolidate and optimize agent memory.

Silent execution, no user notification.
Strategy: consolidate existing entries, then compress to USER.md profile.
"""

import logging
from typing import List

from ..runtime.skill_context import SkillContext
from ..scanners.session_scanner import SessionScanner
from ..scanners.reflection_state import ReflectionState
from .llm_utils import parse_json_response

logger = logging.getLogger(__name__)


async def run(context: SkillContext) -> str:
    """Execute memory review: consolidate entries and compress to profile."""
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

    # 3. Consolidation analysis (single LLM call)
    existing = context.memory_manager.load_entries()
    review = await _analyze_memory_consolidation(context.llm, digests, existing)

    # 4. Apply consolidation + post-validation
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

    # 5. Compress memories → USER.md
    compress_result = await _compress_to_user_profile(context)

    # 6. Advance watermark
    if last_successful_session:
        state.set_watermark("memory-review", last_successful_session.updated_at)
        state.save(context.workspace_path)

    parts = []
    if review_stats.get("merged"):
        parts.append(f"{review_stats['merged']} merged")
    if review_stats.get("refined"):
        parts.append(f"{review_stats['refined']} refined")
    if review_stats.get("deprecated"):
        parts.append(f"{review_stats['deprecated']} deprecated")
    if review_stats.get("reinforced"):
        parts.append(f"{review_stats['reinforced']} reinforced")
    review_summary = ", ".join(parts) if parts else "no changes"

    return f"Memory review: {review_summary}; profile: {compress_result}"


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
- refine: updates content in-place, no score change, does NOT count toward entropy
- Total effect must be entropy-reducing: merge_count + deprecate_count >= reinforce_count
- Refine freely — it improves clarity without affecting entry count or entropy balance

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


async def _compress_to_user_profile(context: SkillContext) -> str:
    """Compress all memory entries into structured tags and write to USER.md.

    This replaces verbose narrative memories with a compact user profile
    that is injected into the system prompt via the USER.md section.
    """
    entries = context.memory_manager.load_entries()
    if not entries:
        return "no entries"

    # Only compress entries with reasonable score
    active = [e for e in entries if e.score >= 0.5]
    if not active:
        return "no active entries"

    entries_text = "\n".join(
        f"- [{e.category}] {e.content}" for e in active
    )

    prompt = f"""Compress these user memory entries into a structured profile.

## Memory Entries
{entries_text}

## Task
Deduplicate and compress into a structured tag format. Rules:
- Group by dimension (技术能力, 偏好, 工作流, 投资, etc.)
- Each dimension is one line with comma-separated tags
- Merge redundant entries (e.g. 5 entries about "user knows code" → one tag)
- Keep only actionable information that affects how to interact with the user
- Use Chinese, keep each tag under 15 characters
- Total output should be under 200 characters

## Output Format (exact format, no extra text)
- 技术能力: tag1, tag2, tag3
- 偏好: tag1, tag2
- 工作流: tag1, tag2
- 投资: tag1, tag2"""

    try:
        response = await context.llm.complete(
            prompt,
            system="You are a profile compression engine. Output only the structured tags, nothing else.",
        )
        profile_content = response.strip()

        # Write to USER.md
        user_md_path = context.workspace_path / "USER.md"
        user_md_path.write_text(
            f"# 用户画像\n\n{profile_content}\n",
            encoding="utf-8",
        )
        logger.info("Compressed %d memory entries to USER.md", len(active))
        return f"compressed {len(active)} entries"
    except Exception as e:
        logger.error("Memory compression to USER.md failed: %s", e)
        return f"error: {e}"

