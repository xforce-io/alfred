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
from ..slm._atomic_io import skill_lock

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
    # Layered SLM: writable = agent workspace skills (loader layer 0).
    # Read chain mirrors dolphin's loader priority (workspace → user → repo)
    # so bootstrap can find baseline content even when workspace is empty.
    agent_name = getattr(context, "agent_name", "") or ""
    if agent_name:
        writable = udm.get_agent_writable_skills_dir(agent_name)
        read_dirs = udm.get_agent_read_skill_dirs(agent_name)
    else:
        # Fallback for legacy callers without agent_name in context.
        writable = udm.skills_dir
        read_dirs = [udm.skills_dir]
    ver_mgr = VersionManager(writable, eval_base_dir=skill_eval_dir, read_skill_dirs=read_dirs)

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
    # Self-heal: ensure this skill has pointer+metadata+snapshot before we
    # do anything. Handles both first-time skills and partial state from
    # crash / manual edit.
    from ..slm.state_normalizer import ensure_registered, RegistrationAction
    from ...infra.user_data import get_user_data_manager
    repo_skills = get_user_data_manager().repo_skills_dir
    registration = ensure_registered(ver_mgr, skill_id, repo_skills_dir=repo_skills)
    if registration.action == RegistrationAction.SKILL_MISSING:
        logger.warning("Skipping %s: SKILL.md missing", skill_id)
        return None
    if registration.action == RegistrationAction.CONFLICT_DETECTED:
        logger.warning("Skipping %s: %s", skill_id, registration.detail)
        return f"conflict: {registration.detail}"

    entries = seg_logger.load(skill_id)
    if not entries:
        return None

    pointer = ver_mgr.get_pointer(skill_id)
    # ensure_registered above guarantees pointer exists (unless SKILL_MISSING
    # or CONFLICT_DETECTED already returned early). A missing pointer here
    # would signal a bootstrap bug — let it fail loudly.
    assert pointer is not None, f"ensure_registered did not create pointer for {skill_id}"
    target_version = pointer.current_version

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
    lock_path = ver_mgr._eval_dir(skill_id) / ".lock"
    with skill_lock(lock_path):
        existing = ver_mgr.get_eval_report(skill_id, target_version)
        if existing and existing.segment_count >= len(segments):
            return None

        ver_mgr.save_eval_report(skill_id, target_version, report)

        pointer = ver_mgr.get_pointer(skill_id)
        if pointer is None or pointer.current_version != target_version:
            logger.info(
                "Skipping post-evaluate for stale %s v%s; current is %s",
                skill_id,
                target_version,
                pointer.current_version if pointer else None,
            )
            return None

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
        try:
            await context.mailbox.deposit(
                summary=f"SLM 异常：技能 {skill_id} v{target_version} 缺少 metadata，评估终止",
                detail=(
                    "_post_evaluate found metadata=None after ensure_registered; "
                    "likely concurrent deletion or partial write. Re-run heartbeat "
                    "may self-heal; investigate if it recurs."
                ),
            )
        except Exception:
            pass
        logger.error("SLM abort: %s v%s metadata missing", skill_id, target_version)
        return

    if meta.status == VersionStatus.TESTING and report.is_promotable:
        ver_mgr.activate(skill_id, target_version)
        try:
            await context.mailbox.deposit(
                summary=f"技能 {skill_id} v{target_version} 验证通过，已生效",
                detail=(
                    f"segments={report.segment_count}, "
                    f"satisfaction={report.mean_satisfaction:.2f}, "
                    f"critical_rate={report.critical_issue_rate:.0%}"
                ),
            )
        except Exception:
            pass
        return

    if report.is_healthy:
        # Healthy but not yet promotable (TESTING with too few segments, or
        # ACTIVE which is already at its target state). Stay live, observe.
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
        try:
            await context.mailbox.deposit(
                summary=f"SLM 异常：技能 {skill_id} 回滚失败，无法触发进化",
                detail=str(e),
            )
        except Exception:
            pass
        logger.error("SLM rollback failed for %s: %s", skill_id, e)
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

    new_content = _sanitize_llm_skill_md(new_content)
    if not _validate_skill_md(new_content):
        preview = (new_content or "")[:200].replace("\n", "\\n")
        logger.warning(
            "Evolve output for %s failed validation. Preview: %r", skill_id, preview
        )
        return None

    try:
        ver_mgr.publish(skill_id, new_version, new_content)
    except ValueError as e:
        logger.warning("Cannot publish evolved %s: %s", skill_id, e)
        return None
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


def _sanitize_llm_skill_md(content: str) -> str:
    """Strip common LLM output decorations to recover the raw skill file.

    LLMs frequently ignore "output ONLY the file" instructions and add:
    - Markdown code fences (```markdown / ```yaml / ```)
    - Conversational preamble before the frontmatter
    - Trailing whitespace or fence
    - Leading whitespace before the opening ``---``

    We strip these so _validate_skill_md sees just the file content. If the
    output truly has no recognizable frontmatter, validation will still
    reject it — sanitization only handles decoration, not malformed content.
    """
    if not content:
        return content
    text = content.strip()

    # Strip leading code fence (``` or ```yaml or ```markdown)
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1:].lstrip()

    # Strip trailing code fence
    if text.rstrip().endswith("```"):
        text = text.rstrip()
        text = text[: text.rfind("```")].rstrip()

    # If a preamble pushed the frontmatter further down, jump to it.
    if not text.startswith("---"):
        m = re.search(r"^---\s*$", text, re.MULTILINE)
        if m:
            text = text[m.start():]

    return text


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
