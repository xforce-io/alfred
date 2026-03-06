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
# 4. SessionManager.get_last_activity_time
# ===========================================================================

class TestGetLastActivityTime:

    @pytest.mark.asyncio
    async def test_prefers_latest_channel_session_activity(self, tmp_path: Path):
        manager = SessionManager(tmp_path)

        old = datetime(2024, 1, 1, tzinfo=timezone.utc)
        new = datetime(2024, 1, 1, 0, 30, tzinfo=timezone.utc)

        primary = _make_session_data(
            session_id="web_session_demo_agent",
            agent_name="demo_agent",
            updated_at=old.isoformat(),
            created_at=old.isoformat(),
        )
        tg = _make_session_data(
            session_id="tg_session_demo_agent__12345",
            agent_name="demo_agent",
            updated_at=new.isoformat(),
            created_at=new.isoformat(),
        )

        await manager.persistence.save_data(primary)
        await manager.persistence.save_data(tg)

        last = manager.get_last_activity_time("demo_agent")

        assert last is not None
        assert last == new.timestamp()

    @pytest.mark.asyncio
    async def test_ignores_heartbeat_and_job_sessions(self, tmp_path: Path):
        manager = SessionManager(tmp_path)

        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        primary = _make_session_data(
            session_id="web_session_demo_agent",
            agent_name="demo_agent",
            updated_at=base.isoformat(),
            created_at=base.isoformat(),
        )
        heartbeat = _make_session_data(
            session_id="heartbeat_session_demo_agent",
            agent_name="demo_agent",
            session_type="heartbeat",
            updated_at=(base.replace(hour=1)).isoformat(),
            created_at=(base.replace(hour=1)).isoformat(),
        )
        job = _make_session_data(
            session_id="job_123",
            agent_name="demo_agent",
            session_type="job",
            updated_at=(base.replace(hour=2)).isoformat(),
            created_at=(base.replace(hour=2)).isoformat(),
        )

        await manager.persistence.save_data(primary)
        await manager.persistence.save_data(heartbeat)
        await manager.persistence.save_data(job)

        last = manager.get_last_activity_time("demo_agent")

        assert last == base.timestamp()


# ===========================================================================
# 5. SessionManager.infer_session_type
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
# 6. SessionManager.resolve_agent_name
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
# 7. SessionManager.create_chat_session_id
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


# ===========================================================================
# 11. Lock event loop safety
# ===========================================================================

class TestLockEventLoopSafety:

    def test_get_lock_recreates_on_different_event_loop(self, tmp_path: Path):
        """Lock created in one event loop must be replaced when accessed from a different loop."""
        import asyncio

        manager = SessionManager(tmp_path)
        session_id = "web_session_test_agent"

        # Create a lock in one event loop
        loop1 = asyncio.new_event_loop()
        lock1 = loop1.run_until_complete(self._get_lock_async(manager, session_id))
        loop1.close()

        # Access lock from a different event loop — should NOT return the stale lock
        loop2 = asyncio.new_event_loop()
        lock2 = loop2.run_until_complete(self._get_lock_async(manager, session_id))
        loop2.close()

        assert lock1 is not lock2, "Lock should be recreated when event loop changes"

    @staticmethod
    async def _get_lock_async(manager: SessionManager, session_id: str) -> "asyncio.Lock":
        return manager._get_lock(session_id)

    @pytest.mark.asyncio
    async def test_acquire_session_survives_loop_change(self, tmp_path: Path):
        """acquire_session should work even after event loop has changed."""
        import asyncio

        manager = SessionManager(tmp_path)
        session_id = "web_session_test_agent"

        # Simulate a stale lock from a previous (now-dead) loop by injecting
        # a tuple with a different loop object directly into the cache.
        stale_loop = asyncio.new_event_loop()
        stale_lock = asyncio.Lock()  # created in current loop but paired with stale_loop
        manager._locks[session_id] = (stale_loop, stale_lock)
        stale_loop.close()

        # Now acquire in current (different) loop — should not raise
        acquired = await manager.acquire_session(session_id, timeout=2.0)
        assert acquired is True
        manager.release_session(session_id)


