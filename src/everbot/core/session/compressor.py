"""
Session history compressor.

Implements a sliding-window + LLM summary strategy:
- When history_messages exceeds COMPRESS_THRESHOLD, older messages are
  summarized via a fast LLM and injected as a compact summary pair at the
  head of the history, while the most recent WINDOW_SIZE messages are kept
  verbatim.
- Token-budget path uses a tool-chain-safe window cut (issue #166).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

from ..models.constants import SUMMARY_TAG  # noqa: F401 — re-exported

# Re-export pure helpers used by policy/tests (single implementation source).
from .history_compaction import (  # noqa: F401
    find_safe_window_start,
    looks_like_summary_error,
    safe_window_trim,
    truncate_summary,
    validate_tool_pairing,
)

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

    def __init__(self, context: Any = None, *, max_summary_tokens: int = 2000):
        """
        Args:
            context: Optional opaque context for oneshot LLM (unused by
                     OneshotLLMProvider but kept for call-site compatibility).
            max_summary_tokens: Cap on generated summary body (tokens ≈ chars/3).
        """
        self._context = context
        self._max_summary_tokens = max_summary_tokens

    # ── Public API ────────────────────────────────────────────────────

    async def maybe_compress(
        self,
        history_messages: List[Dict[str, Any]],
        token_budget: Optional[int] = None,
    ) -> Tuple[bool, List[Dict[str, Any]]]:
        """Compress *history_messages* if they exceed the threshold.

        When *token_budget* is provided, the window is computed by token
        count with a **tool-chain-safe** cut point instead of the fixed
        ``WINDOW_SIZE`` message count. Compression is always attempted when
        *token_budget* is given (the caller already checked the trigger).

        Returns ``(compressed, new_history)`` where *compressed* indicates
        whether compression was actually performed.  The caller should use
        *new_history* in place of the original list.
        """
        if token_budget is None:
            # Legacy path: count-based threshold
            if len(history_messages) <= COMPRESS_THRESHOLD:
                return False, history_messages

        old_summary, remaining = extract_existing_summary(history_messages)

        if token_budget is not None:
            window_start = find_safe_window_start(remaining, token_budget)
            if window_start >= len(remaining) and remaining:
                window_start = len(remaining) - 1
            to_compress = remaining[:window_start]
            to_keep = remaining[window_start:]
        else:
            # Legacy path: fixed message-count window (still expand for tools)
            if len(remaining) <= WINDOW_SIZE:
                return False, history_messages
            window_start = len(remaining) - WINDOW_SIZE
            window_start = _expand_legacy_window_start(remaining, window_start)
            to_compress = remaining[:window_start]
            to_keep = remaining[window_start:]

        if not to_compress:
            return False, history_messages

        try:
            new_summary = await self._generate_summary(old_summary, to_compress)
        except Exception:
            logger.exception("Failed to generate session summary; skipping compression")
            return False, history_messages

        if not new_summary or looks_like_summary_error(new_summary):
            return False, history_messages

        new_summary = truncate_summary(new_summary, self._max_summary_tokens)
        result = inject_summary(new_summary, to_keep)
        if validate_tool_pairing(result):
            logger.warning(
                "Compressed history failed tool pairing validation; skipping compression"
            )
            return False, history_messages

        logger.info(
            "Compressed %d messages into summary (%d chars), keeping %d recent messages",
            len(to_compress),
            len(new_summary),
            len(to_keep),
        )
        return True, result

    # ── High-level entry point ───────────────────────────────────────

    async def compress_history(
        self, history: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Token-budget compress with heartbeat isolation.

        Separates chat from heartbeat/placeholder, compresses only the chat
        portion when it exceeds COMPACT_TOKEN_BUDGET, and re-appends heartbeat
        messages to the tail.  Returns the (possibly compressed) history.

        This is the single entry point used by both save paths
        (session.py and persistence.py) to avoid logic duplication.
        Prefer :class:`HistoryCompactionPolicy` for pre-turn orchestration.
        """
        from .history_utils import (
            _is_heartbeat,
            _is_placeholder,
            _estimate_tokens,
            COMPACT_TOKEN_BUDGET,
            COMPACT_WINDOW_TOKENS,
        )

        chat_msgs = [
            m for m in history
            if not _is_heartbeat(m) and not _is_placeholder(m)
        ]
        if _estimate_tokens(chat_msgs) <= COMPACT_TOKEN_BUDGET:
            return history

        heartbeat_msgs = [
            m for m in history
            if _is_heartbeat(m) or _is_placeholder(m)
        ]
        compressed, new_chat = await self.maybe_compress(
            chat_msgs, token_budget=COMPACT_WINDOW_TOKENS,
        )
        if compressed:
            return new_chat + heartbeat_msgs
        return history

    # ── LLM interaction ──────────────────────────────────────────────

    async def _generate_summary(
        self, old_summary: str, messages: List[Dict[str, Any]]
    ) -> str:
        """Summarize *messages*, covering the full compress region.

        Large regions are map-reduced in chunks so late constraints and tool
        conclusions are not dropped by a prefix-only char cap or a hard map
        chunk limit (issue #166 F-004). Every chunk is mapped; partials are
        merged with a hierarchical reduce so reduce prompts stay bounded.
        """
        chunks = chunk_messages_for_summary(
            messages, max_chars_per_chunk=_SUMMARY_CHUNK_CHARS
        )
        if len(chunks) == 1:
            return await self._call_summary_llm(old_summary, chunks[0])

        # Map: one bounded summary per chunk — full head→tail coverage.
        # Fail-closed (issue #166 F-005): any empty/error map result aborts the
        # entire summary so callers fall back to safe window / kept_original
        # instead of committing a partial summary that silently drops facts.
        partials: List[str] = []
        for idx, chunk in enumerate(chunks):
            # Feed prior summary only into the first map call; later chunks stand alone.
            seed = old_summary if idx == 0 else ""
            partial = await self._call_summary_llm(seed, chunk)
            if not partial or looks_like_summary_error(partial):
                raise RuntimeError(
                    f"Map summary failed for chunk {idx + 1}/{len(chunks)} "
                    "(empty or error-like output); aborting partial summary"
                )
            partials.append(partial.strip())

        # Hierarchical reduce: merge partials in bounded fan-in batches until one.
        return await self._reduce_partial_summaries(old_summary, partials)

    async def _reduce_partial_summaries(
        self, old_summary: str, partials: List[str]
    ) -> str:
        """Merge map partials with hierarchical reduce (bounded fan-in).

        Fail-closed (issue #166 F-005): any empty/error reduce output aborts
        the entire summary so callers keep original history or safe-window trim
        rather than submitting an incomplete merged summary.
        """
        level = list(partials)
        if not level:
            raise RuntimeError("No usable partial summaries for reduce")
        if len(level) == 1:
            return level[0]

        seed = old_summary
        while len(level) > 1:
            next_level: List[str] = []
            for batch_start in range(0, len(level), _SUMMARY_REDUCE_FAN_IN):
                batch = level[batch_start : batch_start + _SUMMARY_REDUCE_FAN_IN]
                if len(batch) == 1:
                    next_level.append(batch[0])
                    continue
                reduce_msgs: List[Dict[str, Any]] = [
                    {
                        "role": "assistant",
                        "content": (
                            f"[分段摘要{batch_start + i + 1}/"
                            f"{len(level)}] {text}"
                        ),
                    }
                    for i, text in enumerate(batch)
                ]
                # Only the first reduce batch absorbs the prior session summary.
                batch_seed = seed if batch_start == 0 else ""
                merged = await self._call_summary_llm(batch_seed, reduce_msgs)
                if not merged or looks_like_summary_error(merged):
                    raise RuntimeError(
                        f"Reduce summary failed for batch starting at "
                        f"{batch_start} (empty or error-like output); "
                        "aborting partial summary"
                    )
                next_level.append(merged.strip())
            # After the first level, old_summary is already folded in.
            seed = ""
            level = next_level

        return level[0]

    async def _call_summary_llm(
        self, old_summary: str, messages: List[Dict[str, Any]]
    ) -> str:
        messages_text = _format_messages_for_prompt(messages)
        if old_summary:
            old_summary_block = f"之前的摘要：\n{old_summary}\n"
        else:
            old_summary_block = ""

        prompt = _SUMMARY_PROMPT_TEMPLATE.format(
            old_summary_block=old_summary_block,
            messages_text=messages_text,
        )

        from ..agent.provider import oneshot_llm_provider

        # raise_on_error=True so error strings are not silently stored as
        # summary text; callers catch and degrade to safe window / keep original.
        return await oneshot_llm_provider().call_llm(
            self._context, prompt, temperature=0.3, fast=True, raise_on_error=True
        )


