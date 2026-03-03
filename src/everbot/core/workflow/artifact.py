"""Artifact extraction and injection between phases."""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_MAX_ARTIFACT_CHARS = 8000
_MAX_FALLBACK_CHARS = 4000


def extract_artifact(
    phase_name: str,
    llm_output: str,
    last_assistant_text: str,
) -> str:
    """Extract artifact from phase LLM output.

    Looks for ``<phase_artifact>...</phase_artifact>`` tag.
    Falls back to truncated last assistant text with WARNING log.
    """
    if not llm_output and not last_assistant_text:
        return ""

    search_text = llm_output or last_assistant_text
    match = re.search(
        r"<phase_artifact>(.*?)</phase_artifact>", search_text, re.DOTALL
    )
    if match:
        return match.group(1).strip()

    # Fallback
    logger.warning(
        "workflow.artifact.tag_missing",
        extra={"phase": phase_name, "fallback": "last_assistant_text"},
    )
    return _truncate(last_assistant_text, _MAX_FALLBACK_CHARS)


def build_artifact_injection(
    artifacts: Dict[str, str],
    input_artifacts: List[str],
) -> str:
    """Build artifact injection text for phase context.

    Formats referenced artifacts as Markdown sections, each truncated
    to ``_MAX_ARTIFACT_CHARS`` characters.
    """
    if not input_artifacts:
        return ""

    parts: List[str] = []
    for name in input_artifacts:
        content = artifacts.get(name, "")
        if not content:
            logger.warning(
                "workflow.artifact.empty_reference",
                extra={"artifact_name": name},
            )
            continue
        truncated = _truncate(content, _MAX_ARTIFACT_CHARS)
        parts.append(f"## {name} 阶段产出\n\n{truncated}")

    return "\n\n".join(parts)


def _truncate(text: str, max_chars: int) -> str:
    """Truncate text to max_chars with an indicator."""
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... [truncated, total {len(text)} chars]"
