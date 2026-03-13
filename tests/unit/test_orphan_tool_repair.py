"""Test that orphan tool messages (e.g. from _load_resource_skill) survive
session save → reload cycles.

Root cause: _load_resource_skill injects a tool-role message into the
conversation.  When the assistant tool_use message is lost (e.g. compression
or export omission), the tool message becomes an "orphan" — no preceding
assistant message references its tool_call_id.  The Dolphin SDK's
repair=True logic then drops it, silently losing skill documentation.

The fix: persistence.restore_to_agent() must heal orphan tool messages
BEFORE passing to import_portable_session, by promoting them to user-role
context blocks so they are never treated as orphans.
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from src.everbot.core.session.persistence import SessionPersistence
from src.everbot.core.session.session_data import SessionData


def _make_session_data(history_messages, **kwargs):
    defaults = dict(
        session_id="test_session",
        agent_name="test_agent",
        history_messages=history_messages,
        variables={"model_name": "gpt-4"},
        session_type="channel",
    )
    defaults.update(kwargs)
    return SessionData(**defaults)


def _make_mock_agent(repair_report=None):
    agent = MagicMock()
    agent.snapshot.import_portable_session.return_value = repair_report or {}
    return agent


# ═══════════════════════════════════════════════════════════
#  Orphan tool messages must be healed before SDK import
# ═══════════════════════════════════════════════════════════


class TestOrphanToolMessageRepair:
    """Orphan tool messages should be converted to user context, not dropped."""

    @pytest.mark.asyncio
    async def test_orphan_tool_message_preserved_as_context(self):
        """A tool message without preceding assistant tool_use should NOT be lost.

        Real scenario: _load_resource_skill result saved as role=tool
        but the assistant tool_call message was lost.
        """
        history = [
            {"role": "user", "content": "帮我 review testcases"},
            # ORPHAN: tool message with no preceding assistant tool_use
            {"role": "tool", "content": "[PIN]\n# Coding Master\n$CM = python tools.py",
             "tool_call_id": "tc_orphan_123"},
            {"role": "assistant", "content": "Here is my review..."},
        ]
        session_data = _make_session_data(history)
        agent = _make_mock_agent()
        persistence = SessionPersistence(sessions_dir="/tmp/test_sessions")

        await persistence.restore_to_agent(agent, session_data)

        # The import_portable_session should have been called
        agent.snapshot.import_portable_session.assert_called_once()
        call_args = agent.snapshot.import_portable_session.call_args
        portable = call_args[0][0]
        restored_history = portable["history_messages"]

        # The tool content must NOT be silently dropped.
        # It should be present in the history (either as tool or promoted to user context).
        all_content = " ".join(
            m.get("content", "") if isinstance(m.get("content"), str)
            else " ".join(p.get("text", "") for p in m.get("content", []) if isinstance(p, dict))
            for m in restored_history
        )
        assert "Coding Master" in all_content, (
            "Skill documentation from orphan tool message was lost during restore"
        )

    @pytest.mark.asyncio
    async def test_orphan_tool_becomes_user_context(self):
        """Orphan tool message should be merged into preceding user message."""
        history = [
            {"role": "user", "content": "use coding master"},
            {"role": "tool", "content": "[PIN]\n# Skill Docs Here",
             "tool_call_id": "tc_lost_001"},
            {"role": "assistant", "content": "OK, running CM..."},
        ]
        session_data = _make_session_data(history)
        agent = _make_mock_agent()
        persistence = SessionPersistence(sessions_dir="/tmp/test_sessions")

        await persistence.restore_to_agent(agent, session_data)

        portable = agent.snapshot.import_portable_session.call_args[0][0]
        restored = portable["history_messages"]

        # Should have no orphan tool messages — they should be promoted
        orphan_tools = []
        for i, m in enumerate(restored):
            if m.get("role") == "tool":
                # Check if there's a preceding assistant with matching tool_call
                has_parent = False
                for j in range(i - 1, -1, -1):
                    prev = restored[j]
                    if prev.get("role") == "assistant":
                        for tc in prev.get("tool_calls", []):
                            if tc.get("id") == m.get("tool_call_id"):
                                has_parent = True
                        break
                if not has_parent:
                    orphan_tools.append(m)
        assert len(orphan_tools) == 0, (
            f"Orphan tool messages should have been healed: {orphan_tools}"
        )

        # The skill content should still be present somewhere
        all_content = " ".join(
            m.get("content", "") if isinstance(m.get("content"), str) else ""
            for m in restored
        )
        assert "Skill Docs Here" in all_content

    @pytest.mark.asyncio
    async def test_paired_tool_message_not_modified(self):
        """A properly paired tool message (with assistant tool_use) must be left alone."""
        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "tc_paired", "type": "function",
                             "function": {"name": "_bash", "arguments": "{}"}}]},
            {"role": "tool", "content": "command output here",
             "tool_call_id": "tc_paired"},
            {"role": "assistant", "content": "Done."},
        ]
        session_data = _make_session_data(history)
        agent = _make_mock_agent()
        persistence = SessionPersistence(sessions_dir="/tmp/test_sessions")

        await persistence.restore_to_agent(agent, session_data)

        portable = agent.snapshot.import_portable_session.call_args[0][0]
        restored = portable["history_messages"]

        # The paired tool message should remain as role=tool
        tool_msgs = [m for m in restored if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["content"] == "command output here"
        assert tool_msgs[0]["tool_call_id"] == "tc_paired"

    @pytest.mark.asyncio
    async def test_multiple_orphan_tools_all_healed(self):
        """Multiple orphan tool messages in sequence should all be healed."""
        history = [
            {"role": "user", "content": "start"},
            {"role": "tool", "content": "[PIN]\n# Skill A docs",
             "tool_call_id": "tc_a"},
            {"role": "tool", "content": "folder listing: file1.py, file2.py",
             "tool_call_id": "tc_b"},
            {"role": "assistant", "content": "Loaded skills and folder."},
        ]
        session_data = _make_session_data(history)
        agent = _make_mock_agent()
        persistence = SessionPersistence(sessions_dir="/tmp/test_sessions")

        await persistence.restore_to_agent(agent, session_data)

        portable = agent.snapshot.import_portable_session.call_args[0][0]
        restored = portable["history_messages"]

        # No orphan tool messages should remain
        for i, m in enumerate(restored):
            if m.get("role") == "tool":
                # Must have a preceding assistant with matching tool_call
                found = False
                for j in range(i - 1, -1, -1):
                    prev = restored[j]
                    if prev.get("role") == "assistant":
                        for tc in prev.get("tool_calls", []):
                            if tc.get("id") == m.get("tool_call_id"):
                                found = True
                        break
                assert found, f"Orphan tool message at index {i} not healed"

        # All content preserved
        all_text = " ".join(
            m.get("content", "") if isinstance(m.get("content"), str) else ""
            for m in restored
        )
        assert "Skill A docs" in all_text
        assert "file1.py" in all_text

    @pytest.mark.asyncio
    async def test_orphan_at_end_of_history(self):
        """Orphan tool message at the very end (no following assistant)."""
        history = [
            {"role": "user", "content": "do something"},
            {"role": "tool", "content": "[PIN]\n# Docs",
             "tool_call_id": "tc_end"},
        ]
        session_data = _make_session_data(history)
        agent = _make_mock_agent()
        persistence = SessionPersistence(sessions_dir="/tmp/test_sessions")

        await persistence.restore_to_agent(agent, session_data)

        portable = agent.snapshot.import_portable_session.call_args[0][0]
        restored = portable["history_messages"]

        # Content should be preserved even without a following assistant
        all_text = " ".join(
            m.get("content", "") if isinstance(m.get("content"), str) else ""
            for m in restored
        )
        assert "Docs" in all_text

    @pytest.mark.asyncio
    async def test_no_messages_no_crash(self):
        """Empty history should not crash the orphan repair logic."""
        session_data = _make_session_data([])
        agent = _make_mock_agent()
        persistence = SessionPersistence(sessions_dir="/tmp/test_sessions")

        await persistence.restore_to_agent(agent, session_data)

        portable = agent.snapshot.import_portable_session.call_args[0][0]
        assert portable["history_messages"] == []
