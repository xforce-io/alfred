"""Detect and notify LLM about resource skill changes between turns.

Compares the agent's current ResourceSkillkit manifest against what the
session last observed, and injects a system notification into history when
skills are added or removed.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Set

from ...infra.dolphin_compat import KEY_HISTORY

logger = logging.getLogger(__name__)

SESSION_VAR_KNOWN_SKILLS = "_known_resource_skills"


def get_current_resource_skills(agent: Any) -> Dict[str, str]:
    """Extract current resource skill names and descriptions from the agent.

    Returns:
        Mapping of skill_name → description (may be empty if ResourceSkillkit not loaded).
    """
    global_skills = getattr(agent, "global_skills", None)
    if global_skills is None:
        return {}

    installed = getattr(global_skills, "installedToolSet", None)
    if installed is None:
        return {}

    rsk = None
    for skill in installed.getSkills():
        owner = getattr(skill, "owner_skillkit", None)
        if owner is not None and getattr(owner, "getName", lambda: "")() == "resource_skillkit":
            rsk = owner
            break

    if rsk is None:
        return {}

    result: Dict[str, str] = {}
    for name in rsk.get_available_skills():
        meta = rsk.get_skill_meta(name)
        desc = (getattr(meta, "description", "") or "") if meta else ""
        result[name] = desc
    return result


def inject_skill_updates_if_needed(
    agent: Any,
    session_id: str,
    session_data: Any,
) -> None:
    """Compare current resource skills with what the session last saw.

    If skills were added or removed, inject a system message into history
    so the LLM learns about the change without modifying the system prompt
    (preserving prefix cache).
    """
    current_skills = get_current_resource_skills(agent)
    if not current_skills:
        return

    current_names = set(current_skills.keys())

    # Read previously known skill set from session variables
    prev_names: Set[str] = set()
    has_prev = False
    if session_data and session_data.variables:
        stored = session_data.variables.get(SESSION_VAR_KNOWN_SKILLS)
        if isinstance(stored, list):
            prev_names = set(stored)
            has_prev = True

    if current_names == prev_names and has_prev:
        return

    # First turn (no previous record): just persist the baseline
    if not has_prev:
        ctx = agent.executor.context
        ctx.set_variable(SESSION_VAR_KNOWN_SKILLS, sorted(current_names))
        return

    added = sorted(current_names - prev_names)
    removed = sorted(prev_names - current_names)

    if not added and not removed:
        return

    # Build notification message
    parts: List[str] = ["[系统通知] 可用 Resource Skills 已更新。"]
    if added:
        lines = [f"  - **{n}**: {current_skills[n][:80]}" for n in added]
        parts.append("新增技能:\n" + "\n".join(lines))
    if removed:
        parts.append("已移除技能: " + ", ".join(f"`{n}`" for n in removed))
    parts.append(
        "当前完整列表: " + ", ".join(f"`{n}`" for n in sorted(current_names))
    )
    notification = "\n".join(parts)

    # Inject into agent history
    ctx = agent.executor.context
    history = ctx.get_var_value(KEY_HISTORY)
    if not isinstance(history, list):
        history = []
    history.append({"role": "user", "content": notification})
    ctx.set_variable(KEY_HISTORY, history)

    # Persist updated skill set for next comparison
    ctx.set_variable(SESSION_VAR_KNOWN_SKILLS, sorted(current_names))

    logger.info(
        "Skill update injected for session %s: +%s -%s",
        session_id,
        added or "none",
        removed or "none",
    )
