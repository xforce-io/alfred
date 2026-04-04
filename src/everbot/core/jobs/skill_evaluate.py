"""Skill evaluation job — score skill invocations using LLM Judge.

Runs as a per-agent task, reads inline evaluation segments from the
agent's skill_logs/ directory, and produces eval_report.json in the
agent's skill_eval/ directory.
"""

import logging

from ..runtime.skill_context import SkillContext
from ..slm.judge import evaluate_skill
from ..slm.segment_logger import SegmentLogger
from ..slm.version_manager import VersionManager

logger = logging.getLogger(__name__)


async def run(context: SkillContext) -> str:
    """Evaluate all skills that have accumulated new entries since last report."""
    from ...infra.user_data import get_user_data_manager

    udm = get_user_data_manager()

    # Use agent-scoped dirs from context, fall back to global for backward compat
    skill_logs_dir = context.skill_logs_dir or udm.skill_logs_dir
    skill_eval_dir = context.skill_eval_dir  # None → legacy .eval/ under skills_dir

    seg_logger = SegmentLogger(skill_logs_dir)
    ver_mgr = VersionManager(udm.skills_dir, eval_base_dir=skill_eval_dir)

    skill_ids = seg_logger.list_skills()
    if not skill_ids:
        return "No skill logs found"

    from .llm_errors import LLMTransientError, LLMConfigError

    evaluated = 0
    for skill_id in skill_ids:
        try:
            result = await _evaluate_one(
                context, seg_logger, ver_mgr, skill_id, udm.sessions_dir,
            )
            if result:
                evaluated += 1
                logger.info("Evaluated %s: %s", skill_id, result)
        except (LLMTransientError, LLMConfigError):
            logger.warning("LLM unavailable during %s evaluation, aborting remaining", skill_id)
            raise
        except Exception as e:
            logger.warning("Failed to evaluate %s: %s", skill_id, e)

    # Cleanup old entries
    for skill_id in skill_ids:
        try:
            seg_logger.cleanup(skill_id)
        except Exception as e:
            logger.warning("Cleanup failed for %s: %s", skill_id, e)

    return f"Evaluated {evaluated}/{len(skill_ids)} skills"


async def _evaluate_one(
    context: SkillContext,
    seg_logger: SegmentLogger,
    ver_mgr: VersionManager,
    skill_id: str,
    sessions_dir,
) -> str | None:
    """Evaluate a single skill. Returns summary string or None if skipped."""
    entries = seg_logger.load(skill_id)
    if not entries:
        return None

    # Find the current version to evaluate
    pointer = ver_mgr.get_pointer(skill_id)
    target_version = pointer.current_version if pointer else "baseline"
    target_entries = [e for e in entries if e.skill_version == target_version]

    if not target_entries:
        return None

    # Check if we already have a report with same segment count
    existing = ver_mgr.get_eval_report(skill_id, target_version)
    if existing and existing.segment_count >= len(target_entries):
        return None  # already evaluated

    # Skip segments with no content (e.g. malformed or incomplete records)
    segments = [e for e in target_entries if e.skill_output or e.context_before]
    if not segments:
        logger.info("No segments with content for %s v%s", skill_id, target_version)
        return None

    report = await evaluate_skill(context.llm, skill_id, target_version, segments)
    ver_mgr.save_eval_report(skill_id, target_version, report)

    return (
        f"v{target_version}: {report.segment_count} segments, "
        f"critical={report.critical_issue_rate:.0%}, "
        f"satisfaction={report.mean_satisfaction:.2f}"
    )
