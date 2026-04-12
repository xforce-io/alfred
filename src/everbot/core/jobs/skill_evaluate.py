"""Skill evaluation job — score skill invocations using LLM Judge.

Runs as a per-agent task, reads inline evaluation segments from the
agent's skill_logs/ directory, and produces eval_report.json in the
agent's skill_eval/ directory.

Post-evaluation: unhealthy skills are rolled back and improved via LLM.
Testing versions that pass evaluation are activated.
"""

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import List

from ..runtime.skill_context import SkillContext
from ..slm.judge import evaluate_skill
from ..slm.models import EvaluationSegment, EvalReport, VersionStatus
from ..slm.segment_logger import SegmentLogger
from ..slm.version_manager import VersionManager

logger = logging.getLogger(__name__)
_SKILL_EVALUATION_TIMEOUT_SECONDS = 120
MAX_CONSECUTIVE_EVOLVE = 2

_EVOLVE_SYSTEM = (
    "You are a skill improvement assistant. "
    "Given a skill definition and examples of failed invocations, "
    "output an improved version of the full skill file. "
    "Output ONLY the complete improved skill file content, nothing else."
)

_EVOLVE_PROMPT = """\
The following skill definition needs improvement. Based on the failure cases below, \
produce an improved version.

## Current Skill Definition

```
{skill_content}
```

## Failure Cases

{failure_block}

## Instructions

1. Analyze why the skill produced bad outputs in these cases.
2. Modify the skill definition to fix the identified issues.
3. Only change parts that caused the failures. Keep everything else intact.
4. The output must be a complete, valid skill file starting with `---` frontmatter.
5. Update the `version` field in the frontmatter to: "{new_version}"
"""


