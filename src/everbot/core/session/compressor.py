"""
Session history compressor.

Implements a sliding-window + LLM summary strategy:
- When history_messages exceeds COMPRESS_THRESHOLD, older messages are
  summarized via a fast LLM and injected as a compact summary pair at the
  head of the history, while the most recent WINDOW_SIZE messages are kept
  verbatim.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Summary marker used to identify injected summary messages.
SUMMARY_TAG = "[context_summary]"

# ── Tunables ──────────────────────────────────────────────────────────
COMPRESS_THRESHOLD = 80  # trigger compression when history exceeds this
WINDOW_SIZE = 60         # keep this many recent messages intact

_SUMMARY_PROMPT_TEMPLATE = """\
你是一个对话摘要助手。请将以下对话历史压缩为简洁的摘要，保留关键信息：
- 用户的主要需求和目标
- 重要的决策和结论
- 关键的事实和数据
- 未完成的任务或待跟进的事项

{old_summary_block}
新增对话：
{messages_text}

请输出更新后的完整摘要（中文，不超过500字）："""


class SessionCompressor:
    """Compress old history messages into an LLM-generated summary."""

    def __init__(self, context: Any):
        """
        Args:
            context: A Dolphin ``Context`` object (``agent.executor.context``).
                     Used to obtain cloud/model config and create an LLMClient.
        """
        self._context = context

    # ── Public API ────────────────────────────────────────────────────

    async def maybe_compress(
        self, history_messages: List[Dict[str, Any]]
    ) -> Tuple[bool, List[Dict[str, Any]]]:
        """Compress *history_messages* if they exceed the threshold.

        Returns ``(compressed, new_history)`` where *compressed* indicates
        whether compression was actually performed.  The caller should use
        *new_history* in place of the original list.
        """
        if len(history_messages) <= COMPRESS_THRESHOLD:
            return False, history_messages

        old_summary, remaining = extract_existing_summary(history_messages)

        # Nothing to compress if remaining messages fit in the window.
        if len(remaining) <= WINDOW_SIZE:
            return False, history_messages

        to_compress = remaining[:-WINDOW_SIZE]
        to_keep = remaining[-WINDOW_SIZE:]

        if not to_compress:
            return False, history_messages

        try:
            new_summary = await self._generate_summary(old_summary, to_compress)
        except Exception:
            logger.exception("Failed to generate session summary; skipping compression")
            return False, history_messages

        if not new_summary:
            return False, history_messages

        logger.info(
            "Compressed %d messages into summary (%d chars), keeping %d recent messages",
            len(to_compress),
            len(new_summary),
            len(to_keep),
        )
        return True, inject_summary(new_summary, to_keep)

    # ── LLM interaction ──────────────────────────────────────────────

    async def _generate_summary(
        self, old_summary: str, messages: List[Dict[str, Any]]
    ) -> str:
        from dolphin.core.llm.llm_client import LLMClient
        from dolphin.core.common.enums import Messages as DolphinMessages, MessageRole

        messages_text = _format_messages_for_prompt(messages)
        if old_summary:
            old_summary_block = f"之前的摘要：\n{old_summary}\n"
        else:
            old_summary_block = ""

        prompt = _SUMMARY_PROMPT_TEMPLATE.format(
            old_summary_block=old_summary_block,
            messages_text=messages_text,
        )

        llm_client = LLMClient(self._context)
        msgs = DolphinMessages()
        msgs.append_message(MessageRole.USER, prompt)

        config = self._context.get_config()
        model = getattr(config, "fast_llm", None) or "qwen-turbo"

        result = ""
        async for chunk in llm_client.mf_chat_stream(
            messages=msgs,
            model=model,
            temperature=0.3,
            no_cache=True,
        ):
            result = chunk.get("content") or ""

        return result.strip()


# ── Pure helpers (no LLM, no I/O) ────────────────────────────────────


def extract_existing_summary(
    messages: List[Dict[str, Any]],
) -> Tuple[str, List[Dict[str, Any]]]:
    """Extract a previously injected summary from the head of *messages*.

    Returns ``(summary_text, remaining_messages)``.  If no summary is
    present, *summary_text* is the empty string and *remaining_messages*
    is the original list.
    """
    if (
        len(messages) >= 2
        and messages[0].get("role") == "user"
        and isinstance(messages[0].get("content"), str)
        and SUMMARY_TAG in messages[0]["content"]
        and messages[1].get("role") == "assistant"
    ):
        summary_text = messages[1].get("content") or ""
        return summary_text, messages[2:]
    return "", list(messages)


def inject_summary(
    summary: str, messages: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Prepend a summary user+assistant message pair to *messages*."""
    user_msg: Dict[str, Any] = {
        "role": "user",
        "content": f"{SUMMARY_TAG}\n请回顾以下之前对话的摘要，以便继续对话。",
    }
    assistant_msg: Dict[str, Any] = {
        "role": "assistant",
        "content": summary,
    }
    return [user_msg, assistant_msg] + list(messages)


def is_summary_message(msg: Dict[str, Any]) -> bool:
    """Return True if *msg* is a summary-injected user message."""
    return (
        msg.get("role") == "user"
        and isinstance(msg.get("content"), str)
        and SUMMARY_TAG in msg["content"]
    )


def _format_messages_for_prompt(
    messages: List[Dict[str, Any]], max_chars: int = 12000
) -> str:
    """Format message dicts into a readable text block for the summary prompt."""
    lines: list[str] = []
    total = 0
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content") or ""
        if not isinstance(content, str):
            continue
        # Skip tool messages — they add noise to the summary.
        if role == "tool":
            continue
        label = {"user": "用户", "assistant": "助手"}.get(role, role)
        line = f"{label}: {content}"
        if total + len(line) > max_chars:
            lines.append("...(部分对话已省略)")
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)