# ===========================================================================
# 12. _history variable deduplication
# ===========================================================================

class TestHistoryVariableDedup:

    @pytest.mark.asyncio
    async def test_save_strips_history_variable(self, tmp_path: Path):
        """When dolphin exports _history in variables, persistence.save should strip it."""
        from src.everbot.core.session.session import SessionPersistence

        persistence = SessionPersistence(tmp_path)
        session_id = "web_session_test_agent"

        agent = _make_mock_agent(
            history_messages=[{"role": "user", "content": "hi"}],
            variables={
                "_history": [{"role": "user", "content": "hi"}],  # duplicate!
                "model_name": "gpt-4",
            },
        )

        await persistence.save(session_id, agent)

        loaded = await persistence.load(session_id)
        assert loaded is not None
        assert "_history" not in loaded.variables, \
            "_history should not be stored in variables (it duplicates history_messages)"

    @pytest.mark.asyncio
    async def test_restore_filters_history_variable(self, tmp_path: Path):
        """restore_to_agent should not pass _history in variables to import_portable_session."""
        manager = SessionManager(tmp_path)

        session_data = _make_session_data(
            session_id="web_session_test_agent",
            history_messages=[{"role": "user", "content": "hi"}],
            variables={
                "_history": [{"role": "user", "content": "hi"}],
                "model_name": "gpt-4",
            },
        )

        agent = _make_mock_agent()
        await manager.restore_to_agent(agent, session_data)

        call_args = agent.snapshot.import_portable_session.call_args
        portable = call_args[0][0]
        assert "_history" not in portable["variables"], \
            "_history should be filtered from restored variables"


# ===========================================================================
# 13. inject_history_message deduplication by run_id
# ===========================================================================

