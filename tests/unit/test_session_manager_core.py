"""Unit tests for SessionManager and SessionPersistence core methods with zero coverage."""

from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock

import pytest

from src.everbot.core.session.session import (
    SessionData,
    SessionManager,
    SessionPersistence,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session_data(session_id: str = "web_session_test_agent", **overrides) -> SessionData:
    """Create a minimal SessionData for testing."""
    defaults = dict(
        session_id=session_id,
        agent_name="test_agent",
        model_name="gpt-4",
        session_type=SessionManager.infer_session_type(session_id),
        history_messages=[{"role": "user", "content": "hello"}],
        mailbox=[],
        variables={"key": "value"},
        created_at="2024-01-01T00:00:00",
        updated_at="2024-01-01T00:00:00",
        state="active",
        events=[],
        timeline=[{"type": "turn_start", "timestamp": "2024-01-01T00:00:00"}],
        context_trace={"trace": True},
        revision=1,
    )
    defaults.update(overrides)
    return SessionData(**defaults)


def _make_mock_agent(
    name: str = "test_agent",
    history_messages: list | None = None,
    variables: dict | None = None,
):
    """Create a mock agent with snapshot.import_portable_session."""
    agent = MagicMock()
    agent.name = name
    agent.snapshot = MagicMock()
    agent.snapshot.import_portable_session = MagicMock()
    agent.snapshot.export_portable_session = MagicMock(return_value={
        "history_messages": history_messages or [],
        "variables": variables or {},
    })
    agent.executor = MagicMock()
    agent.executor.context = MagicMock()
    agent.executor.context.get_var_value = MagicMock(return_value=None)
    return agent


# ===========================================================================
# 1. SessionManager.inject_history_message
# ===========================================================================

class TestInjectHistoryMessage:

    @pytest.mark.asyncio
    async def test_successful_injection(self, tmp_path: Path):
        manager = SessionManager(tmp_path)
        session_id = "web_session_test_agent"
        message = {"role": "assistant", "content": "injected message"}

        result = await manager.inject_history_message(session_id, message)

        assert result is True
        loaded = await manager.load_session(session_id)
        assert loaded is not None
        assert any(m.get("content") == "injected message" for m in loaded.history_messages)

    @pytest.mark.asyncio
    async def test_non_dict_input_returns_false(self, tmp_path: Path):
        manager = SessionManager(tmp_path)
        session_id = "web_session_test_agent"

        assert await manager.inject_history_message(session_id, "not a dict") is False
        assert await manager.inject_history_message(session_id, 42) is False
        assert await manager.inject_history_message(session_id, None) is False
        assert await manager.inject_history_message(session_id, ["list"]) is False

    @pytest.mark.asyncio
    async def test_records_history_inject_count_metric(self, tmp_path: Path):
        manager = SessionManager(tmp_path)
        session_id = "web_session_test_agent"

        await manager.inject_history_message(session_id, {"role": "user", "content": "hi"})

        metrics = manager.get_metrics_snapshot()
        assert metrics.get("history_inject_count", 0) >= 1

    @pytest.mark.asyncio
    async def test_multiple_injections_accumulate(self, tmp_path: Path):
        manager = SessionManager(tmp_path)
        session_id = "web_session_test_agent"

        await manager.inject_history_message(session_id, {"role": "user", "content": "msg1"})
        await manager.inject_history_message(session_id, {"role": "assistant", "content": "msg2"})

        loaded = await manager.load_session(session_id)
        assert loaded is not None
        assert len(loaded.history_messages) == 2


# ===========================================================================
# 2. SessionManager.clear_session_history
# ===========================================================================

class TestClearSessionHistory:

    @pytest.mark.asyncio
    async def test_successful_clear(self, tmp_path: Path):
        manager = SessionManager(tmp_path)
        persistence = manager.persistence
        session_id = "web_session_test_agent"

        data = _make_session_data(session_id=session_id)
        await persistence.save_data(data)

        result = await manager.clear_session_history(session_id)

        assert result is True
        loaded = await manager.load_session(session_id)
        assert loaded is not None
        assert loaded.history_messages == []
        assert loaded.events == []
        assert loaded.timeline == []
        assert loaded.context_trace == {}
        # Metadata preserved
        assert loaded.session_id == session_id
        assert loaded.agent_name == "test_agent"

    @pytest.mark.asyncio
    async def test_nonexistent_session_returns_false(self, tmp_path: Path):
        manager = SessionManager(tmp_path)
        result = await manager.clear_session_history("web_session_nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_in_memory_caches_evicted(self, tmp_path: Path):
        manager = SessionManager(tmp_path)
        session_id = "web_session_test_agent"

        data = _make_session_data(session_id=session_id)
        await manager.persistence.save_data(data)

        # Populate in-memory caches
        manager._agents[session_id] = "fake_agent"
        manager._agent_metadata[session_id] = {"agent_name": "test_agent"}
        manager._timeline_events[session_id] = [{"type": "turn_start"}]

        await manager.clear_session_history(session_id)

        assert session_id not in manager._agents
        assert session_id not in manager._agent_metadata
        assert session_id not in manager._timeline_events


# ===========================================================================
# 3. SessionManager.list_agent_sessions
# ===========================================================================

class TestListAgentSessions:

    @pytest.mark.asyncio
    async def test_multiple_sessions_sorted_by_updated_at(self, tmp_path: Path):
        manager = SessionManager(tmp_path)

        # Create sessions with different updated_at times
        s1 = _make_session_data(
            session_id="web_session_myagent",
            agent_name="myagent",
            updated_at="2024-01-01T00:00:00",
        )
        s2 = _make_session_data(
            session_id="web_session_myagent__20240102_abc",
            agent_name="myagent",
            updated_at="2024-01-03T00:00:00",
        )
        s3 = _make_session_data(
            session_id="web_session_myagent__20240103_def",
            agent_name="myagent",
            updated_at="2024-01-02T00:00:00",
        )
        for s in [s1, s2, s3]:
            await manager.persistence.save_data(s)

        items = await manager.list_agent_sessions("myagent")

        assert len(items) == 3
        # Sorted desc by updated_at: s2 (Jan 3) > s3 (Jan 2) > s1 (Jan 1)
        assert items[0]["session_id"] == "web_session_myagent__20240102_abc"
        assert items[1]["session_id"] == "web_session_myagent__20240103_def"
        assert items[2]["session_id"] == "web_session_myagent"

    @pytest.mark.asyncio
    async def test_limit_param(self, tmp_path: Path):
        manager = SessionManager(tmp_path)

        for i in range(5):
            s = _make_session_data(
                session_id=f"web_session_myagent__2024010{i}_x",
                agent_name="myagent",
                updated_at=f"2024-01-0{i + 1}T00:00:00",
            )
            await manager.persistence.save_data(s)

        items = await manager.list_agent_sessions("myagent", limit=2)
        assert len(items) == 2

    @pytest.mark.asyncio
    async def test_empty_result_for_unknown_agent(self, tmp_path: Path):
        manager = SessionManager(tmp_path)
        items = await manager.list_agent_sessions("nonexistent_agent")
        assert items == []


# ===========================================================================
# 4. SessionManager.infer_session_type
# ===========================================================================

class TestInferSessionType:

    def test_heartbeat(self):
        assert SessionManager.infer_session_type("heartbeat_session_myagent") == "heartbeat"

    def test_job(self):
        assert SessionManager.infer_session_type("job_abc123") == "job"

    def test_sub_session(self):
        assert SessionManager.infer_session_type("web_session_myagent__20240101_abc") == "sub"

    def test_primary_session(self):
        assert SessionManager.infer_session_type("web_session_myagent") == "primary"

    def test_empty_string(self):
        assert SessionManager.infer_session_type("") == "primary"

    def test_none(self):
        assert SessionManager.infer_session_type(None) == "primary"


# ===========================================================================
# 5. SessionManager.resolve_agent_name
# ===========================================================================

class TestResolveAgentName:

    def test_primary_web_session(self):
        assert SessionManager.resolve_agent_name("web_session_daily_insight") == "daily_insight"

    def test_sub_web_session(self):
        assert SessionManager.resolve_agent_name("web_session_agent__20240101_abc") == "agent"

    def test_tg_session_returns_none(self):
        assert SessionManager.resolve_agent_name("tg_session_myagent__12345") is None

    def test_random_string_returns_none(self):
        assert SessionManager.resolve_agent_name("something_else") is None


# ===========================================================================
# 6. SessionManager.create_chat_session_id
# ===========================================================================

class TestCreateChatSessionId:

    def test_prefix(self):
        sid = SessionManager.create_chat_session_id("myagent")
        assert sid.startswith("web_session_myagent__")

    def test_contains_timestamp_and_uuid(self):
        sid = SessionManager.create_chat_session_id("myagent")
        suffix = sid[len("web_session_myagent__"):]
        parts = suffix.split("_")
        # timestamp part (14 chars YYYYMMDDHHmmss) + short uuid (8 hex chars)
        assert len(parts) == 2
        assert len(parts[0]) == 14  # timestamp
        assert len(parts[1]) == 8   # uuid hex

    def test_two_calls_produce_different_ids(self):
        sid1 = SessionManager.create_chat_session_id("myagent")
        sid2 = SessionManager.create_chat_session_id("myagent")
        assert sid1 != sid2


# ===========================================================================
# 7. SessionPersistence._postprocess_loaded_data
# ===========================================================================

class TestPostprocessLoadedData:

    def _make_persistence(self, tmp_path: Path) -> SessionPersistence:
        return SessionPersistence(tmp_path)

    def test_legacy_trajectory_events_promoted_to_timeline(self, tmp_path: Path):
        p = self._make_persistence(tmp_path)
        data = {
            "session_id": "web_session_test",
            "agent_name": "test",
            "model_name": "gpt-4",
            "history_messages": [],
            "variables": {},
            "created_at": "2024-01-01T00:00:00",
            "updated_at": "2024-01-01T00:00:00",
            "revision": 1,
            "trajectory_events": [{"type": "turn_start"}],
            # no "timeline" key
        }
        result = p._postprocess_loaded_data(data)
        assert result.timeline == [{"type": "turn_start"}]

    def test_missing_session_type_inferred(self, tmp_path: Path):
        p = self._make_persistence(tmp_path)
        data = {
            "session_id": "heartbeat_session_myagent",
            "agent_name": "myagent",
            "model_name": "gpt-4",
            "history_messages": [],
            "variables": {},
            "created_at": "2024-01-01T00:00:00",
            "updated_at": "2024-01-01T00:00:00",
            "revision": 1,
        }
        result = p._postprocess_loaded_data(data)
        assert result.session_type == "heartbeat"

    def test_missing_state_defaults_to_active(self, tmp_path: Path):
        p = self._make_persistence(tmp_path)
        data = {
            "session_id": "web_session_test",
            "agent_name": "test",
            "model_name": "gpt-4",
            "history_messages": [],
            "variables": {},
            "created_at": "2024-01-01T00:00:00",
            "updated_at": "2024-01-01T00:00:00",
            "revision": 1,
        }
        result = p._postprocess_loaded_data(data)
        assert result.state == "active"

    def test_events_sidecar_removed_from_messages(self, tmp_path: Path):
        p = self._make_persistence(tmp_path)
        data = {
            "session_id": "web_session_test",
            "agent_name": "test",
            "model_name": "gpt-4",
            "history_messages": [
                {"role": "user", "content": "hi", "events": [{"tool": "x"}]},
                {"role": "assistant", "content": "hello"},
            ],
            "variables": {},
            "created_at": "2024-01-01T00:00:00",
            "updated_at": "2024-01-01T00:00:00",
            "revision": 1,
        }
        result = p._postprocess_loaded_data(data)
        for msg in result.history_messages:
            assert "events" not in msg

    def test_missing_mailbox_defaults_to_empty_list(self, tmp_path: Path):
        p = self._make_persistence(tmp_path)
        data = {
            "session_id": "web_session_test",
            "agent_name": "test",
            "model_name": "gpt-4",
            "history_messages": [],
            "variables": {},
            "created_at": "2024-01-01T00:00:00",
            "updated_at": "2024-01-01T00:00:00",
            "revision": 1,
        }
        result = p._postprocess_loaded_data(data)
        assert result.mailbox == []

    def test_missing_context_trace_defaults_to_empty_dict(self, tmp_path: Path):
        p = self._make_persistence(tmp_path)
        data = {
            "session_id": "web_session_test",
            "agent_name": "test",
            "model_name": "gpt-4",
            "history_messages": [],
            "variables": {},
            "created_at": "2024-01-01T00:00:00",
            "updated_at": "2024-01-01T00:00:00",
            "revision": 1,
        }
        result = p._postprocess_loaded_data(data)
        assert result.context_trace == {}

    def test_existing_timeline_preserved(self, tmp_path: Path):
        """When timeline already exists, trajectory_events should not overwrite it."""
        p = self._make_persistence(tmp_path)
        data = {
            "session_id": "web_session_test",
            "agent_name": "test",
            "model_name": "gpt-4",
            "history_messages": [],
            "variables": {},
            "created_at": "2024-01-01T00:00:00",
            "updated_at": "2024-01-01T00:00:00",
            "revision": 1,
            "timeline": [{"type": "new_event"}],
            "trajectory_events": [{"type": "old_event"}],
        }
        result = p._postprocess_loaded_data(data)
        assert result.timeline == [{"type": "new_event"}]


# ===========================================================================
# 8. SessionPersistence.is_safe_session_id
# ===========================================================================

class TestIsSafeSessionId:

    def test_valid_web_session(self):
        assert SessionPersistence.is_safe_session_id("web_session_agent") is True

    def test_valid_heartbeat_session(self):
        assert SessionPersistence.is_safe_session_id("heartbeat_session_agent") is True

    def test_valid_job(self):
        assert SessionPersistence.is_safe_session_id("job_abc123") is True

    def test_valid_with_dots_and_dashes(self):
        assert SessionPersistence.is_safe_session_id("session-name.v2") is True

    def test_invalid_empty_string(self):
        assert SessionPersistence.is_safe_session_id("") is False

    def test_invalid_none(self):
        assert SessionPersistence.is_safe_session_id(None) is False

    def test_invalid_path_traversal(self):
        assert SessionPersistence.is_safe_session_id("../traversal") is False

    def test_invalid_spaces(self):
        assert SessionPersistence.is_safe_session_id("has spaces") is False

    def test_invalid_slash(self):
        assert SessionPersistence.is_safe_session_id("has/slash") is False

    def test_invalid_special_chars(self):
        assert SessionPersistence.is_safe_session_id("bad;chars") is False


# ===========================================================================
# 9. SessionManager.save_session with lock_already_held=True
# ===========================================================================

class TestSaveSessionLockAlreadyHeld:

    @pytest.mark.asyncio
    async def test_calls_persistence_save_directly(self, tmp_path: Path):
        manager = SessionManager(tmp_path)
        agent = _make_mock_agent()

        # Spy on persistence.save to confirm it's called
        original_save = manager.persistence.save
        save_called = {"value": False}

        async def fake_save(*args, **kwargs):
            save_called["value"] = True

        manager.persistence.save = fake_save

        # Spy on update_atomic to confirm it's NOT called
        update_atomic_called = {"value": False}
        original_update_atomic = manager.update_atomic

        async def fake_update_atomic(*args, **kwargs):
            update_atomic_called["value"] = True
            return None

        manager.update_atomic = fake_update_atomic

        await manager.save_session(
            "web_session_test_agent",
            agent,
            lock_already_held=True,
        )

        assert save_called["value"] is True
        assert update_atomic_called["value"] is False


# ===========================================================================
# 10. SessionManager.restore_to_agent history truncation
# ===========================================================================

class TestRestoreToAgentTruncation:

    @pytest.mark.asyncio
    async def test_history_truncated_when_exceeding_max(self, tmp_path: Path):
        manager = SessionManager(tmp_path)
        max_msgs = SessionPersistence.MAX_RESTORED_HISTORY_MESSAGES  # 120

        # Create a session with more than MAX_RESTORED_HISTORY_MESSAGES
        large_history = [{"role": "user", "content": f"msg {i}"} for i in range(200)]
        session_data = _make_session_data(
            session_id="web_session_test_agent",
            history_messages=large_history,
        )

        agent = _make_mock_agent()

        await manager.restore_to_agent(agent, session_data)

        # Verify import_portable_session was called
        agent.snapshot.import_portable_session.assert_called_once()
        call_args = agent.snapshot.import_portable_session.call_args
        portable = call_args[0][0]
        restored_history = portable["history_messages"]

        # Should be truncated to MAX_RESTORED_HISTORY_MESSAGES
        assert len(restored_history) <= max_msgs

    @pytest.mark.asyncio
    async def test_history_not_truncated_when_within_limit(self, tmp_path: Path):
        manager = SessionManager(tmp_path)

        small_history = [{"role": "user", "content": f"msg {i}"} for i in range(10)]
        session_data = _make_session_data(
            session_id="web_session_test_agent",
            history_messages=small_history,
        )

        agent = _make_mock_agent()

        await manager.restore_to_agent(agent, session_data)

        agent.snapshot.import_portable_session.assert_called_once()
        call_args = agent.snapshot.import_portable_session.call_args
        portable = call_args[0][0]
        restored_history = portable["history_messages"]

        assert len(restored_history) == 10

    @pytest.mark.asyncio
    async def test_workspace_instructions_filtered(self, tmp_path: Path):
        manager = SessionManager(tmp_path)

        session_data = _make_session_data(
            session_id="web_session_test_agent",
            history_messages=[{"role": "user", "content": "hi"}],
            variables={"workspace_instructions": "should be filtered", "keep_me": "yes"},
        )

        agent = _make_mock_agent()

        await manager.restore_to_agent(agent, session_data)

        call_args = agent.snapshot.import_portable_session.call_args
        portable = call_args[0][0]
        assert "workspace_instructions" not in portable["variables"]
        assert portable["variables"]["keep_me"] == "yes"

    @pytest.mark.asyncio
    async def test_repair_flag_passed(self, tmp_path: Path):
        manager = SessionManager(tmp_path)

        session_data = _make_session_data(
            session_id="web_session_test_agent",
            history_messages=[],
        )

        agent = _make_mock_agent()
        await manager.restore_to_agent(agent, session_data)

        call_args = agent.snapshot.import_portable_session.call_args
        assert call_args[1].get("repair") is True or (len(call_args[0]) > 1 and call_args[0][1] is True)
