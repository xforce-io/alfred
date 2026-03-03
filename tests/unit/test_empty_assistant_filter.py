"""
Tests for empty assistant message filtering during session save/restore.

Bug: LLM returns tool_calls with empty content → persisted as
{"role": "assistant", "content": ""}.  On restore, tool_calls may be
stripped, leaving a bare empty assistant message that DeepSeek API rejects
with 400 error.

These tests MUST FAIL with the current code to prove the bug exists.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dolphin.core.common.constants import KEY_HISTORY
from src.everbot.core.session.persistence import SessionPersistence
from src.everbot.core.session.session import SessionManager, SessionData


class MinimalContext:
    """Bare-bones context for testing persistence round-trips."""

    def __init__(self):
        self._vars: dict[str, Any] = {}
        self._history: list[dict[str, Any]] = []

    def get_var_value(self, name: str) -> Any:
        return self._vars.get(name)

    def set_variable(self, name: str, value: Any) -> None:
        self._vars[name] = value
        if name == KEY_HISTORY and isinstance(value, list):
            self._history = list(value)

    def get_history_messages(self, normalize: bool = False) -> list[dict[str, Any]]:
        return list(self._history)

    def set_history_bucket(self, messages: Any) -> None:
        if isinstance(messages, list):
            self._history = list(messages)

    def init_trajectory(self, path: str, overwrite: bool = True) -> None:
        pass

    def set_session_id(self, sid: str) -> None:
        self._vars["session_id"] = sid

    def get_session_id(self) -> str:
        return self._vars.get("session_id", "")

    def get_user_variables(self, include_system_context_vars: bool = False) -> dict:
        return dict(self._vars)

    def delete_variable(self, name: str) -> None:
        self._vars.pop(name, None)


class MinimalSnapshot:
    def __init__(self, ctx: MinimalContext):
        self._ctx = ctx

    def export_portable_session(self) -> dict:
        return {
            "session_id": self._ctx.get_session_id(),
            "history_messages": list(self._ctx._history),
            "variables": {k: v for k, v in self._ctx._vars.items() if k != KEY_HISTORY},
        }

    def import_portable_session(self, state: dict, repair: bool = False, trusted: bool = False) -> dict:
        history = state.get("history_messages", [])
        self._ctx.set_variable(KEY_HISTORY, history)
        self._ctx._history = list(history)
        variables = state.get("variables", {})
        for k, v in variables.items():
            if k != KEY_HISTORY:
                self._ctx.set_variable(k, v)
        return {}


def _make_agent(name: str, history: list[dict]) -> SimpleNamespace:
    ctx = MinimalContext()
    ctx._history = list(history)
    ctx.set_variable(KEY_HISTORY, list(history))
    snapshot = MinimalSnapshot(ctx)
    return SimpleNamespace(
        name=name,
        executor=SimpleNamespace(context=ctx),
        snapshot=snapshot,
        get_execution_trace=lambda: {},
    )


# ---------------------------------------------------------------------------
# Bug 1: Empty assistant messages survive save/restore round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_assistant_message_filtered_on_save(tmp_path: Path):
    """Session save should strip empty assistant messages that have no tool_calls.

    An assistant message with content="" and no tool_calls is meaningless and
    will cause API errors (e.g. DeepSeek 400).
    """
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    sm = SessionManager(sessions_dir)

    history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
        {"role": "user", "content": "next"},
        {"role": "assistant", "content": ""},  # ← BUG: empty, should be filtered
        {"role": "user", "content": "another"},
        {"role": "assistant", "content": "ok"},
    ]
    agent = _make_agent("test_agent", history)
    session_id = "web_session_test_agent"

    await sm.save_session(session_id, agent)

    loaded = await sm.load_session(session_id)
    assert loaded is not None

    restored_msgs = loaded.history_messages
    empty_assistants = [
        (i, m) for i, m in enumerate(restored_msgs)
        if m.get("role") == "assistant" and not m.get("content", "").strip()
        and not m.get("tool_calls")
    ]
    assert len(empty_assistants) == 0, (
        f"Session save should filter bare empty assistant messages, but found "
        f"{len(empty_assistants)} at positions {[i for i, _ in empty_assistants]}. "
        f"These will cause DeepSeek API 400 errors."
    )


@pytest.mark.asyncio
async def test_empty_assistant_with_tool_calls_preserved(tmp_path: Path):
    """An assistant message with empty content BUT valid tool_calls should
    be preserved (this is a legitimate tool-only response).
    """
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    sm = SessionManager(sessions_dir)

    history = [
        {"role": "user", "content": "search for X"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "tc1", "type": "function", "function": {"name": "search", "arguments": "{}"}}
        ]},
        {"role": "tool", "content": "result Y", "tool_call_id": "tc1"},
        {"role": "assistant", "content": "Found Y"},
    ]
    agent = _make_agent("test_agent", history)
    session_id = "web_session_test_agent"

    await sm.save_session(session_id, agent)

    loaded = await sm.load_session(session_id)
    assert loaded is not None

    restored_msgs = loaded.history_messages
    # The assistant message with tool_calls should still exist
    tool_call_assistants = [
        m for m in restored_msgs
        if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    assert len(tool_call_assistants) >= 1, (
        "Assistant message with tool_calls should be preserved even if content is empty"
    )


@pytest.mark.asyncio
async def test_restore_filters_empty_assistant_from_disk(tmp_path: Path):
    """Even if an old session file contains empty assistant messages,
    restore should filter them out before feeding to the agent.
    """
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    session_id = "web_session_test_agent"
    # Simulate a corrupted session file on disk
    session_file = sessions_dir / f"{session_id}.json"
    session_file.write_text(json.dumps({
        "session_id": session_id,
        "agent_name": "test_agent",
        "model_name": "gpt-4",
        "session_type": "primary",
        "history_messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "next"},
            {"role": "assistant", "content": ""},  # ← corrupted empty message
            {"role": "user", "content": "more"},
            {"role": "assistant", "content": "ok"},
        ],
        "mailbox": [],
        "variables": {},
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "state": "active",
        "timeline": [],
        "context_trace": {},
        "revision": 1,
    }), encoding="utf-8")

    sm = SessionManager(sessions_dir)
    agent = _make_agent("test_agent", [])

    loaded = await sm.load_session(session_id)
    assert loaded is not None
    await sm.restore_to_agent(agent, loaded)

    # Agent context should NOT have the empty assistant message
    ctx_history = agent.executor.context._history
    empty_assistants = [
        (i, m) for i, m in enumerate(ctx_history)
        if m.get("role") == "assistant" and not m.get("content", "").strip()
        and not m.get("tool_calls")
    ]
    assert len(empty_assistants) == 0, (
        f"After restore, agent context should not contain bare empty assistant "
        f"messages, but found {len(empty_assistants)} at positions "
        f"{[i for i, _ in empty_assistants]}."
    )


# ---------------------------------------------------------------------------
# Bug 1b: Empty assistant messages in trailing_messages code path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trailing_messages_with_empty_assistant_filtered(tmp_path: Path):
    """When persistence.save() appends trailing_messages, empty assistant
    messages in the trailing portion must also be filtered.

    This exercises the code path at persistence.py:168-177 where
    trailing_messages are appended after orphan tool_calls are trimmed.
    """
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    persistence = SessionPersistence(sessions_dir)

    history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]
    agent = _make_agent("test_agent", history)
    session_id = "web_session_test_agent"

    # trailing_messages contain an empty assistant message (artifact from
    # a failed tool execution that timed out)
    trailing = [
        {"role": "assistant", "content": ""},  # ← should be filtered
        {"role": "user", "content": "retry"},
        {"role": "assistant", "content": "done"},
    ]

    await persistence.save(session_id, agent, trailing_messages=trailing)

    loaded = await persistence.load(session_id)
    assert loaded is not None

    empty_assistants = [
        (i, m) for i, m in enumerate(loaded.history_messages)
        if m.get("role") == "assistant" and not m.get("content", "").strip()
        and not m.get("tool_calls")
    ]
    assert len(empty_assistants) == 0, (
        f"persistence.save() with trailing_messages should filter empty "
        f"assistant messages, but found {len(empty_assistants)} at positions "
        f"{[i for i, _ in empty_assistants]}. "
        f"Full history: {loaded.history_messages}"
    )


@pytest.mark.asyncio
async def test_trailing_messages_orphan_tool_calls_trimmed(tmp_path: Path):
    """When trailing_messages are provided, orphan assistant tool_calls at
    the end of main history should be trimmed before appending trailing.

    An orphan tool_call is an assistant message with tool_calls but no
    corresponding tool response — this breaks the tool chain contract.
    """
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    persistence = SessionPersistence(sessions_dir)

    history = [
        {"role": "user", "content": "search for X"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "tc1", "type": "function", "function": {"name": "search", "arguments": "{}"}}
        ]},
        # NOTE: No tool response follows — this is an orphan tool_call
    ]
    agent = _make_agent("test_agent", history)
    session_id = "web_session_test_agent"

    trailing = [
        {"role": "assistant", "content": "Sorry, the search timed out."},
    ]

    await persistence.save(session_id, agent, trailing_messages=trailing)

    loaded = await persistence.load(session_id)
    assert loaded is not None

    # The orphan assistant+tool_calls should have been trimmed
    orphan_tool_calls = [
        m for m in loaded.history_messages
        if m.get("role") == "assistant" and m.get("tool_calls") and (
            # Check if next message is NOT a tool response
            loaded.history_messages.index(m) == len(loaded.history_messages) - 1 or
            loaded.history_messages[loaded.history_messages.index(m) + 1].get("role") != "tool"
        )
    ]
    # The orphan should have been removed; only the trailing message remains
    assert any(
        m.get("content") == "Sorry, the search timed out."
        for m in loaded.history_messages
    ), (
        f"trailing_messages should be appended after trimming orphan tool_calls. "
        f"History: {loaded.history_messages}"
    )


@pytest.mark.asyncio
async def test_whitespace_only_assistant_filtered(tmp_path: Path):
    """Assistant messages with whitespace-only content (spaces, tabs, newlines)
    should also be treated as empty and filtered out.
    """
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    sm = SessionManager(sessions_dir)

    history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "   \n\t  "},  # whitespace-only
        {"role": "user", "content": "next"},
        {"role": "assistant", "content": "ok"},
    ]
    agent = _make_agent("test_agent", history)
    session_id = "web_session_test_agent"

    await sm.save_session(session_id, agent)

    loaded = await sm.load_session(session_id)
    assert loaded is not None

    empty_assistants = [
        m for m in loaded.history_messages
        if m.get("role") == "assistant" and not m.get("content", "").strip()
        and not m.get("tool_calls")
    ]
    assert len(empty_assistants) == 0, (
        f"Whitespace-only assistant messages should be filtered, but found "
        f"{len(empty_assistants)}. History: {loaded.history_messages}"
    )


@pytest.mark.asyncio
async def test_none_content_assistant_filtered(tmp_path: Path):
    """Assistant message with content=None should be filtered."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    sm = SessionManager(sessions_dir)

    history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": None},  # None content
        {"role": "user", "content": "next"},
        {"role": "assistant", "content": "ok"},
    ]
    agent = _make_agent("test_agent", history)
    session_id = "web_session_test_agent"

    await sm.save_session(session_id, agent)

    loaded = await sm.load_session(session_id)
    assert loaded is not None

    # content=None should be treated as empty
    none_assistants = [
        m for m in loaded.history_messages
        if m.get("role") == "assistant" and m.get("content") is None
        and not m.get("tool_calls")
    ]
    assert len(none_assistants) == 0, (
        f"Assistant messages with content=None should be filtered, but found "
        f"{len(none_assistants)}. History: {loaded.history_messages}"
    )