class TestUserMessageContentDedup:
    """Issue 1: User messages without run_id (e.g. from frontend) can be
    submitted multiple times and each copy is blindly appended because the
    dedup logic only checks metadata.run_id.  These tests prove the bug."""

    @pytest.mark.asyncio
    async def test_identical_user_messages_without_run_id_deduplicated(self, tmp_path: Path):
        """Submitting the same user message 10 times (no metadata.run_id)
        should NOT create 10 entries — at minimum consecutive duplicates
        with identical content should be collapsed."""
        manager = SessionManager(tmp_path)
        session_id = "web_session_test_agent"

        for _ in range(10):
            await manager.inject_history_message(
                session_id, {"role": "user", "content": "你好"}
            )

        loaded = await manager.load_session(session_id)
        assert loaded is not None
        user_messages = [
            m for m in loaded.history_messages if m.get("role") == "user"
        ]
        assert len(user_messages) <= 2, (
            f"Expected at most 2 user messages (with placeholders), got {len(user_messages)}. "
            "inject_history_message does not deduplicate user messages without run_id — "
            "identical '你好' was appended 10 times."
        )

    @pytest.mark.asyncio
    async def test_consecutive_identical_user_messages_collapsed(self, tmp_path: Path):
        """Two consecutive user messages with the exact same content should be
        collapsed into one (the second is a frontend re-submit)."""
        manager = SessionManager(tmp_path)
        session_id = "web_session_test_agent"

        # Normal flow: user → assistant → user (same content = re-submit)
        await manager.inject_history_message(
            session_id, {"role": "user", "content": "帮我搜索一下 Anthropic 最新的新闻"}
        )
        await manager.inject_history_message(
            session_id, {"role": "assistant", "content": "好的，正在搜索..."}
        )
        # Frontend re-submits the same message (network glitch, double-click, etc.)
        await manager.inject_history_message(
            session_id, {"role": "user", "content": "帮我搜索一下 Anthropic 最新的新闻"}
        )
        await manager.inject_history_message(
            session_id, {"role": "user", "content": "帮我搜索一下 Anthropic 最新的新闻"}
        )

        loaded = await manager.load_session(session_id)
        assert loaded is not None
        search_messages = [
            m for m in loaded.history_messages
            if m.get("role") == "user" and m.get("content") == "帮我搜索一下 Anthropic 最新的新闻"
        ]
        # The first one after assistant reply is valid; the immediate duplicate is not
        assert len(search_messages) <= 2, (
            f"Expected at most 2 user messages for the same query, got {len(search_messages)}. "
            "Consecutive identical user messages should be collapsed."
        )

    @pytest.mark.asyncio
    async def test_non_consecutive_duplicate_user_message_rejected(self, tmp_path: Path):
        """Production bug: same user message sent 8 minutes apart (separated by
        assistant reply + tool calls) is NOT deduplicated because the current
        code only checks consecutive identical user messages.

        Scenario from production trajectory:
            11:11:59 user: "帮我注册一个定时任务：每两分钟检测一次会话..."
            11:12:xx assistant: (processes request, registers routine)
            11:19:50 user: "帮我注册一个定时任务：每两分钟检测一次会话..."  ← DUPLICATE

        The second submission should be rejected because it's identical content
        within a short time window, but inject_history_message only deduplicates
        CONSECUTIVE identical user messages (last_msg.role == "user" && same content).
        When an assistant reply sits in between, the duplicate slips through.
        """
        manager = SessionManager(tmp_path)
        session_id = "web_session_test_agent"

        duplicate_content = "帮我注册一个定时任务：每两分钟检测一次会话，看会话轨迹是否有问题"

        # First: user sends the message
        await manager.inject_history_message(
            session_id, {"role": "user", "content": duplicate_content}
        )
        # Agent processes and replies
        await manager.inject_history_message(
            session_id, {"role": "assistant", "content": "好的，我已经帮你注册了定时任务。"}
        )
        # 8 minutes later: same message re-sent (frontend glitch / network retry)
        await manager.inject_history_message(
            session_id, {"role": "user", "content": duplicate_content}
        )

        loaded = await manager.load_session(session_id)
        assert loaded is not None
        duplicate_messages = [
            m for m in loaded.history_messages
            if m.get("role") == "user" and m.get("content") == duplicate_content
        ]
        assert len(duplicate_messages) == 1, (
            f"Expected 1 user message, got {len(duplicate_messages)}. "
            "Non-consecutive duplicate user message was not rejected. "
            "inject_history_message only deduplicates CONSECUTIVE identical user "
            "messages — when an assistant reply separates them, the duplicate "
            "slips through, causing 'duplicate routine detected' errors downstream."
        )


    @pytest.mark.asyncio
    async def test_distant_duplicate_user_message_allowed(self, tmp_path: Path):
        """User messages identical to very old history should NOT be blocked.

        The dedup window is limited to the last N user messages so that
        legitimate repeated messages (e.g. "你好") are not silently dropped.
        """
        manager = SessionManager(tmp_path)
        session_id = "web_session_test_agent"

        repeated_content = "你好"

        # First occurrence
        await manager.inject_history_message(
            session_id, {"role": "user", "content": repeated_content}
        )
        await manager.inject_history_message(
            session_id, {"role": "assistant", "content": "你好！"}
        )
        # Inject enough distinct user messages to push the first "你好" out of
        # the dedup window (window = 5)
        for i in range(6):
            await manager.inject_history_message(
                session_id, {"role": "user", "content": f"问题 {i}"}
            )
            await manager.inject_history_message(
                session_id, {"role": "assistant", "content": f"回答 {i}"}
            )
        # Now send "你好" again — should be accepted (outside dedup window)
        await manager.inject_history_message(
            session_id, {"role": "user", "content": repeated_content}
        )

        loaded = await manager.load_session(session_id)
        assert loaded is not None
        hello_messages = [
            m for m in loaded.history_messages
            if m.get("role") == "user" and m.get("content") == repeated_content
        ]
        assert len(hello_messages) == 2, (
            f"Expected 2 '你好' messages, got {len(hello_messages)}. "
            "Distant duplicate user messages outside the dedup window should be allowed."
        )


