"""Memory review — consolidate facts and rebuild the USER.md projection."""

import logging
import os
from pathlib import Path
from typing import List, Optional

from ..runtime.skill_context import SkillContext
from ..scanners.session_scanner import SessionScanner
from ..scanners.reflection_state import ReflectionState
from .llm_utils import parse_json_response, parse_system_dph

logger = logging.getLogger(__name__)
_DERIVED_PROFILE_MARKER = "<!-- derived_from_memory: true -->"
_EMPTY_PROFILE = "（暂无活跃画像）"
_MAX_ANALYZED_SESSIONS = 3


async def run(context: SkillContext) -> Optional[str]:
    """Execute memory review: consolidate entries and compress to profile.

    Returns a concise profile terminal when a projection was cleared or
    rebuilt, and None only for a true no-change run. Failures propagate so
    cron records degraded/failed and the unchanged watermark permits retry.
    """
    scanner = SessionScanner(context.sessions_dir)
    state = ReflectionState.load(context.workspace_path)

    # 1. Get sessions: reuse gate result if available, otherwise query directly
    skill_wm = state.get_watermark("memory-review")
    if context.scan_result and context.scan_result.payload:
        sessions = context.scan_result.payload
    else:
        sessions = scanner.get_reviewable_sessions(skill_wm, agent_name=context.agent_name)
    if not sessions:
        user_path = context.workspace_path / "USER.md"
        current_user = user_path.read_text(encoding="utf-8") if user_path.exists() else ""
        if _DERIVED_PROFILE_MARKER in current_user:
            return None
        existing = context.memory_manager.load_entries()
        profile_content, profile_result = await _render_user_profile(
            context, existing,
        )
        _commit_profile_if_memory_unchanged(
            context,
            user_path,
            profile_content,
            existing,
        )
        return profile_result

    # 2. Extract digests, skip failed sessions
    digests, successful_sessions = [], []
    for s in sessions:
        try:
            digests.append(
                f"[source_session:{s.id}]\n{scanner.extract_digest(s.path)}"
            )
            successful_sessions.append(s)
        except Exception as e:
            logger.warning("Failed to extract session %s: %s, skipping", s.id, e)
            continue

    if not digests:
        return None

    # 3. Consolidation analysis (single LLM call).  Every downstream boundary
    # uses this exact batch: prompt, correction source allowlist, and watermark.
    analyzed_digests = digests[:_MAX_ANALYZED_SESSIONS]
    analyzed_sessions = successful_sessions[:_MAX_ANALYZED_SESSIONS]
    existing = context.memory_manager.load_entries()
    review = await _analyze_memory_consolidation(
        context.llm, analyzed_digests, existing,
    )
    review = _validate_review_sources(
        review, [session.id for session in analyzed_sessions],
    )

    # 4. Project in memory and finish all LLM work before mutating any file.
    projected, review_stats = context.memory_manager.preview_review(review, existing)
    profile_content, profile_result = await _render_user_profile(context, projected)

    # 5. Recoverable commit boundary: MEMORY + USER + watermark.
    memory_path = context.memory_manager.store.memory_path
    user_path = context.workspace_path / "USER.md"
    state_path = context.workspace_path / ".reflection_state.json"
    snapshots = {
        memory_path: _snapshot_file(memory_path),
        user_path: _snapshot_file(user_path),
        state_path: _snapshot_file(state_path),
    }
    expected = context.memory_manager.entries_fingerprint(existing)
    with context.memory_manager.review_lock():
        try:
            context.memory_manager.commit_review(
                projected,
                expected_fingerprint=expected,
                lock_already_held=True,
            )
            _atomic_write_profile(user_path, profile_content)
            if analyzed_sessions:
                state.set_watermark(
                    "memory-review", analyzed_sessions[-1].updated_at,
                )
                if not state.save(context.workspace_path):
                    raise OSError("failed to persist memory-review watermark")
        except Exception:
            for path, snapshot in snapshots.items():
                _restore_file(path, snapshot)
            raise

    logger.info("Memory review: %s, profile: %s", review_stats, profile_result)
    return (
        f"{profile_result}; corrected={review_stats['corrected']}; "
        f"split={review_stats['split']}"
    )


