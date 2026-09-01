"""Host-side skill catalog for natural listing questions.

Models ignore the prompt rule "call skill_list when asked what skills you have"
and recite MEMORY / old names. When the user asks that question in ordinary
language, attach this turn's discover_skills list to the composed message.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Sequence

_CATALOG_RE = re.compile(
    r"^\s*(?:你|您)?"
    r"(?:现在|目前|当前)?"
    r"(?:到底)?"
    r"(?:都)?"
    r"(?:有哪些|有什么)"
    r"\s*(?:skills?|技能)\s*[?？]?\s*$"
    r"|^\s*(?:列出|罗列)(?:一下)?(?:你的|全部|所有)?(?:已安装)?(?:的)?"
    r"(?:skills?|技能)\s*[?？]?\s*$"
    r"|^\s*(?:what|which)\s+skills(?:\s+do\s+you\s+have|\s+are\s+installed)?\s*[?]?\s*$"
    r"|^\s*list(?:\s+your)?(?:\s+installed)?\s+skills\s*[?]?\s*$",
    re.IGNORECASE,
)


def is_skill_catalog_query(text: str) -> bool:
    """True when the raw user trigger is a skill-listing question."""
    if not text or not isinstance(text, str):
        return False
    return _CATALOG_RE.match(text.strip()) is not None


def format_authoritative_skill_catalog(skills: Sequence[Dict[str, Any]]) -> str:
    lines = [
        "## Installed skills (authoritative this turn)",
        "回答「有哪些技能」时**只列出下面这些名字**。禁止根据记忆补充清单里没有的技能"
        "（例如已卸载的 kweaver、已替换的 paper-discovery）。",
        "",
    ]
    if not skills:
        lines.append("（当前未发现任何技能）")
        return "\n".join(lines)
    for skill in skills:
        name = str(skill.get("name") or "").strip()
        if not name:
            continue
        desc = str(skill.get("description") or skill.get("title") or "").strip()
        if desc:
            lines.append(f"- **{name}** — {desc}")
        else:
            lines.append(f"- **{name}**")
    return "\n".join(lines)


def append_catalog_if_listing_query(
    trigger: str,
    composed: str,
    skills: Iterable[Dict[str, Any]],
) -> str:
    if not is_skill_catalog_query(trigger):
        return composed
    block = format_authoritative_skill_catalog(list(skills))
    base = (composed or trigger or "").rstrip()
    if not base:
        return block
    return f"{base}\n\n{block}\n"


def load_installed_skills(agent_name: str) -> List[Dict[str, Any]]:
    """discover_skills with the same include/exclude as sidecar spawn."""
    from ..agent.provider.milkie.provider import _agent_skill_filter, _resolve_agent_workspace
    from ..agent.provider.milkie.skills import discover_skills

    workspace = _resolve_agent_workspace(agent_name)
    include, exclude = _agent_skill_filter(agent_name)
    return discover_skills(workspace, include=include, exclude=exclude)
