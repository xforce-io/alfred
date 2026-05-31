"""Shared helpers for LLM-driven memory extractors.

Both ``ProfileExtractor`` and ``EventExtractor`` need the same machinery:
formatting conversation history into a prompt, calling the Dolphin LLM
client, and parsing JSON-shaped responses with multiple fallback
strategies. This module factors that out so the two extractors only
own what is actually different — their prompt and result shape.

These are free functions (not a base class) on purpose. The shared bits
are utility code, not behavior to override; inheritance would couple
two unrelated lifecycle stages just to reuse three small helpers.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def format_messages(messages: List[Dict[str, Any]], max_chars: int = 8000) -> str:
    """Format conversation messages into a prompt-friendly transcript."""
    lines: List[str] = []
    total = 0
    for msg in messages:
        role = "用户" if msg.get("role") == "user" else "助手"
        content = str(msg.get("content", ""))
        if len(content) > 500:
            content = content[:500] + "..."
        line = f"**{role}**: {content}"
        total += len(line)
        if total > max_chars:
            lines.append("... (对话过长，已截断)")
            break
        lines.append(line)
    return "\n\n".join(lines)


def extract_json_object(raw: str) -> Optional[dict]:
    """Best-effort parse of a single JSON object from LLM output.

    Returns the parsed dict, or None if every strategy fails.
    """
    # 1. Direct parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # 2. Inside a markdown code block
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    return None


async def call_dolphin_llm(context: Any, prompt: str, temperature: float = 0.3) -> str:
    """Call the LLM via the active provider with a single user-message prompt.

    Raises ``RuntimeError`` if the provider surfaced an error message.
    """
    from ..agent.provider import get_provider

    return await get_provider().call_llm(
        context, prompt, temperature=temperature, fast=False
    )
