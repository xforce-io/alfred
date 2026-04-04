"""Memory review skill — consolidate and optimize agent memory.

Silent execution, no user notification.
Strategy: consolidate existing entries, then compress to USER.md profile.
"""

import logging
from typing import List

from ..runtime.skill_context import SkillContext
from ..scanners.session_scanner import SessionScanner
from ..scanners.reflection_state import ReflectionState
from .llm_utils import parse_json_response, parse_system_dph

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

    # Advance watermark — if we got here, both LLM calls succeeded.
    if last_successful_session:
        state.set_watermark("memory-review", last_successful_session.updated_at)
        state.save(context.workspace_path)
    return f"Memory review: {review_stats}, profile: {compress_result}"


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

    from pathlib import Path

    dph_path = Path(__file__).parent / "system_dphs" / "memory_review_consolidation.dph"
    dph_data = parse_system_dph(str(dph_path), {
        "existing_text": existing_text,
        "context_text": context_text,
    })
    sys_prompt = dph_data["config"].pop("system_prompt", "")
    model_override = dph_data["config"].pop("model", "")

    response = await llm.complete(
        dph_data["prompt"],
        system=sys_prompt,
        model_override=model_override,
        **dph_data["config"]
    )
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

    from pathlib import Path

    dph_path = Path(__file__).parent / "system_dphs" / "memory_review_compression.dph"
    dph_data = parse_system_dph(str(dph_path), {
        "entries_text": entries_text,
    })
    sys_prompt = dph_data["config"].pop("system_prompt", "")
    model_override = dph_data["config"].pop("model", "")

    response = await context.llm.complete(
        dph_data["prompt"],
        system=sys_prompt,
        model_override=model_override,
        **dph_data["config"]
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