def _expand_legacy_window_start(
    messages: List[Dict[str, Any]], start: int
) -> int:
    """Expand a count-based window start so tool pairs stay intact."""
    from .history_compaction import _expand_to_safe_boundary

    return _expand_to_safe_boundary(messages, start)


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


# Per-chunk budget for map-reduce summary; keeps each oneshot prompt bounded
# while still covering the full compressed region via multiple map calls.
# Map stage processes *all* chunks (no hard map cap — issue #166 F-004).
# Hierarchical reduce merges at most _SUMMARY_REDUCE_FAN_IN partials per call.
_SUMMARY_CHUNK_CHARS = 10_000
_SUMMARY_REDUCE_FAN_IN = 8
_TOOL_BODY_MAX = 800
_TOOL_ARGS_MAX = 400


def _message_to_summary_line(msg: Dict[str, Any]) -> Optional[str]:
    """One human-readable line for the summary prompt (includes tool facts)."""
    role = msg.get("role", "unknown")
    content = msg.get("content")

    if role == "tool":
        tcid = msg.get("tool_call_id") or ""
        body = content if isinstance(content, str) else ""
        if len(body) > _TOOL_BODY_MAX:
            body = body[:_TOOL_BODY_MAX] + "…"
        return f"工具结果({tcid}): {body}"

    if role == "assistant":
        parts: list[str] = []
        if isinstance(content, str) and content:
            parts.append(content)
        for tc in msg.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            if not isinstance(fn, dict):
                fn = {}
            name = fn.get("name") or ""
            args = fn.get("arguments") or ""
            if not isinstance(args, str):
                args = str(args)
            if len(args) > _TOOL_ARGS_MAX:
                args = args[:_TOOL_ARGS_MAX] + "…"
            parts.append(f"[调用工具 {name}({args})]")
        if not parts:
            return None
        return f"助手: {' '.join(parts)}"

    if role == "user":
        if not isinstance(content, str) or not content:
            return None
        return f"用户: {content}"

    if isinstance(content, str) and content:
        return f"{role}: {content}"
    return None


