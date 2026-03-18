"""Skill evaluation job — score recent segments using LLM Judge.

Runs as an isolated task, reads skill_logs/, produces eval_report.json.
"""

import logging

from ..runtime.skill_context import SkillContext
from ..slm.judge import evaluate_skill
from ..slm.models import EvalReport
from ..slm.segment_logger import SegmentLogger
from ..slm.version_manager import VersionManager

logger = logging.getLogger(__name__)


async def run(context: SkillContext) -> str:
    """Evaluate all skills that have accumulated new segments since last report."""
    from ...infra.user_data import get_user_data_manager

    udm = get_user_data_manager()
    seg_logger = SegmentLogger(udm.skill_logs_dir)
    ver_mgr = VersionManager(udm.skills_dir)

    skill_ids = seg_logger.list_skills()
    if not skill_ids:
        return "No skill logs found"

    evaluated = 0
    for skill_id in skill_ids:
        try:
            result = await _evaluate_one(context, seg_logger, ver_mgr, skill_id)
            if result:
                evaluated += 1
                logger.info("Evaluated %s: %s", skill_id, result)
        except Exception as e:
            logger.warning("Failed to evaluate %s: %s", skill_id, e)

    # Cleanup old segments
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
) -> str | None:
    """Evaluate a single skill. Returns summary string or None if skipped."""
    segments = seg_logger.load(skill_id)
    if not segments:
        return None

    # Group by version, evaluate the latest version
    versions: dict[str, list] = {}
    for seg in segments:
        versions.setdefault(seg.skill_version, []).append(seg)

    # Find the current version to evaluate
    pointer = ver_mgr.get_pointer(skill_id)
    target_version = pointer.current_version if pointer else "baseline"
    target_segments = versions.get(target_version, [])

    if not target_segments:
        return None

    # Check if we already have a report with same segment count
    existing = ver_mgr.get_eval_report(skill_id, target_version)
    if existing and existing.segment_count >= len(target_segments):
        return None  # already evaluated

    report = await evaluate_skill(context.llm, skill_id, target_version, target_segments)
    ver_mgr.save_eval_report(skill_id, target_version, report)

    return (
        f"v{target_version}: {report.segment_count} segments, "
        f"critical={report.critical_issue_rate:.0%}, "
        f"satisfaction={report.mean_satisfaction:.2f}"
    )