class TestInjectHistoryMessageDedup:
    """inject_history_message should deduplicate messages with the same
    metadata.run_id.  Currently it does NOT, so these tests are expected to
    FAIL – proving the bug exists."""

    @pytest.mark.asyncio
    async def test_duplicate_deferred_result_not_appended(self, tmp_path: Path):
        """Injecting the same deferred result (same run_id) twice should NOT
        create two entries in history_messages."""
        manager = SessionManager(tmp_path)
        session_id = "web_session_test_agent"

        msg = {
            "role": "assistant",
            "content": "[此消息由超时后台任务完成后自动生成]\n\n每日投资信号推送 completed",
            "metadata": {
                "source": "deferred_result",
                "run_id": "run_abc123",
                "injected_at": "2024-06-01T00:00:00+00:00",
            },
        }

        await manager.inject_history_message(session_id, msg)
        await manager.inject_history_message(session_id, msg)
        await manager.inject_history_message(session_id, msg)

        loaded = await manager.load_session(session_id)
        assert loaded is not None
        matching = [
            m for m in loaded.history_messages
            if isinstance(m.get("metadata"), dict)
            and m["metadata"].get("run_id") == "run_abc123"
        ]
        assert len(matching) == 1, (
            f"Expected 1 message with run_id=run_abc123, got {len(matching)}. "
            "inject_history_message lacks deduplication by run_id."
        )

    @pytest.mark.asyncio
    async def test_duplicate_heartbeat_result_not_appended(self, tmp_path: Path):
        """Injecting the same heartbeat result (same run_id) multiple times
        should only keep one copy."""
        manager = SessionManager(tmp_path)
        session_id = "web_session_test_agent"

        for i in range(5):
            msg = {
                "role": "assistant",
                "content": "[此消息由心跳系统自动执行例行任务生成]\n\n每日投资信号推送 completed",
                "metadata": {
                    "source": "heartbeat",
                    "run_id": "hb_run_001",
                    "injected_at": f"2024-06-01T00:00:0{i}+00:00",
                },
            }
            await manager.inject_history_message(session_id, msg)

        loaded = await manager.load_session(session_id)
        assert loaded is not None
        matching = [
            m for m in loaded.history_messages
            if isinstance(m.get("metadata"), dict)
            and m["metadata"].get("run_id") == "hb_run_001"
        ]
        assert len(matching) == 1, (
            f"Expected 1 message with run_id=hb_run_001, got {len(matching)}. "
            "Heartbeat result was injected {len(matching)} times without dedup."
        )

    @pytest.mark.asyncio
    async def test_different_run_ids_both_kept(self, tmp_path: Path):
        """Messages with different run_ids should both be kept."""
        manager = SessionManager(tmp_path)
        session_id = "web_session_test_agent"

        for run_id in ["run_aaa", "run_bbb"]:
            msg = {
                "role": "assistant",
                "content": f"Result from {run_id}",
                "metadata": {"source": "deferred_result", "run_id": run_id},
            }
            await manager.inject_history_message(session_id, msg)

        loaded = await manager.load_session(session_id)
        assert loaded is not None
        assert len(loaded.history_messages) == 2


# ===========================================================================
# 14. Consecutive user messages need assistant placeholder
# ===========================================================================