async def _analyze_memory_consolidation(llm, digests: List[str], existing_entries) -> dict:
    """Analyze memory entries for consolidation opportunities.

    Returns dict with: merge_pairs, deprecate_ids, reinforce_ids, refined_entries.
    """
    if not existing_entries:
        return {}

    active_entries = [
        entry for entry in existing_entries if entry.status == "active"
    ]
    if not active_entries:
        return {}

    existing_text = "\n".join(
        f"- [{e.id}] [{e.category}] (score={e.score:.2f}, count={e.activation_count}) {e.content}"
        for e in active_entries
    )
    # Corrections often occur near the end of a long session. Preserve the
    # source header plus the recent tail instead of truncating at 500 chars.
    context_text = "\n".join(_review_digest_window(d) for d in digests)

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
    if not isinstance(result, dict):
        raise ValueError("memory review response must be a JSON object")

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


def _review_digest_window(digest: str, limit: int = 2000) -> str:
    if len(digest) <= limit:
        return digest
    return f"{digest[:200]}\n...[earlier context truncated]...\n{digest[-(limit - 240):]}"


def _validate_review_sources(review: dict, session_ids: List[str]) -> dict:
    """Reject corrective facts whose claimed source is outside this review batch."""
    allowed = set(session_ids)
    corrections = []
    for item in review.get("corrections", []):
        if not isinstance(item, dict):
            continue
        source = str(item.get("source_session", ""))
        if source not in allowed:
            logger.warning("Skipping correction with untrusted source session: %s", source)
            continue
        corrections.append(item)
    review["corrections"] = corrections
    return review


async def _compress_to_user_profile(context: SkillContext) -> str:
    """Compress all memory entries into structured tags and write to USER.md.

    This replaces verbose narrative memories with a compact user profile
    that is injected into the system prompt via the USER.md section.
    """
    content, result = await _render_user_profile(
        context, context.memory_manager.load_entries(),
    )
    _atomic_write_profile(context.workspace_path / "USER.md", content)
    return result


async def _render_user_profile(context: SkillContext, entries) -> tuple[str, str]:
    """Render a USER.md projection without writing it."""
    active = [e for e in entries if e.status == "active" and e.score >= 0.5]
    if not active:
        return (
            f"# 用户画像\n\n{_DERIVED_PROFILE_MARKER}\n\n{_EMPTY_PROFILE}\n",
            "profile_cleared",
        )

    entries_text = "\n".join(
        f"- [{e.category}] {e.content}" for e in active
    )

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

    return (
        f"# 用户画像\n\n{_DERIVED_PROFILE_MARKER}\n\n{profile_content}\n",
        f"profile_rebuilt:{len(active)}",
    )


def _atomic_write_profile(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _commit_profile_if_memory_unchanged(
    context: SkillContext,
    user_path: Path,
    profile_content: str,
    existing_entries,
) -> None:
    """Commit a bootstrap USER projection against an unchanged MEMORY view."""
    from ..memory.manager import IntegrityError

    expected = context.memory_manager.entries_fingerprint(existing_entries)
    with context.memory_manager.review_lock():
        current = context.memory_manager.load_entries()
        if context.memory_manager.entries_fingerprint(current) != expected:
            raise IntegrityError("Memory changed concurrently; profile must retry")
        user_snapshot = _snapshot_file(user_path)
        try:
            _atomic_write_profile(user_path, profile_content)
        except Exception:
            _restore_file(user_path, user_snapshot)
            raise


def _snapshot_file(path: Path) -> Optional[bytes]:
    return path.read_bytes() if path.exists() else None


def _restore_file(path: Path, snapshot: Optional[bytes]) -> None:
    """Restore one file atomically after a failed multi-file commit."""
    if snapshot is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".rollback")
    tmp.write_bytes(snapshot)
    os.replace(tmp, path)
