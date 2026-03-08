"""History policy utilities — heartbeat isolation and token-budget compact.

Public API:
- _is_heartbeat(msg) — identify heartbeat messages (metadata + legacy prefix)
- _is_placeholder(msg) — identify placeholder messages (metadata + legacy content)
- _estimate_tokens(messages) — estimate token count (chars // 3)
- evict_oldest_heartbeat(history, max_heartbeat) — cap heartbeat count with FIFO eviction
- prepare_for_restore(messages) — strip placeholders and normalize heartbeat results for LLM context
- extract_recent_heartbeat(messages, max_count) — extract recent heartbeat messages from primary session
"""

from __future__ import annotations

from typing import List

MAX_HEARTBEAT_MESSAGES = 20
COMPACT_TOKEN_BUDGET = 40_000
COMPACT_WINDOW_TOKENS = 20_000

_HEARTBEAT_PREFIX = "[此消息由心跳系统自动执行例行任务生成]"
_PLACEHOLDER_CONTENTS = {"(acknowledged)", "[Background notification follows]"}

# ── Token estimation ─────────────────────────────────────────────────

_MSG_OVERHEAD = 12  # role/name/tool_call_id structural chars (~4 tokens)


def _estimate_tokens(messages: List[dict]) -> int:
    """Estimate token count for a message list.

    Counts all content that would be sent to the LLM:
    content, tool_calls (name + arguments + id), tool_call_id, plus
    per-message structural overhead.

    Ratio: 1 token ~ 3 chars (consistent with context_manager.py).
    """
    total_chars = 0
    for msg in messages:
        total_chars += _MSG_OVERHEAD

        # content
        content = msg.get("content") or ""
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    total_chars += len(str(part.get("text", "")))

        # tool_calls (assistant messages)
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            total_chars += len(fn.get("name") or "")
            total_chars += len(fn.get("arguments") or "")
            total_chars += len(tc.get("id") or "")

        # tool_call_id (tool response messages)
        total_chars += len(msg.get("tool_call_id") or "")

    return total_chars // 3


# ── Message classification ───────────────────────────────────────────


def _is_heartbeat(msg: dict) -> bool:
    """Identify heartbeat messages (new metadata format + legacy content prefix)."""
    meta = msg.get("metadata")
    if isinstance(meta, dict) and meta.get("source") == "heartbeat":
        return True
    content = msg.get("content") or ""
    return isinstance(content, str) and content.startswith(_HEARTBEAT_PREFIX)


def _is_placeholder(msg: dict) -> bool:
    """Identify placeholder messages (new metadata format + legacy content match).

    Legacy placeholders have no metadata — identified by exact content match.
    This is consistent with persistence.py's existing _filter_heartbeat_messages.
    """
    meta = msg.get("metadata")
    if isinstance(meta, dict) and meta.get("category") == "placeholder":
        return True
    content = msg.get("content")
    return isinstance(content, str) and content in _PLACEHOLDER_CONTENTS


# ── Heartbeat eviction ───────────────────────────────────────────────


def evict_oldest_heartbeat(
    history: List[dict],
    max_heartbeat: int = MAX_HEARTBEAT_MESSAGES,
) -> List[dict]:
    """Evict oldest heartbeat messages (and their placeholders) to stay under *max_heartbeat*.

    Algorithm: two-pass scan.
    Pass 1: find heartbeat indices, determine which to evict (oldest N).
    Pass 2: filter out evicted heartbeats + bound placeholders (by run_id for new format,
             by position heuristic for legacy format).
    """
    heartbeat_indices = [i for i, m in enumerate(history) if _is_heartbeat(m)]
    if len(heartbeat_indices) <= max_heartbeat:
        return history

    to_evict = set(heartbeat_indices[: len(heartbeat_indices) - max_heartbeat])

    # Collect run_ids of evicted heartbeats (new format placeholder matching)
    evict_run_ids: set = set()
    for idx in to_evict:
        meta = history[idx].get("metadata")
        if isinstance(meta, dict) and meta.get("run_id"):
            evict_run_ids.add(meta["run_id"])

    result: List[dict] = []
    for i, msg in enumerate(history):
        if i in to_evict:
            continue

        # New-format placeholder: metadata.run_id in eviction set
        meta = msg.get("metadata")
        if (
            isinstance(meta, dict)
            and meta.get("run_id") in evict_run_ids
            and meta.get("category") == "placeholder"
        ):
            continue

        # Legacy placeholder: immediately before an evicted heartbeat
        if _is_placeholder(msg) and (i + 1) in to_evict:
            continue
        # Legacy placeholder pair: (acknowledged) two positions before evicted heartbeat
        if _is_placeholder(msg) and (i + 2) in to_evict:
            if i + 1 < len(history) and _is_placeholder(history[i + 1]):
                continue

        result.append(msg)
    return result


# ── Restore preparation ─────────────────────────────────────────────


def _normalize_heartbeat(msg: dict) -> dict:
    """Normalize a heartbeat message for LLM context.

    Strips the legacy content prefix so the LLM sees clean text.
    Preserves ``metadata.source = "heartbeat"`` so the message remains
    identifiable after a channel session save→restore round-trip.
    """
    msg = dict(msg)

    # Strip legacy content prefix
    content = msg.get("content") or ""
    if isinstance(content, str) and content.startswith(_HEARTBEAT_PREFIX):
        content = content[len(_HEARTBEAT_PREFIX):].lstrip("\n")
        msg["content"] = content

    return msg


def prepare_for_restore(messages: List[dict]) -> List[dict]:
    """Prepare history messages for LLM context restoration.

    Two concerns separated:
    - **Placeholders** (structural role-alternation artifacts) → removed
    - **Heartbeat results** (content-bearing async task output) → normalized
      (legacy prefix stripped) but ``metadata.source`` preserved so they
      remain identifiable after a channel session save→restore round-trip.
    """
    result: List[dict] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if _is_placeholder(msg):
            continue
        if _is_heartbeat(msg):
            msg = _normalize_heartbeat(msg)
        result.append(msg)
    return result


# ── Cross-session heartbeat context ───────────────────────────────


def extract_recent_heartbeat(
    messages: List[dict],
    max_count: int = 5,
) -> List[dict]:
    """Extract the most recent heartbeat messages from a session history.

    Returns normalized copies (legacy prefix stripped, metadata preserved)
    so they can be injected into another session while remaining identifiable.
    """
    heartbeats: List[dict] = []
    for msg in messages:
        if isinstance(msg, dict) and _is_heartbeat(msg):
            heartbeats.append(msg)
    recent = heartbeats[-max_count:] if len(heartbeats) > max_count else heartbeats
    return [_normalize_heartbeat(m) for m in recent]