class TestConsecutiveUserMessageGuard:
    """When a user-role message is injected into history and the last message
    is also user-role, inject_history_message should insert a placeholder
    assistant message in between.  Currently it does NOT, so these tests
    are expected to FAIL."""

    @pytest.mark.asyncio
    async def test_no_consecutive_user_messages_after_injection(self, tmp_path: Path):
        """Injecting a user-role message right after another user-role message
        must insert an assistant placeholder to avoid consecutive user messages."""
        manager = SessionManager(tmp_path)
        session_id = "web_session_test_agent"

        # First inject a user message
        await manager.inject_history_message(
            session_id, {"role": "user", "content": "Hello"}
        )
        # Then inject another user message (e.g. a Background Updates notification)
        await manager.inject_history_message(
            session_id,
            {
                "role": "user",
                "content": "## Background Updates\n- [job_completed] 每日投资信号推送 completed",
            },
        )

        loaded = await manager.load_session(session_id)
        assert loaded is not None
        messages = loaded.history_messages
        for i in range(len(messages) - 1):
            if messages[i].get("role") == "user" and messages[i + 1].get("role") == "user":
                pytest.fail(
                    f"Consecutive user messages at index [{i},{i+1}]: "
                    f"'{messages[i].get('content', '')[:40]}' → "
                    f"'{messages[i+1].get('content', '')[:40]}'. "
                    "A placeholder assistant message should be inserted between them."
                )

    @pytest.mark.asyncio
    async def test_history_alternation_after_multiple_injections(self, tmp_path: Path):
        """After multiple mixed-role injections, no two consecutive messages
        should have the same role (user-user or assistant-assistant without
        tool_calls)."""
        manager = SessionManager(tmp_path)
        session_id = "web_session_test_agent"

        # Simulate: user msg → heartbeat assistant → user msg → another user
        # (the last pair is the problematic one)
        injections = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！有什么可以帮助你的？"},
            {"role": "user", "content": "查看今日新闻"},
            {"role": "user", "content": "## Background Updates\n- [job_completed] 每日投资信号推送 completed"},
        ]
        for msg in injections:
            await manager.inject_history_message(session_id, msg)

        loaded = await manager.load_session(session_id)
        assert loaded is not None
        messages = loaded.history_messages
        for i in range(len(messages) - 1):
            curr_role = messages[i].get("role")
            next_role = messages[i + 1].get("role")
            if curr_role == "user" and next_role == "user":
                pytest.fail(
                    f"Consecutive user messages at index [{i},{i+1}]. "
                    "inject_history_message should insert an assistant placeholder."
                )


# ===========================================================================
# 15. Heartbeat assistant response must not appear as reply to user question
# ===========================================================================

class TestHeartbeatResponseDoesNotPollutePrimaryConversation:
    """Issue 2: When heartbeat injects an assistant message into the primary
    session's history_messages, it can appear immediately after a user message,
    making the LLM think the heartbeat output is the response to that user
    question.  The injected heartbeat message must NOT break the user→assistant
    reply pairing.

    Example from production:
        [60] user: 帮我搜索一下 Anthropic 最新的新闻
        [61] assistant: [此消息由心跳系统自动执行例行任务生成] Heartbeat reflection...
    """

    @pytest.mark.asyncio
    async def test_heartbeat_injection_after_unanswered_user_message(self, tmp_path: Path):
        """If the last message is a user question and a heartbeat assistant
        message is injected, the heartbeat should NOT be placed directly after
        the user question (it would look like the answer)."""
        manager = SessionManager(tmp_path)
        session_id = "web_session_test_agent"

        # User asked a question
        await manager.inject_history_message(
            session_id,
            {"role": "user", "content": "帮我搜索一下 Anthropic 最新的新闻"},
        )

        # Heartbeat injects its result (assistant role) into the same session
        heartbeat_msg = {
            "role": "assistant",
            "content": "[此消息由心跳系统自动执行例行任务生成] Heartbeat reflection proposed 1 routine(s)",
            "metadata": {
                "source": "heartbeat",
                "run_id": "hb_run_999",
            },
        }
        await manager.inject_history_message(session_id, heartbeat_msg)

        loaded = await manager.load_session(session_id)
        assert loaded is not None
        messages = loaded.history_messages

        # Find the user question
        user_q_idx = None
        for i, m in enumerate(messages):
            if m.get("content", "").startswith("帮我搜索"):
                user_q_idx = i
                break
        assert user_q_idx is not None

        # The message immediately after the user question should NOT be the
        # heartbeat message — it would confuse the LLM into thinking heartbeat
        # output is the search result.
        next_msg = messages[user_q_idx + 1] if user_q_idx + 1 < len(messages) else None
        assert next_msg is not None, "Expected a message after the user question"
        is_heartbeat = (
            isinstance(next_msg.get("metadata"), dict)
            and next_msg["metadata"].get("source") == "heartbeat"
        )
        assert not is_heartbeat, (
            "Heartbeat assistant message was injected directly after an unanswered "
            "user question. This makes the LLM think the heartbeat output is the "
            "reply to the user's question. Heartbeat injection must defer or insert "
            "a separator when the last message is an unanswered user message."
        )

    @pytest.mark.asyncio
    async def test_heartbeat_injection_preserves_conversation_coherence(self, tmp_path: Path):
        """Simulates the production scenario: user asks → before the chat agent
        can reply, heartbeat injects an assistant message → next chat turn sees
        the heartbeat message as the 'reply' to the user's question."""
        manager = SessionManager(tmp_path)
        session_id = "web_session_test_agent"

        # Build a conversation history
        history = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！有什么可以帮助你的？"},
            {"role": "user", "content": "帮我搜索一下 Anthropic 最新的新闻"},
            # At this point, the chat agent hasn't replied yet.
            # Heartbeat runs and injects its result:
        ]
        for msg in history:
            await manager.inject_history_message(session_id, msg)

        # Heartbeat injects
        await manager.inject_history_message(session_id, {
            "role": "assistant",
            "content": "[此消息由心跳系统自动执行例行任务生成] Heartbeat reflection proposed 1 routine(s)",
            "metadata": {"source": "heartbeat", "run_id": "hb_coherence_test"},
        })

        loaded = await manager.load_session(session_id)
        messages = loaded.history_messages

        # Check: the assistant message after "帮我搜索" should not be heartbeat
        for i in range(len(messages) - 1):
            if (
                messages[i].get("role") == "user"
                and "搜索" in messages[i].get("content", "")
            ):
                next_msg = messages[i + 1]
                if next_msg.get("role") == "assistant":
                    content = next_msg.get("content", "")
                    assert "心跳" not in content and "Heartbeat" not in content, (
                        f"Heartbeat response at index [{i+1}] appears as reply to "
                        f"user question at [{i}]. This breaks conversation coherence."
                    )
                break