# ---------------------------------------------------------------------------
# Bug 1c: Checksum corruption recovery via .bak fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checksum_corruption_falls_back_to_bak(tmp_path: Path):
    """When the main session file has a corrupted checksum, load() should
    fall back to the .bak file and recover the session.
    """
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    persistence = SessionPersistence(sessions_dir)

    # First, create a valid session via normal save
    history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ]
    agent = _make_agent("test_agent", history)
    session_id = "web_session_test_agent"
    await persistence.save(session_id, agent)

    # Verify save created the file
    session_path = sessions_dir / f"{session_id}.json"
    assert session_path.exists()

    # Save again to create a .bak (atomic_save rotates old → .bak)
    history2 = history + [
        {"role": "user", "content": "second turn"},
        {"role": "assistant", "content": "response2"},
    ]
    agent2 = _make_agent("test_agent", history2)
    await persistence.save(session_id, agent2)

    bak_path = session_path.with_suffix(".json.bak")
    assert bak_path.exists(), ".bak file should exist after second save"

    # Now corrupt the main file by tampering with content
    raw = session_path.read_text()
    data = json.loads(raw)
    data["history_messages"].append({"role": "user", "content": "INJECTED"})
    # Write back WITHOUT updating checksum → checksum mismatch
    session_path.write_text(json.dumps(data, indent=2))

    # load() should detect corruption and fall back to .bak
    loaded = await persistence.load(session_id)
    assert loaded is not None, (
        "load() should recover from .bak when main file has corrupted checksum"
    )
    # .bak contains the first save (without "second turn")
    injected = [
        m for m in loaded.history_messages
        if isinstance(m, dict) and m.get("content") == "INJECTED"
    ]
    assert len(injected) == 0, (
        "Corrupted main file data should not be loaded; .bak fallback should "
        f"be used instead. Got history: {loaded.history_messages}"
    )


@pytest.mark.asyncio
async def test_both_files_corrupt_returns_none(tmp_path: Path):
    """When both main and .bak files are corrupt, load() should return None."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    persistence = SessionPersistence(sessions_dir)

    session_id = "web_session_test_agent"
    session_path = sessions_dir / f"{session_id}.json"
    bak_path = session_path.with_suffix(".json.bak")

    # Write corrupt data to both files
    session_path.write_text("not valid json at all {{{")
    bak_path.write_text("also not valid json |||")

    loaded = await persistence.load(session_id)
    assert loaded is None, (
        "load() should return None when both main and .bak are corrupt"
    )
