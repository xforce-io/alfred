"""Host-side authoritative skill catalog for primary compose.

Models ignore the prompt rule "call skill_list when asked what skills you have"
and recite MEMORY / old names. Primary compose always attaches this turn's
discover_skills names so listing answers cannot invent skills outside the list.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Sequence


def format_authoritative_skill_catalog(skills: Sequence[Dict[str, Any]]) -> str:
    """Short block: skill names plus one forbid-outside-list line."""
    lines = [
        "## Installed skills (authoritative this turn)",
        "禁止补充清单外的技能名（例如已卸载的 kweaver、已替换的 paper-discovery）。",
        "",
    ]
    if not skills:
        lines.append("（当前未发现任何技能）")
        return "\n".join(lines)
    for skill in skills:
        name = str(skill.get("name") or "").strip()
        if not name:
            continue
        lines.append(f"- **{name}**")
    return "\n".join(lines)


def append_authoritative_skill_catalog(
    composed: str,
    skills: Iterable[Dict[str, Any]],
) -> str:
    """Append the short catalog after the composed user message."""
    block = format_authoritative_skill_catalog(list(skills))
    base = (composed or "").rstrip()
    if not base:
        return f"{block}\n"
    return f"{base}\n\n{block}\n"


def load_installed_skills(agent_name: str) -> List[Dict[str, Any]]:
    """discover_skills with the same include/exclude as sidecar spawn."""
    from ..agent.provider.milkie.provider import _agent_skill_filter, _resolve_agent_workspace
    from ..agent.provider.milkie.skills import discover_skills

    workspace = _resolve_agent_workspace(agent_name)
    include, exclude = _agent_skill_filter(agent_name)
    return discover_skills(workspace, include=include, exclude=exclude)