# ===========================================================================
# 16. Isolated job results must not pollute primary session restore context
# ===========================================================================

class TestIsolatedJobResultNotRestoredToLLMContext:
    """Production bug: isolated routine results (heartbeat-injected assistant
    messages) are persisted in primary session's history_messages. When
    restore_to_agent loads the session for the next chat turn, these heartbeat
    messages appear in the LLM context as if they were normal conversation
    messages.

    Example from production history_messages:
        [0] user: "你好"
        [1] assistant: "[此消息由心跳系统自动执行例行任务生成] MicroStrategy 吸引子分析..."
        [2] user: "帮我搜索 Anthropic 新闻"
        [3] assistant: "[此消息由心跳系统自动执行例行任务生成] 会话轨迹健康检测..."

    The LLM sees these heartbeat results as part of the conversation, which:
    - Confuses the model about what the user is asking
    - Wastes context window with irrelevant heartbeat output
    - Can cause the model to reference heartbeat analysis in unrelated replies
    """

    @pytest.mark.asyncio
    async def test_heartbeat_messages_filtered_during_restore(self, tmp_path: Path):
        """restore_to_agent should filter out heartbeat-injected messages so
        the LLM only sees real user-assistant conversation turns."""
        manager = SessionManager(tmp_path)
        session_id = "web_session_test_agent"

        # Build a session with heartbeat messages mixed into conversation
        session_data = _make_session_data(
            session_id=session_id,
            history_messages=[
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "你好！有什么可以帮助你的？"},
                # Heartbeat result injected by _inject_result_to_primary_history
                {"role": "assistant", "content": "(acknowledged)"},
                {"role": "user", "content": "[Background notification follows]"},
                {
                    "role": "assistant",
                    "content": "[此消息由心跳系统自动执行例行任务生成]\n\nMicroStrategy 吸引子分析：当前价格...",
                    "metadata": {"source": "heartbeat", "run_id": "hb_001"},
                },
                # Another real conversation turn
                {"role": "user", "content": "帮我搜索 Anthropic 最新的新闻"},
                # Another heartbeat injection
                {"role": "assistant", "content": "(acknowledged)"},
                {"role": "user", "content": "[Background notification follows]"},
                {
                    "role": "assistant",
                    "content": "[此消息由心跳系统自动执行例行任务生成]\n\n会话轨迹健康检测结果...",
                    "metadata": {"source": "heartbeat", "run_id": "hb_002"},
                },
            ],
        )
        await manager.persistence.save_data(session_data)

        agent = _make_mock_agent()
        await manager.restore_to_agent(agent, session_data)

        # Check what was passed to import_portable_session
        call_args = agent.snapshot.import_portable_session.call_args
        portable = call_args[0][0]
        restored_history = portable["history_messages"]

        # Heartbeat messages should NOT be in the restored context
        heartbeat_messages = [
            m for m in restored_history
            if isinstance(m, dict)
            and isinstance(m.get("metadata"), dict)
            and m["metadata"].get("source") == "heartbeat"
        ]
        assert len(heartbeat_messages) == 0, (
            f"Found {len(heartbeat_messages)} heartbeat message(s) in restored history. "
            "Heartbeat-injected messages should be filtered during restore_to_agent "
            "to prevent isolated job results from polluting the LLM conversation context."
        )

    @pytest.mark.asyncio
    async def test_heartbeat_placeholder_messages_filtered_during_restore(self, tmp_path: Path):
        """The '(acknowledged)' and '[Background notification follows]' placeholder
        messages inserted by inject_history_message should also be filtered during
        restore, as they are artifacts of the heartbeat injection mechanism."""
        manager = SessionManager(tmp_path)
        session_id = "web_session_test_agent"

        session_data = _make_session_data(
            session_id=session_id,
            history_messages=[
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "你好！"},
                {"role": "assistant", "content": "(acknowledged)"},
                {"role": "user", "content": "[Background notification follows]"},
                {
                    "role": "assistant",
                    "content": "[此消息由心跳系统自动执行例行任务生成]\n\n分析结果...",
                    "metadata": {"source": "heartbeat", "run_id": "hb_003"},
                },
                {"role": "user", "content": "继续"},
            ],
        )
        await manager.persistence.save_data(session_data)

        agent = _make_mock_agent()
        await manager.restore_to_agent(agent, session_data)

        call_args = agent.snapshot.import_portable_session.call_args
        portable = call_args[0][0]
        restored_history = portable["history_messages"]

        # Check no placeholder artifacts remain
        placeholder_messages = [
            m for m in restored_history
            if isinstance(m, dict) and m.get("content") in (
                "(acknowledged)", "[Background notification follows]"
            )
        ]
        assert len(placeholder_messages) == 0, (
            f"Found {len(placeholder_messages)} placeholder message(s) in restored history. "
            "'(acknowledged)' and '[Background notification follows]' are artifacts of "
            "heartbeat injection and should be filtered during restore to keep the LLM "
            "context clean."
        )

    @pytest.mark.asyncio
    async def test_multimodal_list_content_not_crash_filter(self, tmp_path: Path):
        """Messages with list-typed content (multimodal format) must not crash
        the heartbeat filter with 'TypeError: unhashable type: list'."""
        manager = SessionManager(tmp_path)
        session_id = "web_session_test_agent"

        multimodal_content = [{"type": "text", "text": "你好"}, {"type": "image_url", "image_url": {"url": "https://example.com/img.png"}}]
        session_data = _make_session_data(
            session_id=session_id,
            history_messages=[
                {"role": "user", "content": multimodal_content},
                {"role": "assistant", "content": "这是一张图片"},
                {"role": "assistant", "content": "(acknowledged)"},
                {"role": "user", "content": "[Background notification follows]"},
                {
                    "role": "assistant",
                    "content": "[此消息由心跳系统自动执行例行任务生成]\n\n结果...",
                    "metadata": {"source": "heartbeat", "run_id": "hb_004"},
                },
            ],
        )
        await manager.persistence.save_data(session_data)

        agent = _make_mock_agent()
        await manager.restore_to_agent(agent, session_data)

        call_args = agent.snapshot.import_portable_session.call_args
        portable = call_args[0][0]
        restored_history = portable["history_messages"]

        # The multimodal message should be preserved
        assert any(
            isinstance(m, dict) and m.get("content") == multimodal_content
            for m in restored_history
        ), "Multimodal message with list content should be preserved after filtering"
        # Heartbeat and placeholders should still be filtered
        assert len(restored_history) == 2, (
            f"Expected 2 messages (multimodal user + assistant reply), got {len(restored_history)}"
        )
