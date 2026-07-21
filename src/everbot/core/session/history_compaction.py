"""Long-session history compaction policy (issue #166).

Unified entry for pre-turn and save-path compaction:
- token budget gate (trigger / target)
- LLM summary + tool-chain-safe recent window (via SessionCompressor helpers)
- safe window trim fallback when summary fails
- never silently drop messages that cannot be reduced safely
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple, Union

from .history_utils import (
    COMPACT_TOKEN_BUDGET,
    COMPACT_WINDOW_TOKENS,
    _estimate_tokens,
    _is_heartbeat,
    _is_placeholder,
)
logger = logging.getLogger(__name__)

# Default max summary budget (tokens); chars ≈ tokens * 3.
DEFAULT_MAX_SUMMARY_TOKENS = 2_000

SummarizeFn = Callable[[str, List[Dict[str, Any]]], Awaitable[str]]


@dataclass(frozen=True)
class HistoryCompactionConfig:
    """Resolved compaction settings (agent > global > default)."""

    enabled: bool = True
    trigger_tokens: int = COMPACT_TOKEN_BUDGET
    target_recent_tokens: int = COMPACT_WINDOW_TOKENS
    max_summary_tokens: int = DEFAULT_MAX_SUMMARY_TOKENS


@dataclass
class CompactionResult:
    """Outcome of :meth:`HistoryCompactionPolicy.ensure_within_budget`."""

    history: List[Dict[str, Any]]
    changed: bool
    outcome: str
    reason: str
    before_tokens: int
    after_tokens: int
    summary_tokens: int
    retained_messages: int

    def to_event_payload(self, *, provider: str = "", session_id: str = "") -> Dict[str, Any]:
        """Structured timeline/log fields (no history body)."""
        payload: Dict[str, Any] = {
            "type": "history_compaction",
            "provider": provider,
            "reason": self.reason,
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
            "summary_tokens": self.summary_tokens,
            "retained_messages": self.retained_messages,
            "outcome": self.outcome,
        }
        if session_id:
            payload["session_id"] = session_id
        return payload


def resolve_history_compaction_config(
    config: Optional[Dict[str, Any]] = None,
    agent_name: Optional[str] = None,
) -> HistoryCompactionConfig:
    """Resolve config with priority: agent > global > default.

    Paths:
      - ``everbot.agents.<agent_name>.session.history_compaction.*``
      - ``everbot.session.history_compaction.*``
    Invalid values log a warning and fall back to defaults (never raise).
    """
    defaults = HistoryCompactionConfig()
    if not isinstance(config, dict):
        return defaults

    everbot = config.get("everbot", {})
    if not isinstance(everbot, dict):
        return defaults

    global_raw = (everbot.get("session") or {}).get("history_compaction") or {}
    if not isinstance(global_raw, dict):
        global_raw = {}

    agent_raw: Dict[str, Any] = {}
    if agent_name:
        agent_cfg = (everbot.get("agents") or {}).get(agent_name) or {}
        if isinstance(agent_cfg, dict):
            session_cfg = agent_cfg.get("session") or {}
            if isinstance(session_cfg, dict):
                raw = session_cfg.get("history_compaction") or {}
                if isinstance(raw, dict):
                    agent_raw = raw

    merged: Dict[str, Any] = {**global_raw, **agent_raw}
    return _sanitize_config(merged, defaults)


def _sanitize_config(
    raw: Dict[str, Any], defaults: HistoryCompactionConfig
) -> HistoryCompactionConfig:
    enabled = defaults.enabled
    if "enabled" in raw:
        val = raw["enabled"]
        if isinstance(val, bool):
            enabled = val
        else:
            logger.warning(
                "Invalid history_compaction.enabled=%r; using default %s",
                val,
                defaults.enabled,
            )

    trigger = defaults.trigger_tokens
    if "trigger_tokens" in raw:
        try:
            t = int(raw["trigger_tokens"])
            if t >= 1000:
                trigger = t
            else:
                logger.warning(
                    "Invalid history_compaction.trigger_tokens=%r (<1000); using default",
                    raw["trigger_tokens"],
                )
        except (TypeError, ValueError):
            logger.warning(
                "Invalid history_compaction.trigger_tokens=%r; using default",
                raw["trigger_tokens"],
            )

    target = defaults.target_recent_tokens
    if "target_recent_tokens" in raw:
        try:
            t = int(raw["target_recent_tokens"])
            if t >= 500:
                target = t
            else:
                logger.warning(
                    "Invalid history_compaction.target_recent_tokens=%r (<500); using default",
                    raw["target_recent_tokens"],
                )
        except (TypeError, ValueError):
            logger.warning(
                "Invalid history_compaction.target_recent_tokens=%r; using default",
                raw["target_recent_tokens"],
            )

    if target > trigger:
        logger.warning(
            "history_compaction.target_recent_tokens (%s) > trigger_tokens (%s); "
            "clamping target to trigger",
            target,
            trigger,
        )
        target = trigger

    max_summary = defaults.max_summary_tokens
    if "max_summary_tokens" in raw:
        try:
            t = int(raw["max_summary_tokens"])
            if 200 <= t <= 8000:
                max_summary = t
            else:
                logger.warning(
                    "Invalid history_compaction.max_summary_tokens=%r; using default",
                    raw["max_summary_tokens"],
                )
        except (TypeError, ValueError):
            logger.warning(
                "Invalid history_compaction.max_summary_tokens=%r; using default",
                raw["max_summary_tokens"],
            )

    return HistoryCompactionConfig(
        enabled=enabled,
        trigger_tokens=trigger,
        target_recent_tokens=target,
        max_summary_tokens=max_summary,
    )


# ── Tool-chain safety ────────────────────────────────────────────────


def _tool_call_ids(msg: Dict[str, Any]) -> List[str]:
    ids: List[str] = []
    for tc in msg.get("tool_calls") or []:
        if isinstance(tc, dict) and tc.get("id"):
            ids.append(str(tc["id"]))
    return ids


def validate_tool_pairing(messages: Sequence[Dict[str, Any]]) -> List[str]:
    """Return error codes for illegal tool sequences; empty list means valid.

    - ``orphan_tool:<id>`` — tool result without a prior assistant tool_call in window
    - ``unmatched_tool_call:<id>`` — tool_call closed by a later non-tool message without result

    Unfinished tool chains at the **end** of the list are allowed (open pending).
    """
    errors: List[str] = []
    pending: Dict[str, int] = {}

    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "assistant":
            if pending:
                for tcid in list(pending):
                    errors.append(f"unmatched_tool_call:{tcid}")
                pending.clear()
            for tcid in _tool_call_ids(msg):
                pending[tcid] = i
        elif role == "tool":
            tcid = msg.get("tool_call_id")
            if not tcid or str(tcid) not in pending:
                errors.append(f"orphan_tool:{tcid or 'missing'}")
            else:
                del pending[str(tcid)]
        elif role == "user":
            if pending:
                for tcid in list(pending):
                    errors.append(f"unmatched_tool_call:{tcid}")
                pending.clear()
        # other roles ignored
    return errors


def _find_assistant_for_tool(
    messages: Sequence[Dict[str, Any]], tool_idx: int
) -> Optional[int]:
    """Locate the assistant message that owns ``messages[tool_idx]`` tool_call_id."""
    tcid = messages[tool_idx].get("tool_call_id")
    if not tcid:
        return None
    for j in range(tool_idx - 1, -1, -1):
        if messages[j].get("role") == "assistant" and str(tcid) in _tool_call_ids(
            messages[j]
        ):
            return j
    return None


def find_safe_window_start(
    messages: Sequence[Dict[str, Any]], token_budget: int
) -> int:
    """Return inclusive start index of a tool-safe recent window.

    Accumulates tokens from the tail up to *token_budget*, then expands the
    cut point so the retained slice has no orphan tool messages. Always keeps
    at least the last message when the list is non-empty.
    """
    n = len(messages)
    if n == 0:
        return 0

    window_start = n
    accumulated = 0
    for j in range(n - 1, -1, -1):
        msg_tokens = _estimate_tokens([messages[j]])
        if accumulated > 0 and accumulated + msg_tokens > token_budget:
            break
        accumulated += msg_tokens
        window_start = j
        if accumulated >= token_budget and j < n - 1:
            # filled budget; keep what we have (may be over if single msg is huge)
            if accumulated > token_budget and window_start < n - 1:
                # last added blew budget and we already had messages — drop it
                window_start = j + 1
                break
            break

    if window_start >= n:
        window_start = n - 1

    # Prefer keeping the latest user turn with its assistant/tool chain
    # (design: at least the newest user message + related tool chain).
    if window_start > 0 and messages[window_start].get("role") != "user":
        for j in range(window_start - 1, -1, -1):
            if messages[j].get("role") == "user":
                window_start = j
                break

    return _expand_to_safe_boundary(messages, window_start)


def _expand_to_safe_boundary(
    messages: Sequence[Dict[str, Any]], start: int
) -> int:
    """Expand *start* leftward until ``messages[start:]`` has valid tool pairing."""
    if start <= 0:
        return 0

    # If cut lands on tool, jump to owning assistant.
    if messages[start].get("role") == "tool":
        asst = _find_assistant_for_tool(messages, start)
        if asst is not None:
            start = asst
        else:
            start = max(0, start - 1)

    # Expand until validation passes or we hit 0.
    while start > 0:
        errs = validate_tool_pairing(messages[start:])
        if not errs:
            return start
        # Prefer jumping to assistant for leading orphan tool.
        if messages[start].get("role") == "tool":
            asst = _find_assistant_for_tool(messages, start)
            if asst is not None and asst < start:
                start = asst
                continue
        start -= 1
    return 0


def safe_window_trim(
    messages: List[Dict[str, Any]], token_budget: int
) -> Tuple[List[Dict[str, Any]], bool]:
    """Trim to a tool-safe recent window without injecting a summary.

    Returns ``(trimmed, reduced)`` where *reduced* is True only when the
    result is strictly shorter than the input and tool pairing is valid.
    Never returns a structurally invalid sequence; on failure returns the
    original list with ``reduced=False``.
    """
    if not messages:
        return messages, False

    start = find_safe_window_start(messages, token_budget)
    if start <= 0:
        # Cannot drop anything without starting at 0.
        # Still may be over budget (single huge message / full chain).
        trimmed = list(messages)
        if validate_tool_pairing(trimmed):
            return messages, False
        # start==0 means full list kept
        return messages, False

    trimmed = list(messages[start:])
    if validate_tool_pairing(trimmed):
        return messages, False
    if len(trimmed) >= len(messages):
        return messages, False
    return trimmed, True


def looks_like_summary_error(text: str) -> bool:
    """Heuristic: oneshot LLM error strings must not become history summaries."""
    if not text or not str(text).strip():
        return True
    t = str(text).strip()
    low = t.lower()
    if t.startswith("Error") or t.startswith("oneshot LLM") or t.startswith("LLM call failed"):
        return True
    if "traceback" in low or "runtimeerror" in low:
        return True
    if low.startswith("http ") and ("error" in low or "failed" in low):
        return True
    if "llm call failed" in low or "oneshot llm" in low:
        return True
    return False


def truncate_summary(text: str, max_summary_tokens: int) -> str:
    """Bound summary body by char budget ``max_summary_tokens * 3``."""
    if max_summary_tokens <= 0:
        return text
    max_chars = max_summary_tokens * 3
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def split_chat_and_heartbeat(
    history: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Separate chat messages from heartbeat/placeholder (appended at tail later)."""
    chat = [
        m
        for m in history
        if isinstance(m, dict) and not _is_heartbeat(m) and not _is_placeholder(m)
    ]
    heartbeat = [
        m
        for m in history
        if isinstance(m, dict) and (_is_heartbeat(m) or _is_placeholder(m))
    ]
    return chat, heartbeat


