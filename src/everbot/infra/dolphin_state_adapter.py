"""
Dolphin session state adapter for EverBot.

Provides history compaction (max_messages truncation with tool-call sequence
preservation) and heartbeat message filtering.  Export/import/validate of
portable sessions is handled by the Dolphin SDK's ``agent.snapshot`` API.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class DolphinStateAdapter:
    """Adapter layer between EverBot persistence and Dolphin context.

    Retained capabilities:
    - ``compact_session_state``: truncate history to *max_messages* while
      preserving tool-call pairing and context-summary headers.
    - Heartbeat / init-prompt message detection and stripping.
    """

    # ── History compaction ────────────────────────────────────────

    @classmethod
    def compact_session_state(
        cls,
        history_messages: List[Dict[str, Any]],
        max_messages: Optional[int],
    ) -> List[Dict[str, Any]]:
        """Compact history while preserving tool-call sequence validity."""
        from ..core.session.compressor import SUMMARY_TAG

        messages = [msg for msg in history_messages if isinstance(msg, dict)]
        if not max_messages or max_messages <= 0:
            trimmed = list(messages)
        elif len(messages) <= max_messages:
            trimmed = list(messages)
        else:
            # Preserve summary message pair at the head if present.
            summary_prefix: List[Dict[str, Any]] = []
            rest = messages
            if (
                len(messages) >= 2
                and messages[0].get("role") == "user"
                and isinstance(messages[0].get("content"), str)
                and SUMMARY_TAG in messages[0]["content"]
                and messages[1].get("role") == "assistant"
            ):
                summary_prefix = messages[:2]
                rest = messages[2:]

            budget = max_messages - len(summary_prefix)
            if budget <= 0:
                trimmed = list(summary_prefix)
            elif len(rest) <= budget:
                trimmed = summary_prefix + rest
            else:
                trimmed = summary_prefix + list(rest[-budget:])

        # 1. Avoid starting from an orphaned tool response.
        while trimmed and trimmed[0].get("role") == "tool":
            trimmed.pop(0)

        # 2. Avoid starting from an assistant tool call without its paired tool responses.
        while trimmed and cls._is_assistant_tool_call(trimmed[0]):
            trimmed.pop(0)
            while trimmed and trimmed[0].get("role") == "tool":
                trimmed.pop(0)

        # 3. Suffix Fix: Avoid ending with an assistant tool call that has no (or incomplete) responses.
        # This prevents BadRequest 400 errors where the model protocol is violated.
        while trimmed and cls._validate_tool_sequence(trimmed):
            # If the validation error is at the end (assistant tool call without responses)
            if cls._is_assistant_tool_call(trimmed[-1]):
                trimmed.pop()
                continue

            # As a fallback for other issues (like missing tool responses in the middle),
            # we keep dropping from the front until the sequence is valid.
            trimmed.pop(0)

        return trimmed

    @staticmethod
    def _is_assistant_tool_call(msg: Dict[str, Any]) -> bool:
        return msg.get("role") == "assistant" and isinstance(msg.get("tool_calls"), list) and len(msg["tool_calls"]) > 0

    @classmethod
    def _validate_tool_sequence(cls, messages: List[Dict[str, Any]]) -> List[str]:
        issues: List[str] = []
        idx = 0
        while idx < len(messages):
            msg = messages[idx]
            if cls._is_assistant_tool_call(msg):
                expected_ids = []
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict):
                        call_id = tc.get("id") or tc.get("tool_call_id")
                        if call_id:
                            expected_ids.append(call_id)

                matched = []
                pointer = idx + 1
                while pointer < len(messages) and len(matched) < len(expected_ids):
                    next_msg = messages[pointer]
                    if next_msg.get("role") != "tool":
                        issues.append(
                            f"assistant tool_calls at index {idx} is followed by non-tool role={next_msg.get('role')} at index {pointer}"
                        )
                        break
                    tool_call_id = next_msg.get("tool_call_id")
                    if tool_call_id in expected_ids and tool_call_id not in matched:
                        matched.append(tool_call_id)
                    pointer += 1

                if set(matched) != set(expected_ids):
                    issues.append(
                        f"assistant tool_calls at index {idx} missing tool responses: expected={expected_ids}, matched={matched}"
                    )
            idx += 1
        return issues

    # ── Heartbeat / internal message filtering ────────────────────

    HEARTBEAT_USER_PREFIXES = ("[系统心跳", "[系統心跳")

    # Pattern for the initial /explore/ prompt template that Dolphin stores as
    # a user message (e.g. "demo_agent Agent\n当前时间：...\n请根据用户的要求提供帮助。\n...")
    _INIT_PROMPT_MARKER = "请根据用户的要求提供帮助"

    @classmethod
    def is_heartbeat_user_message(cls, msg: Dict[str, Any]) -> bool:
        """Check if a message is a heartbeat-injected user message."""
        if msg.get("role") != "user":
            return False
        content = msg.get("content")
        if not isinstance(content, str):
            return False
        trimmed = content.lstrip()
        return any(trimmed.startswith(p) for p in cls.HEARTBEAT_USER_PREFIXES)

    @classmethod
    def _is_init_prompt_message(cls, msg: Dict[str, Any]) -> bool:
        """Check if a message is the initial /explore/ prompt template."""
        if msg.get("role") != "user":
            return False
        content = msg.get("content")
        if not isinstance(content, str):
            return False
        return cls._INIT_PROMPT_MARKER in content and "Agent" in content

    @classmethod
    def strip_heartbeat_turns(cls, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Remove heartbeat user messages and their subsequent assistant/tool responses.

        A heartbeat "turn" is: heartbeat user msg → (assistant msg with optional
        tool_calls → tool responses)*.  We consume all consecutive assistant/tool
        messages that follow a heartbeat user message until the next user message.
        """
        result: List[Dict[str, Any]] = []
        skip_until_next_user = False

        for msg in messages:
            role = msg.get("role", "")
            if role == "user":
                if cls.is_heartbeat_user_message(msg) or cls._is_init_prompt_message(msg):
                    skip_until_next_user = True
                    continue
                else:
                    skip_until_next_user = False
            elif skip_until_next_user:
                # Skip assistant/tool messages that belong to the heartbeat turn
                continue

            result.append(msg)

        return result