def _pack_lines_with_coverage(lines: List[str], max_chars: int) -> str:
    """Fill *max_chars* using head + mid samples + tail (never prefix-only).

    Late messages (constraints / tool conclusions near the compress cut) get a
    larger share of the budget than early bulk noise.
    """
    n = len(lines)
    if n == 0 or max_chars <= 0:
        return ""

    # Budget split: head 25%, mid 30%, tail 40% (+ markers).
    head_budget = max(200, max_chars // 4)
    tail_budget = max(400, (max_chars * 2) // 5)
    mid_budget = max(200, max_chars - head_budget - tail_budget - 100)

    selected: List[Tuple[int, str]] = []
    used: set[int] = set()

    total = 0
    for i, line in enumerate(lines):
        cost = len(line) + 1
        if total + cost > head_budget:
            break
        selected.append((i, line))
        used.add(i)
        total += cost

    total = 0
    for i in range(n - 1, -1, -1):
        if i in used:
            continue
        line = lines[i]
        cost = len(line) + 1
        if total + cost > tail_budget:
            break
        selected.append((i, line))
        used.add(i)
        total += cost

    remaining = [i for i in range(n) if i not in used]
    if remaining and mid_budget > 0:
        # Evenly sample remaining indices so mid-region facts still appear.
        approx_lines = max(1, mid_budget // 120)
        step = max(1, len(remaining) // approx_lines)
        mid_total = 0
        for k in range(0, len(remaining), step):
            i = remaining[k]
            line = lines[i]
            cost = len(line) + 1
            if mid_total + cost > mid_budget:
                break
            selected.append((i, line))
            mid_total += cost

    selected.sort(key=lambda item: item[0])
    out: list[str] = []
    prev = -1
    for i, line in selected:
        if prev >= 0 and i > prev + 1:
            out.append("...(部分对话已省略)")
        out.append(line)
        prev = i
    text = "\n".join(out)
    if len(text) > max_chars:
        return text[: max_chars - 1].rstrip() + "…"
    return text


def _format_messages_for_prompt(
    messages: List[Dict[str, Any]], max_chars: int = 12000
) -> str:
    """Format message dicts for the summary prompt.

    Includes user/assistant/tool content so confirmed constraints and tool
    conclusions survive. When the transcript exceeds *max_chars*, uses coverage
    packing (head/mid/tail) instead of truncating only the earliest prefix.
    """
    lines: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        line = _message_to_summary_line(msg)
        if line is not None:
            lines.append(line)

    if not lines:
        return ""

    full = "\n".join(lines)
    if len(full) <= max_chars:
        return full
    return _pack_lines_with_coverage(lines, max_chars)


def chunk_messages_for_summary(
    messages: List[Dict[str, Any]],
    *,
    max_chars_per_chunk: int = _SUMMARY_CHUNK_CHARS,
) -> List[List[Dict[str, Any]]]:
    """Split *messages* into contiguous chunks by formatted size.

    Each chunk stays within *max_chars_per_chunk* (single oversized message
    becomes its own chunk). Used by map-reduce summary so the whole compress
    region is covered, not only the first 12k characters.
    """
    if not messages:
        return [[]]
    if max_chars_per_chunk <= 0:
        return [list(messages)]

    chunks: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    current_chars = 0

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        line = _message_to_summary_line(msg) or ""
        cost = len(line) + 1
        if current and current_chars + cost > max_chars_per_chunk:
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(msg)
        current_chars += cost

    if current:
        chunks.append(current)
    return chunks or [[]]