class HistoryCompactionPolicy:
    """Orchestrate threshold check, summary compress, and safe-window fallback."""

    async def ensure_within_budget(
        self,
        history: List[Dict[str, Any]],
        config: HistoryCompactionConfig,
        *,
        summarize: Optional[Union[SummarizeFn, Any]] = None,
    ) -> CompactionResult:
        """Compress *history* when over trigger budget.

        *summarize* may be an async ``(old_summary, messages) -> str`` callable
        or a :class:`SessionCompressor` instance. Required only when compression
        is attempted; under-threshold / disabled paths do not call it.
        """
        original = list(history) if history else []
        chat, heartbeat = split_chat_and_heartbeat(original)
        before = _estimate_tokens(chat)

        def _result(
            hist: List[Dict[str, Any]],
            *,
            changed: bool,
            outcome: str,
            reason: str,
            summary_tokens: int = 0,
        ) -> CompactionResult:
            after_chat, _ = split_chat_and_heartbeat(hist)
            return CompactionResult(
                history=hist,
                changed=changed,
                outcome=outcome,
                reason=reason,
                before_tokens=before,
                after_tokens=_estimate_tokens(after_chat),
                summary_tokens=summary_tokens,
                retained_messages=len(after_chat),
            )

        if not config.enabled:
            return _result(original, changed=False, outcome="skipped", reason="disabled")

        if before <= config.trigger_tokens:
            return _result(original, changed=False, outcome="skipped", reason="under_trigger")

        # --- Attempt summary + safe window via compressor helpers ---
        summarized: Optional[List[Dict[str, Any]]] = None
        summary_tokens = 0
        summary_error: Optional[str] = None

        if summarize is not None:
            try:
                summarized, summary_tokens = await self._try_summarize(
                    chat, config, summarize
                )
            except Exception as exc:
                logger.exception("History summary failed; will try safe window trim")
                summary_error = str(exc)[:200]
                summarized = None

        if summarized is not None:
            merged = summarized + heartbeat
            if (
                not validate_tool_pairing(summarized)
                and _estimate_tokens(summarized) < before
            ):
                return _result(
                    merged,
                    changed=True,
                    outcome="summarized",
                    reason="over_trigger",
                    summary_tokens=summary_tokens,
                )
            # Invalid structure — discard summary path
            logger.warning(
                "Summarized history failed tool pairing validation; discarding"
            )
            summarized = None

        # --- Safe window trim fallback ---
        trimmed, reduced = safe_window_trim(chat, config.target_recent_tokens)
        if reduced and not validate_tool_pairing(trimmed):
            after = _estimate_tokens(trimmed)
            if after >= before:
                # No actual reduction
                return _result(
                    original,
                    changed=False,
                    outcome="kept_original",
                    reason=summary_error or "trim_no_reduction",
                )
            # Still over target but smaller → over_budget if minimal keep is huge
            outcome = "window_trimmed"
            if after > config.target_recent_tokens:
                # Check if we kept everything that is a single oversize chain
                min_start = find_safe_window_start(chat, 1)  # force minimal keep
                min_slice = chat[min_start:]
                if len(min_slice) == len(trimmed) and after > config.target_recent_tokens:
                    outcome = "over_budget_unavoidable"
            return _result(
                trimmed + heartbeat,
                changed=True,
                outcome=outcome,
                reason=summary_error or "summary_failed",
            )

        # Cannot safely reduce
        # Detect over_budget_unavoidable: single message / min chain already over target
        min_start = find_safe_window_start(chat, config.target_recent_tokens)
        min_tokens = _estimate_tokens(chat[min_start:]) if chat else 0
        if min_start == 0 and min_tokens > config.target_recent_tokens:
            return _result(
                original,
                changed=False,
                outcome="over_budget_unavoidable",
                reason="single_message_or_chain_over_target",
            )

        return _result(
            original,
            changed=False,
            outcome="kept_original",
            reason=summary_error or "cannot_safely_reduce",
        )

    async def _try_summarize(
        self,
        chat: List[Dict[str, Any]],
        config: HistoryCompactionConfig,
        summarize: Union[SummarizeFn, Any],
    ) -> Tuple[Optional[List[Dict[str, Any]]], int]:
        """Run summary compression; return (history_or_None, summary_tokens)."""
        from .compressor import (
            extract_existing_summary,
            inject_summary,
            SessionCompressor,
        )

        old_summary, remaining = extract_existing_summary(chat)
        window_start = find_safe_window_start(remaining, config.target_recent_tokens)
        # Guarantee at least last message
        if window_start >= len(remaining) and remaining:
            window_start = len(remaining) - 1

        to_compress = remaining[:window_start]
        to_keep = remaining[window_start:]
        if not to_compress:
            return None, 0

        # Call summarize
        if isinstance(summarize, SessionCompressor):
            new_summary = await summarize._generate_summary(old_summary, to_compress)
        elif callable(summarize):
            new_summary = await summarize(old_summary, to_compress)
        else:
            return None, 0

        if looks_like_summary_error(new_summary):
            logger.warning("Rejecting summary that looks like an error string")
            return None, 0

        new_summary = truncate_summary(new_summary, config.max_summary_tokens)
        summary_tokens = max(1, len(new_summary) // 3) if new_summary else 0
        result = inject_summary(new_summary, to_keep)
        if validate_tool_pairing(result):
            return None, 0
        return result, summary_tokens