async def run(context: SkillContext) -> str:
    """Evaluate all skills that have accumulated new entries since last report."""
    from ...infra.user_data import get_user_data_manager

    udm = get_user_data_manager()

    skill_logs_dir = context.skill_logs_dir or udm.skill_logs_dir
    skill_eval_dir = context.skill_eval_dir

    seg_logger = SegmentLogger(skill_logs_dir)
    ver_mgr = VersionManager(udm.skills_dir, eval_base_dir=skill_eval_dir)

    skill_ids = seg_logger.list_skills()
    if not skill_ids:
        return "HEARTBEAT_OK No skill logs found"

    from .llm_errors import LLMTransientError, LLMConfigError

    evaluated = 0
    unavailable = 0
    for skill_id in skill_ids:
        try:
            result = await _evaluate_one(
                context, seg_logger, ver_mgr, skill_id, udm.sessions_dir,
            )
            if result:
                evaluated += 1
                logger.info("Evaluated %s: %s", skill_id, result)
        except (LLMTransientError, LLMConfigError):
            unavailable += 1
            logger.warning("LLM unavailable during %s evaluation, skipping skill", skill_id)
        except Exception as e:
            logger.warning("Failed to evaluate %s: %s", skill_id, e)

    for skill_id in skill_ids:
        try:
            seg_logger.cleanup(skill_id)
        except Exception as e:
            logger.warning("Cleanup failed for %s: %s", skill_id, e)

    summary = f"Evaluated {evaluated}/{len(skill_ids)} skills"
    if unavailable:
        summary += f", skipped {unavailable} due to LLM unavailability"
        return summary
    return f"HEARTBEAT_OK {summary}"


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

    pointer = ver_mgr.get_pointer(skill_id)
    if pointer:
        target_version = pointer.current_version
    else:
        from collections import Counter
        version_counts = Counter(e.skill_version for e in entries)
        target_version = version_counts.most_common(1)[0][0] if version_counts else "baseline"

    target_entries = [e for e in entries if e.skill_version == target_version]
    if not target_entries:
        return None

    existing = ver_mgr.get_eval_report(skill_id, target_version)
    if existing and existing.segment_count >= len(target_entries):
        return None

    segments = [e for e in target_entries if e.skill_output or e.context_before]
    if not segments:
        logger.info("No segments with content for %s v%s", skill_id, target_version)
        return None

    from .llm_errors import LLMTransientError

    try:
        report = await asyncio.wait_for(
            evaluate_skill(context.llm, skill_id, target_version, segments),
            timeout=_SKILL_EVALUATION_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        raise LLMTransientError(
            f"Request timed out during skill evaluation for {skill_id}"
        ) from exc
    ver_mgr.save_eval_report(skill_id, target_version, report)

    await _post_evaluate(context, ver_mgr, seg_logger, skill_id, target_version, report)

    return (
        f"v{target_version}: {report.segment_count} segments, "
        f"critical={report.critical_issue_rate:.0%}, "
        f"satisfaction={report.mean_satisfaction:.2f}"
    )


async def _post_evaluate(
    context: SkillContext,
    ver_mgr: VersionManager,
    seg_logger: SegmentLogger,
    skill_id: str,
    target_version: str,
    report: EvalReport,
) -> None:
    """Post-evaluation actions: activate healthy testing, rollback+evolve unhealthy."""
    meta = ver_mgr.get_metadata(skill_id, target_version)
    if not meta:
        return

    if meta.status == VersionStatus.TESTING and report.is_healthy:
        ver_mgr.activate(skill_id, target_version)
        try:
            await context.mailbox.deposit(
                summary=f"技能 {skill_id} v{target_version} 验证通过，已生效",
                detail="",
            )
        except Exception:
            pass
        return

    if report.is_healthy:
        return

    pointer = ver_mgr.get_pointer(skill_id)

    if pointer and pointer.consecutive_evolve_count > MAX_CONSECUTIVE_EVOLVE:
        meta.status = VersionStatus.SUSPENDED
        meta.suspended_reason = "consecutive evolve limit exceeded"
        ver_dir = ver_mgr._version_dir(skill_id, target_version)
        (ver_dir / "metadata.json").write_text(meta.to_json(), encoding="utf-8")
        try:
            await context.mailbox.deposit(
                summary=f"技能 {skill_id} 连续改进仍不达标，已暂停",
                detail=f"satisfaction={report.mean_satisfaction:.2f}, critical_rate={report.critical_issue_rate:.0%}",
            )
        except Exception:
            pass
        logger.warning("Suspended %s after %d consecutive evolve attempts", skill_id, pointer.consecutive_evolve_count)
        return

    try:
        ver_mgr.rollback(skill_id, reason="auto-evolve: unhealthy evaluation")
    except ValueError as e:
        logger.warning("Cannot rollback %s: %s", skill_id, e)
        return

    new_version = await _maybe_evolve(context, ver_mgr, seg_logger, skill_id, report)
    if new_version:
        # Re-read pointer since publish() overwrites it
        pointer = ver_mgr.get_pointer(skill_id)
        if pointer:
            pointer.consecutive_evolve_count += 1
            ver_mgr._current_json(skill_id).write_text(pointer.to_json(), encoding="utf-8")

    evolve_msg = f"改进为 v{new_version}，进入验证阶段" if new_version else "自动改进失败，已回退到稳定版本"
    try:
        await context.mailbox.deposit(
            summary=f"技能 {skill_id} 评估不达标，{evolve_msg}",
            detail=f"satisfaction={report.mean_satisfaction:.2f}, critical_rate={report.critical_issue_rate:.0%}",
        )
    except Exception:
        pass


async def _maybe_evolve(
    context: SkillContext,
    ver_mgr: VersionManager,
    seg_logger: SegmentLogger,
    skill_id: str,
    report: EvalReport,
) -> str | None:
    """Generate improved SKILL.md via LLM based on failure cases."""
    # Read the version snapshot (not the live SKILL.md which rollback may have overwritten).
    snapshot = ver_mgr._version_dir(skill_id, report.skill_version) / "skill.md"
    if snapshot.exists():
        current_content = snapshot.read_text(encoding="utf-8")
    else:
        # Fallback to live SKILL.md (e.g. baseline with no snapshot)
        skill_md = ver_mgr._skill_md(skill_id)
        if not skill_md.exists():
            return None
        current_content = skill_md.read_text(encoding="utf-8")

    # Apply the same content filter used during evaluation to align with report.results.
    target_entries = seg_logger.load_by_version(skill_id, report.skill_version)
    segments = [e for e in target_entries if e.skill_output or e.context_before]
    failed: List[tuple[EvaluationSegment, str]] = []
    for entry, result in zip(segments, report.results):
        if result.has_critical_issue or result.satisfaction < 0.5:
            failed.append((entry, result.reason))
    if not failed:
        return None

    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    base = report.skill_version.split("-evolve-")[0]
    new_version = f"{base}-evolve-{ts}"

    failure_block = _build_failure_block(failed)
    prompt = _EVOLVE_PROMPT.format(
        skill_content=current_content,
        failure_block=failure_block,
        new_version=new_version,
    )

    try:
        new_content = await context.llm.complete(prompt, system=_EVOLVE_SYSTEM)
    except Exception as e:
        logger.warning("Evolve LLM call failed for %s: %s", skill_id, e)
        return None

    if not _validate_skill_md(new_content):
        logger.warning("Evolve output for %s failed validation", skill_id)
        return None

    ver_mgr.publish(skill_id, new_version, new_content)
    logger.info("Evolved %s to v%s", skill_id, new_version)
    return new_version


def _build_failure_block(failed: List[tuple[EvaluationSegment, str]]) -> str:
    parts = []
    for i, (seg, reason) in enumerate(failed):
        parts.append(
            f"### Case {i + 1}\n"
            f"**User Input:** {seg.context_before or '(empty)'}\n"
            f"**Skill Output:** {seg.skill_output or '(empty)'}\n"
            f"**User Reaction:** {seg.context_after or '(no reaction recorded)'}\n"
            f"**Judge Assessment:** {reason}"
        )
    return "\n\n".join(parts)


def _validate_skill_md(content: str) -> bool:
    if not content or not content.strip():
        return False
    if not re.search(r"^---\s*\n", content):
        return False
    match = re.search(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return False
    frontmatter = match.group(1)
    if "version:" not in frontmatter:
        return False
    return True
