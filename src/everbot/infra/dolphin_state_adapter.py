"""
Dolphin session state adapter for EverBot.

This module centralizes how EverBot reads/writes conversation history so that
business code does not manipulate Dolphin internals directly.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from dolphin.core.common.constants import KEY_HISTORY
from dolphin.core.common.enums import Messages


class DolphinStateAdapter:
    """Adapter layer between EverBot persistence and Dolphin context."""

    ALLOWED_MESSAGE_KEYS = {
        "role",
        "content",
        "tool_calls",
        "tool_call_id",
        "timestamp",
        "name",
        "metadata",
    }

    @classmethod
    def export_session_state(cls, agent: Any) -> Dict[str, Any]:
        """Export a minimal, storage-safe session state from a Dolphin agent."""
        context = agent.executor.context
        history_raw = context.get_history_messages(normalize=False) or []
        history_messages = cls._normalize_messages(history_raw)
        return {"history_messages": history_messages}

    @classmethod
    def import_session_state(
        cls,
        agent: Any,
        state: Dict[str, Any],
        max_messages: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Restore session state into a Dolphin agent context."""
        context = agent.executor.context
        history_messages = state.get("history_messages") or []
        compacted = cls.compact_session_state(history_messages, max_messages=max_messages)
        cls._set_history_bucket(context, compacted)
        return compacted

    @classmethod
    def get_display_messages(cls, agent: Any) -> List[Dict[str, Any]]:
        """Return display-friendly normalized messages from Dolphin context."""
        context = agent.executor.context
        history_raw = context.get_history_messages(normalize=False) or []
        return cls._normalize_messages(history_raw)

    @classmethod
    def validate_session_state(cls, state: Dict[str, Any]) -> List[str]:
        """Validate tool-calling sequence in a session state object."""
        messages = state.get("history_messages") or []
        return cls._validate_tool_sequence(messages)

    @classmethod
    def compact_session_state(
        cls,
        history_messages: List[Dict[str, Any]],
        max_messages: Optional[int],
    ) -> List[Dict[str, Any]]:
        """Compact history while preserving tool-call sequence validity."""
        normalized = cls._normalize_messages(history_messages)
        if not max_messages or max_messages <= 0:
            trimmed = normalized
        elif len(normalized) <= max_messages:
            trimmed = normalized
        else:
            trimmed = list(normalized[-max_messages:])

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

    @classmethod
    def _normalize_messages(cls, history_raw: List[Any]) -> List[Dict[str, Any]]:
        plain_messages = [cls._to_plain_dict(item) for item in history_raw]
        normalized: List[Dict[str, Any]] = []
        try:
            messages = Messages()
            messages.extend_plain_messages(plain_messages)
            normalized = messages.get_messages_as_dict()
        except Exception:
            normalized = plain_messages
        return [cls._sanitize_message(msg) for msg in normalized if isinstance(msg, dict)]

    @classmethod
    def _set_history_bucket(cls, context: Any, history_messages: List[Dict[str, Any]]) -> None:
        messages = Messages()
        messages.extend_plain_messages(history_messages)
        if hasattr(context, "set_history_bucket"):
            context.set_history_bucket(messages)
        # Keep variable-pool history synchronized for contexts/code paths that still
        # read from KEY_HISTORY directly.
        context.set_variable(KEY_HISTORY, messages.get_messages_as_dict())

    @staticmethod
    def _to_plain_dict(msg: Any) -> Dict[str, Any]:
        if isinstance(msg, dict):
            return dict(msg)
        role = getattr(msg, "role", "unknown")
        if hasattr(role, "value"):
            role = role.value
        result: Dict[str, Any] = {
            "role": str(role),
            "content": getattr(msg, "content", ""),
        }
        for key in ("tool_calls", "tool_call_id", "timestamp", "name"):
            value = getattr(msg, key, None)
            if value is not None:
                result[key] = value
        return result

    # ---- Heartbeat / internal message filtering ----

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

    @classmethod
    def _sanitize_message(cls, msg: Dict[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in msg.items() if k in cls.ALLOWED_MESSAGE_KEYS}

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
