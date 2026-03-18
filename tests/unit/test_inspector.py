"""Unit tests for Inspector — heartbeat reflection observation engine."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.everbot.core.runtime.inspector import (
    Inspector,
    InspectionResult,
    InspectionContext,
    INSPECTOR_STATE_FILE,
    emit_push_message,
)
from src.everbot.core.runtime.reflection import ReflectionManager
from src.everbot.core.tasks.routine_manager import RoutineManager


class _StubUserDataManager:
    """Test double for user data paths."""

    def __init__(self, base_dir: Path):
        self._base_dir = base_dir

    def get_agent_tmp_dir(self, agent_name: str) -> Path:
        return self._base_dir / agent_name / "tmp"

    @property
    def heartbeat_events_file(self) -> Path:
        return self._base_dir / "logs" / "heartbeat_events.jsonl"


def _make_inspector(tmp_path: Path, **overrides) -> Inspector:
    """Create an Inspector with sensible defaults for testing."""
    defaults = dict(
        agent_name="test_agent",
        workspace_path=tmp_path,
        routine_manager=RoutineManager(tmp_path),
        auto_register_routines=False,
        inspect_force_interval_hours=24,
    )
    defaults.update(overrides)
    with patch(
        "src.everbot.core.runtime.inspector.get_user_data_manager",
        return_value=_StubUserDataManager(tmp_path / ".alfred"),
    ):
        return Inspector(**defaults)


def _make_reflection_response_with_proposals(proposals: list[dict]) -> str:
    """Build an LLM response containing a JSON routine proposal block."""
    payload = json.dumps({"routines": proposals}, ensure_ascii=False, indent=2)
    return f"Here are my suggestions:\n```json\n{payload}\n```"


def _make_reflection_response_no_proposals() -> str:
    """Build an LLM response with no routine proposals."""
    return "Everything looks good. No new routines needed."


def _make_reflection_response_v2_format(
    heartbeat_ok: bool = True,
    push_message: str = None,
    routines: list = None,
) -> str:
    """Build an LLM response with the unified JSON format."""
    payload = {
        "heartbeat_ok": heartbeat_ok,
        "push_message": push_message,
        "routines": routines or [],
    }
    return f"```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```"


# ── should_skip / _should_inspect ────────────────────────────


class TestShouldSkip:
    """Tests for Inspector.should_skip() — file hash + time interval logic."""

    def test_skip_when_files_unchanged(self, tmp_path):
        """After update_state, should_skip returns True if files haven't changed."""
        (tmp_path / "MEMORY.md").write_text("some memory content")
        (tmp_path / "HEARTBEAT.md").write_text("some heartbeat content")

        inspector = _make_inspector(tmp_path)
        assert inspector.should_skip() is False

        inspector.update_state()
        assert inspector.should_skip() is True

    def test_no_skip_when_force_interval_elapsed(self, tmp_path):
        """When force interval has elapsed, should_skip returns False."""
        (tmp_path / "MEMORY.md").write_text("content")
        (tmp_path / "HEARTBEAT.md").write_text("content")

        inspector = _make_inspector(tmp_path, inspect_force_interval_hours=1)
        inspector.update_state()

        inspector._reflection.last_reflect_at = datetime.now() - timedelta(hours=2)
        assert inspector.should_skip() is False

    def test_no_skip_when_files_changed(self, tmp_path):
        """When files change after update_state, should_skip returns False."""
        (tmp_path / "MEMORY.md").write_text("v1")
        (tmp_path / "HEARTBEAT.md").write_text("v1")

        inspector = _make_inspector(tmp_path)
        inspector.update_state()

        (tmp_path / "MEMORY.md").write_text("v2 - updated")
        assert inspector.should_skip() is False

    def test_no_skip_when_context_file_deleted(self, tmp_path):
        """Deleting a previously tracked context file should trigger inspection."""
        (tmp_path / "MEMORY.md").write_text("v1")
        (tmp_path / "HEARTBEAT.md").write_text("v1")

        inspector = _make_inspector(tmp_path)
        inspector.update_state()

        (tmp_path / "MEMORY.md").unlink()
        assert inspector.should_skip() is False

    def test_no_skip_when_never_reflected(self, tmp_path):
        """First call should never skip."""
        inspector = _make_inspector(tmp_path)
        assert inspector.should_skip() is False

    def test_should_skip_includes_session_context(self, tmp_path):
        """should_skip should use session summary for precheck decisions."""
        (tmp_path / "MEMORY.md").write_text("memory")
        (tmp_path / "HEARTBEAT.md").write_text("heartbeat")

        inspector = _make_inspector(tmp_path)
        session_manager = MagicMock()
        session_manager.get_session_summary = MagicMock(return_value="summary v1")

        assert inspector.should_skip(
            session_manager=session_manager,
            primary_session_id="primary_1",
        ) is False

        ctx = inspector._gather_context(
            "heartbeat",
            session_manager=session_manager,
            primary_session_id="primary_1",
        )
        inspector.update_state(ctx, InspectionResult())

        session_manager.get_session_summary.return_value = "summary v2"

        assert inspector.should_skip(
            session_manager=session_manager,
            primary_session_id="primary_1",
        ) is False


class TestEmitPushMessage:
    @pytest.mark.asyncio
    async def test_session_scope_emit_sets_target_session_id(self, monkeypatch):
        emitted = []

        async def _fake_emit(source_session_id, data, **kwargs):
            emitted.append((source_session_id, data, kwargs))

        monkeypatch.setattr("src.everbot.core.runtime.events.emit", _fake_emit)

        await emit_push_message(
            "Need attention",
            primary_session_id="web_session_test_agent",
            agent_name="test_agent",
            run_id="run_1",
            scope="session",
        )

        assert len(emitted) == 1
        source_session_id, _data, kwargs = emitted[0]
        assert source_session_id == "web_session_test_agent"
        assert kwargs["scope"] == "session"
        assert kwargs["target_session_id"] == "web_session_test_agent"


# ── _should_inspect with enriched context ─────────────────────


class TestShouldInspect:
    """Tests for _should_inspect() with session/task/events context hashes."""

    def test_detects_session_change(self, tmp_path):
        """_should_inspect returns True when session_summary changes."""
        inspector = _make_inspector(tmp_path)
        (tmp_path / "MEMORY.md").write_text("memory")
        (tmp_path / "HEARTBEAT.md").write_text("heartbeat")

        context1 = InspectionContext(
            memory_content="memory",
            heartbeat_content="heartbeat",
            session_summary="User asked about Python",
            task_execution_stats={},
            recent_events=[],
        )

        assert inspector._should_inspect(context1) is True
        inspector.update_state(context1, InspectionResult())
        assert inspector._should_inspect(context1) is False

        context2 = InspectionContext(
            memory_content="memory",
            heartbeat_content="heartbeat",
            session_summary="User asked about JavaScript",
            task_execution_stats={},
            recent_events=[],
        )
        assert inspector._should_inspect(context2) is True

    def test_detects_task_stats_change(self, tmp_path):
        """_should_inspect returns True when task_execution_stats changes."""
        inspector = _make_inspector(tmp_path)
        (tmp_path / "MEMORY.md").write_text("memory")
        (tmp_path / "HEARTBEAT.md").write_text("heartbeat")

        context1 = InspectionContext(
            memory_content="memory",
            heartbeat_content="heartbeat",
            task_execution_stats={"completed": 5, "failed": 0},
            recent_events=[],
        )

        assert inspector._should_inspect(context1) is True
        inspector.update_state(context1, InspectionResult())
        assert inspector._should_inspect(context1) is False

        context2 = InspectionContext(
            memory_content="memory",
            heartbeat_content="heartbeat",
            task_execution_stats={"completed": 6, "failed": 1},
            recent_events=[],
        )
        assert inspector._should_inspect(context2) is True

    def test_events_change_does_not_trigger(self, tmp_path):
        """_should_inspect returns False when only recent_events changes.

        Events are excluded from context hashes because routine cron completions
        would otherwise trigger inspection on every heartbeat tick.
        """
        inspector = _make_inspector(tmp_path)
        (tmp_path / "MEMORY.md").write_text("memory")
        (tmp_path / "HEARTBEAT.md").write_text("heartbeat")

        context1 = InspectionContext(
            memory_content="memory",
            heartbeat_content="heartbeat",
            recent_events=[{"type": "error", "msg": "Connection failed"}],
        )

        assert inspector._should_inspect(context1) is True
        inspector.update_state(context1, InspectionResult())
        assert inspector._should_inspect(context1) is False

        context2 = InspectionContext(
            memory_content="memory",
            heartbeat_content="heartbeat",
            recent_events=[
                {"type": "error", "msg": "Connection failed"},
                {"type": "error", "msg": "Timeout"},
            ],
        )
        # Events changed but other context didn't — should NOT trigger
        assert inspector._should_inspect(context2) is False

    def test_respects_force_interval(self, tmp_path):
        """_should_inspect returns True when force interval elapsed even if context unchanged."""
        inspector = _make_inspector(tmp_path, inspect_force_interval_hours=1)
        (tmp_path / "MEMORY.md").write_text("memory")
        (tmp_path / "HEARTBEAT.md").write_text("heartbeat")

        context = InspectionContext(
            memory_content="memory",
            heartbeat_content="heartbeat",
        )

        assert inspector._should_inspect(context) is True
        inspector.update_state(context, InspectionResult())
        assert inspector._should_inspect(context) is False

        state = inspector._load_state()
        state["last_run_at"] = (datetime.now() - timedelta(hours=2)).isoformat()
        inspector._persist_state(state)
        inspector._reflection.last_reflect_at = datetime.now() - timedelta(hours=2)

        assert inspector._should_inspect(context) is True


# ── Context gathering ─────────────────────────────────────────


class TestContextGathering:
    """Tests for _gather_context() — session, task stats, events collection."""

    def test_with_session_manager(self, tmp_path):
        """_gather_context collects session summary when session_manager provided."""
        inspector = _make_inspector(tmp_path)

        mock_session_manager = MagicMock()
        mock_session_manager.get_session_summary = MagicMock(
            return_value="User asked about Python debugging"
        )

        context = inspector._gather_context(
            heartbeat_content="test heartbeat",
            session_manager=mock_session_manager,
            primary_session_id="telegram_123",
        )

        assert context.session_summary == "User asked about Python debugging"
        assert context.heartbeat_content == "test heartbeat"
        mock_session_manager.get_session_summary.assert_called_once()

    def test_without_session_manager(self, tmp_path):
        """_gather_context works without session_manager (graceful degradation)."""
        inspector = _make_inspector(tmp_path)

        context = inspector._gather_context(
            heartbeat_content="test heartbeat",
            session_manager=None,
            primary_session_id=None,
        )

        assert context.session_summary is None
        assert context.heartbeat_content == "test heartbeat"
        assert isinstance(context.task_execution_stats, dict)
        assert isinstance(context.recent_events, list)

    def test_with_task_stats(self, tmp_path):
        """_gather_context includes task execution statistics."""
        inspector = _make_inspector(tmp_path)

        mock_routine_manager = MagicMock()
        mock_routine_manager.list_routines = MagicMock(return_value=[
            {"id": "routine_1", "title": "Daily Check", "enabled": True},
        ])
        inspector.routine_manager = mock_routine_manager

        context = inspector._gather_context(
            heartbeat_content="test",
            session_manager=None,
            primary_session_id=None,
        )

        assert isinstance(context.task_execution_stats, dict)

    def test_reads_task_stats_from_list_routines(self, tmp_path):
        """Task stats should be derived from RoutineManager.list_routines()."""
        inspector = _make_inspector(tmp_path)

        now = datetime.now(timezone.utc)
        mock_routine_manager = MagicMock()
        mock_routine_manager.list_routines = MagicMock(
            return_value=[
                {
                    "id": "routine_1",
                    "title": "Pending job",
                    "state": "pending",
                    "last_run_at": now.isoformat(),
                },
                {
                    "id": "routine_2",
                    "title": "Failed job",
                    "state": "failed",
                    "last_run_at": (now - timedelta(days=2)).isoformat(),
                },
            ]
        )
        inspector.routine_manager = mock_routine_manager

        context = inspector._gather_context("test")

        assert context.task_execution_stats == {
            "total": 2,
            "failed": 1,
            "pending": 1,
            "last_24h": 1,
        }


# ── inspect() main cycle ──────────────────────────────────────


class TestInspect:
    """Tests for Inspector.inspect() — the main inspection cycle."""

    @pytest.mark.asyncio
    async def test_skipped_returns_skipped_result(self, tmp_path):
        """When should_skip is True, inspect returns a skipped result without calling LLM."""
        (tmp_path / "MEMORY.md").write_text("content")
        (tmp_path / "HEARTBEAT.md").write_text("content")

        inspector = _make_inspector(tmp_path)
        inspector.update_state()

        run_agent = AsyncMock()
        inject_context = AsyncMock()

        with patch.object(inspector, "_write_event"):
            result = await inspector.inspect(
                run_agent=run_agent,
                inject_context=inject_context,
                agent=MagicMock(),
                heartbeat_content="content",
                run_id="run_001",
            )

        assert result.skipped is True
        assert result.skip_reason == "no_context_change"
        assert result.output == "HEARTBEAT_OK"
        run_agent.assert_not_called()
        inject_context.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_proposals_returns_heartbeat_ok(self, tmp_path):
        """When LLM produces no proposals, result output is HEARTBEAT_OK."""
        inspector = _make_inspector(tmp_path)

        run_agent = AsyncMock(return_value=_make_reflection_response_no_proposals())
        inject_context = AsyncMock(return_value="reflected prompt")

        with patch.object(inspector, "_write_event"):
            result = await inspector.inspect(
                run_agent=run_agent,
                inject_context=inject_context,
                agent=MagicMock(),
                heartbeat_content="hb content",
                run_id="run_002",
            )

        assert result.skipped is False
        assert result.proposals == []
        assert result.output == "HEARTBEAT_OK"
        assert result.applied == 0
        inject_context.assert_awaited_once()
        assert inject_context.await_args.kwargs["mode"] == "reflect_json"

    @pytest.mark.asyncio
    async def test_proposals_auto_register(self, tmp_path):
        """When auto_register_routines=True, proposals are applied via RoutineManager."""
        routine_mgr = RoutineManager(tmp_path)
        inspector = _make_inspector(
            tmp_path,
            routine_manager=routine_mgr,
            auto_register_routines=True,
        )

        proposals = [
            {"title": "Daily digest", "schedule": "24h", "description": "Send daily digest"},
        ]
        run_agent = AsyncMock(return_value=_make_reflection_response_with_proposals(proposals))
        inject_context = AsyncMock(return_value="reflected prompt")

        with patch.object(inspector, "_write_event"):
            result = await inspector.inspect(
                run_agent=run_agent,
                inject_context=inject_context,
                agent=MagicMock(),
                heartbeat_content="hb content",
                run_id="run_003",
            )

        assert result.skipped is False
        assert len(result.proposals) == 1
        assert result.applied == 1
        assert result.deposited == 0
        assert result.output == "HEARTBEAT_OK"

    @pytest.mark.asyncio
    async def test_proposals_deposit_to_mailbox(self, tmp_path):
        """When auto_register_routines=False, proposals are deposited to mailbox."""
        inspector = _make_inspector(tmp_path, auto_register_routines=False)

        proposals = [
            {"title": "Weekly report", "schedule": "168h", "description": "Generate weekly report"},
        ]
        run_agent = AsyncMock(return_value=_make_reflection_response_with_proposals(proposals))
        inject_context = AsyncMock(return_value="reflected prompt")

        session_manager = MagicMock()
        session_manager.deposit_mailbox_event = AsyncMock(return_value=True)

        with patch.object(inspector, "_write_event"):
            result = await inspector.inspect(
                run_agent=run_agent,
                inject_context=inject_context,
                agent=MagicMock(),
                heartbeat_content="hb content",
                run_id="run_004",
                session_manager=session_manager,
                primary_session_id="primary_sess_001",
            )

        assert result.skipped is False
        assert len(result.proposals) == 1
        assert result.deposited == 1
        assert result.applied == 0
        assert result.output == "HEARTBEAT_OK"
        session_manager.deposit_mailbox_event.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_proposals_deposit_without_session_manager(self, tmp_path):
        """When no session_manager is provided, deposit doesn't crash."""
        inspector = _make_inspector(tmp_path, auto_register_routines=False)

        proposals = [
            {"title": "Nightly backup", "schedule": "24h", "description": "Run backup"},
        ]
        run_agent = AsyncMock(return_value=_make_reflection_response_with_proposals(proposals))
        inject_context = AsyncMock(return_value="reflected prompt")

        with patch.object(inspector, "_write_event"):
            result = await inspector.inspect(
                run_agent=run_agent,
                inject_context=inject_context,
                agent=MagicMock(),
                heartbeat_content="hb content",
                run_id="run_005",
            )

        assert result.deposited == 0
        assert len(result.proposals) == 1

    @pytest.mark.asyncio
    async def test_auto_register_duplicate_skipped(self, tmp_path):
        """Duplicate routines are silently skipped during auto-register."""
        routine_mgr = RoutineManager(tmp_path)
        inspector = _make_inspector(
            tmp_path,
            routine_manager=routine_mgr,
            auto_register_routines=True,
        )

        proposals = [
            {"title": "Daily digest", "schedule": "24h", "description": "Send daily digest"},
        ]

        run_agent = AsyncMock(return_value=_make_reflection_response_with_proposals(proposals))
        inject_context = AsyncMock(return_value="reflected prompt")

        with patch.object(inspector, "_write_event"):
            result1 = await inspector.inspect(
                run_agent=run_agent,
                inject_context=inject_context,
                agent=MagicMock(),
                heartbeat_content="hb content",
                run_id="run_006",
            )

        assert result1.applied == 1

        inspector._reflection.last_reflect_at = None

        with patch.object(inspector, "_write_event"):
            result2 = await inspector.inspect(
                run_agent=run_agent,
                inject_context=inject_context,
                agent=MagicMock(),
                heartbeat_content="hb content",
                run_id="run_007",
            )

        assert result2.applied == 0
        assert "Skipped duplicates: 1" in result2.output or result2.output == "HEARTBEAT_OK"

    @pytest.mark.asyncio
    async def test_update_state_called_after_llm(self, tmp_path):
        """update_state is called after LLM call to record file hashes."""
        inspector = _make_inspector(tmp_path)
        (tmp_path / "MEMORY.md").write_text("content")
        (tmp_path / "HEARTBEAT.md").write_text("hb")

        run_agent = AsyncMock(return_value=_make_reflection_response_no_proposals())
        inject_context = AsyncMock(return_value="prompt")

        with patch.object(inspector, "_write_event"):
            await inspector.inspect(
                run_agent=run_agent,
                inject_context=inject_context,
                agent=MagicMock(),
                heartbeat_content="hb",
                run_id="run_008",
            )

        assert inspector.should_skip() is True

    @pytest.mark.asyncio
    async def test_empty_llm_response_does_not_update_state(self, tmp_path):
        """Empty reflection responses must not be persisted as successful inspections."""
        inspector = _make_inspector(tmp_path)
        (tmp_path / "MEMORY.md").write_text("content")
        (tmp_path / "HEARTBEAT.md").write_text("hb")

        run_agent = AsyncMock(side_effect=["", _make_reflection_response_no_proposals()])
        inject_context = AsyncMock(return_value="prompt")

        with patch.object(inspector, "_write_event"):
            first = await inspector.inspect(
                run_agent=run_agent,
                inject_context=inject_context,
                agent=MagicMock(),
                heartbeat_content="hb",
                run_id="run_empty_001",
            )
            second = await inspector.inspect(
                run_agent=run_agent,
                inject_context=inject_context,
                agent=MagicMock(),
                heartbeat_content="hb",
                run_id="run_empty_002",
            )

        assert first.heartbeat_ok is False
        assert first.output == "LLM_ERROR: empty response"
        assert first.skipped is False
        assert second.skipped is False
        assert second.output == "HEARTBEAT_OK"
        assert run_agent.await_count == 2


# ── New output format {heartbeat_ok, push_message, routines} ──


class TestNewOutputFormat:
    """Tests for unified LLM output format."""

    @pytest.mark.asyncio
    async def test_heartbeat_ok_true_no_push(self, tmp_path):
        """When heartbeat_ok=true and no push_message, result is HEARTBEAT_OK."""
        inspector = _make_inspector(tmp_path)
        (tmp_path / "MEMORY.md").write_text("content")

        response = _make_reflection_response_v2_format(heartbeat_ok=True)

        run_agent = AsyncMock(return_value=response)
        inject_context = AsyncMock(return_value="prompt")

        with patch.object(inspector, "_write_event"):
            result = await inspector.inspect(
                run_agent=run_agent,
                inject_context=inject_context,
                agent=MagicMock(),
                heartbeat_content="hb",
                run_id="run_v2_001",
            )

        assert result.heartbeat_ok is True
        assert result.push_message is None
        assert result.output == "HEARTBEAT_OK"

    @pytest.mark.asyncio
    async def test_heartbeat_ok_false_with_push(self, tmp_path):
        """When heartbeat_ok=false with push_message, result reflects that."""
        inspector = _make_inspector(tmp_path)
        (tmp_path / "MEMORY.md").write_text("content")

        response = _make_reflection_response_v2_format(
            heartbeat_ok=False,
            push_message="Detected anomalous task failures",
        )

        run_agent = AsyncMock(return_value=response)
        inject_context = AsyncMock(return_value="prompt")

        with patch.object(inspector, "_write_event"):
            result = await inspector.inspect(
                run_agent=run_agent,
                inject_context=inject_context,
                agent=MagicMock(),
                heartbeat_content="hb",
                run_id="run_v2_002",
            )

        assert result.heartbeat_ok is False
        assert result.push_message == "Detected anomalous task failures"
        assert result.output == "HEARTBEAT_ERROR"

    def test_string_false_heartbeat_ok_is_parsed_as_false(self):
        """String false must not be treated as a truthy heartbeat result."""
        response = _make_reflection_response_v2_format(
            heartbeat_ok="false",
            push_message="Need attention",
        )
        parsed = ReflectionManager.extract_unified_response(response)
        assert parsed.heartbeat_ok is False

    def test_string_true_heartbeat_ok_is_parsed_as_true(self):
        """String true should still be treated as a healthy heartbeat result."""
        response = _make_reflection_response_v2_format(
            heartbeat_ok="true",
        )
        parsed = ReflectionManager.extract_unified_response(response)
        assert parsed.heartbeat_ok is True

    @pytest.mark.asyncio
    async def test_push_message_extraction(self, tmp_path):
        """Non-routine push_message is extracted and returned separately."""
        inspector = _make_inspector(tmp_path)
        (tmp_path / "MEMORY.md").write_text("content")

        response = _make_reflection_response_v2_format(
            heartbeat_ok=True,
            push_message="User requested feature X - needs manual review",
        )

        run_agent = AsyncMock(return_value=response)
        inject_context = AsyncMock(return_value="prompt")

        mock_session_manager = MagicMock()
        mock_session_manager.deposit_mailbox_event = AsyncMock(return_value=True)

        with patch.object(inspector, "_write_event"):
            result = await inspector.inspect(
                run_agent=run_agent,
                inject_context=inject_context,
                agent=MagicMock(),
                heartbeat_content="hb",
                run_id="run_v2_003",
                session_manager=mock_session_manager,
                primary_session_id="telegram_123",
            )

        assert result.push_message == "User requested feature X - needs manual review"
        assert result.proposals == []

    @pytest.mark.asyncio
    async def test_combined_routines_and_push(self, tmp_path):
        """LLM can return both routines and push_message simultaneously."""
        inspector = _make_inspector(tmp_path)
        (tmp_path / "MEMORY.md").write_text("content")

        response = _make_reflection_response_v2_format(
            heartbeat_ok=True,
            push_message="I've identified some improvements",
            routines=[{
                "title": "New Routine",
                "schedule": "1h",
                "description": "Test routine",
            }],
        )

        run_agent = AsyncMock(return_value=response)
        inject_context = AsyncMock(return_value="prompt")

        with patch.object(inspector, "_write_event"):
            result = await inspector.inspect(
                run_agent=run_agent,
                inject_context=inject_context,
                agent=MagicMock(),
                heartbeat_content="hb",
                run_id="run_v2_004",
            )

        assert result.push_message == "I've identified some improvements"
        assert len(result.proposals) == 1
        assert result.proposals[0]["title"] == "New Routine"


# ── Push message delivery ─────────────────────────────────────


class TestPushMessageDelivery:
    """Tests for push message delivery to primary session."""

    @pytest.mark.asyncio
    async def test_unhealthy_push_message_not_reused_as_result_output(self, tmp_path):
        """Unhealthy push_message should be deposited once and not mirrored in output."""
        inspector = _make_inspector(tmp_path)
        (tmp_path / "MEMORY.md").write_text("content")

        response = _make_reflection_response_v2_format(
            heartbeat_ok=False,
            push_message="Important notification",
        )

        run_agent = AsyncMock(return_value=response)
        inject_context = AsyncMock(return_value="prompt")

        mock_session_manager = MagicMock()
        mock_session_manager.deposit_mailbox_event = AsyncMock(return_value=True)

        with patch.object(inspector, "_write_event"):
            result = await inspector.inspect(
                run_agent=run_agent,
                inject_context=inject_context,
                agent=MagicMock(),
                heartbeat_content="hb",
                run_id="run_v2_004b",
                session_manager=mock_session_manager,
                primary_session_id="telegram_123",
            )

        assert result.output == "HEARTBEAT_ERROR"
        assert result.push_message == "Important notification"
        assert result.deposited == 1
        mock_session_manager.deposit_mailbox_event.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_push_message_deposited_to_session(self, tmp_path):
        """When push_message exists and session_manager provided, deposit event."""
        inspector = _make_inspector(tmp_path)
        (tmp_path / "MEMORY.md").write_text("content")

        response = _make_reflection_response_v2_format(
            heartbeat_ok=True,
            push_message="Important notification",
        )

        run_agent = AsyncMock(return_value=response)
        inject_context = AsyncMock(return_value="prompt")

        mock_session_manager = MagicMock()
        mock_session_manager.deposit_mailbox_event = AsyncMock(return_value=True)

        with patch.object(inspector, "_write_event"):
            result = await inspector.inspect(
                run_agent=run_agent,
                inject_context=inject_context,
                agent=MagicMock(),
                heartbeat_content="hb",
                run_id="run_v2_005",
                session_manager=mock_session_manager,
                primary_session_id="telegram_123",
            )

        assert result.deposited == 1
        mock_session_manager.deposit_mailbox_event.assert_called_once()
        call_args = mock_session_manager.deposit_mailbox_event.call_args
        assert call_args[0][0] == "telegram_123"

    @pytest.mark.asyncio
    async def test_no_deposit_without_session_manager(self, tmp_path):
        """When no session_manager, push_message is set but not deposited."""
        inspector = _make_inspector(tmp_path)
        (tmp_path / "MEMORY.md").write_text("content")

        response = _make_reflection_response_v2_format(
            heartbeat_ok=True,
            push_message="Important notification",
        )

        run_agent = AsyncMock(return_value=response)
        inject_context = AsyncMock(return_value="prompt")

        with patch.object(inspector, "_write_event"):
            result = await inspector.inspect(
                run_agent=run_agent,
                inject_context=inject_context,
                agent=MagicMock(),
                heartbeat_content="hb",
                run_id="run_v2_006",
                session_manager=None,
                primary_session_id=None,
            )

        assert result.push_message == "Important notification"
        assert result.deposited == 0

    @pytest.mark.asyncio
    async def test_no_deposit_without_primary_session(self, tmp_path):
        """When no primary_session_id, push_message is set but not deposited."""
        inspector = _make_inspector(tmp_path)
        (tmp_path / "MEMORY.md").write_text("content")

        response = _make_reflection_response_v2_format(
            heartbeat_ok=True,
            push_message="Important notification",
        )

        run_agent = AsyncMock(return_value=response)
        inject_context = AsyncMock(return_value="prompt")

        mock_session_manager = MagicMock()

        with patch.object(inspector, "_write_event"):
            result = await inspector.inspect(
                run_agent=run_agent,
                inject_context=inject_context,
                agent=MagicMock(),
                heartbeat_content="hb",
                run_id="run_v2_007",
                session_manager=mock_session_manager,
                primary_session_id=None,
            )

        assert result.push_message == "Important notification"
        assert result.deposited == 0
        mock_session_manager.deposit_mailbox_event.assert_not_called()


# ── State persistence ─────────────────────────────────────────


class TestStatePersistence:
    """Tests for .inspector_state.json persistence."""

    def test_state_file_created_after_inspect(self, tmp_path):
        """State file is created in user data tmp after successful inspection."""
        inspector = _make_inspector(tmp_path)
        (tmp_path / "MEMORY.md").write_text("content")

        context = InspectionContext(
            memory_content="content",
            session_summary="test session",
        )
        result = InspectionResult(heartbeat_ok=True)

        inspector.update_state(context, result)

        state_file = tmp_path / ".alfred" / "test_agent" / "tmp" / INSPECTOR_STATE_FILE
        assert state_file.exists()
        assert not (tmp_path / INSPECTOR_STATE_FILE).exists()

        state = json.loads(state_file.read_text())
        assert "context_hashes" in state
        assert "last_run_at" in state
        assert "session_summary" in state["context_hashes"]

    def test_state_loaded_on_init(self, tmp_path):
        """Existing state file is loaded when Inspector is created."""
        state = {
            "last_run_at": datetime.now().isoformat(),
            "context_hashes": {
                "memory": "abc123",
                "heartbeat": "def456",
                "session_summary": "session_hash",
                "task_stats": "task_hash",
            },
        }
        state_file = tmp_path / ".alfred" / "test_agent" / "tmp" / INSPECTOR_STATE_FILE
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(state))

        inspector = _make_inspector(tmp_path)
        loaded = inspector._load_state()

        assert loaded["context_hashes"]["session_summary"] == "session_hash"


# ── Idle-aware inspection ─────────────────────────────────────


class TestIdleAwareInspection:
    """Tests for idle_hours context and proactive inspection triggers."""

    def test_gather_context_computes_idle_hours(self, tmp_path):
        """_gather_context computes idle_hours from session_manager."""
        import time

        inspector = _make_inspector(tmp_path)
        mock_sm = MagicMock()
        mock_sm.get_last_activity_time = MagicMock(
            return_value=time.time() - 3 * 3600,  # 3 hours ago
        )

        ctx = inspector._gather_context(
            "heartbeat", session_manager=mock_sm, primary_session_id="p1",
        )
        assert ctx.idle_hours is not None
        assert 2.9 <= ctx.idle_hours <= 3.1

    def test_gather_context_idle_hours_none_without_session_manager(self, tmp_path):
        """idle_hours is None when no session_manager is provided."""
        inspector = _make_inspector(tmp_path)
        ctx = inspector._gather_context("heartbeat")
        assert ctx.idle_hours is None

    def test_gather_context_idle_hours_none_when_no_activity(self, tmp_path):
        """idle_hours is None when get_last_activity_time returns None."""
        inspector = _make_inspector(tmp_path)
        mock_sm = MagicMock()
        mock_sm.get_last_activity_time = MagicMock(return_value=None)

        ctx = inspector._gather_context(
            "heartbeat", session_manager=mock_sm, primary_session_id="p1",
        )
        assert ctx.idle_hours is None

    def test_should_inspect_true_when_idle_grows(self, tmp_path):
        """_should_inspect returns True when idle_hours grows by >= 1 hour."""
        inspector = _make_inspector(tmp_path)
        (tmp_path / "MEMORY.md").write_text("memory")
        (tmp_path / "HEARTBEAT.md").write_text("heartbeat")

        ctx = InspectionContext(
            memory_content="memory",
            heartbeat_content="heartbeat",
            idle_hours=3.0,
        )
        inspector.update_state(ctx, InspectionResult())

        ctx_later = InspectionContext(
            memory_content="memory",
            heartbeat_content="heartbeat",
            idle_hours=4.5,
        )
        assert inspector._should_inspect(ctx_later) is True

    def test_should_inspect_false_when_idle_grows_less_than_1h(self, tmp_path):
        """_should_inspect returns False when idle_hours grows by < 1 hour."""
        inspector = _make_inspector(tmp_path)
        (tmp_path / "MEMORY.md").write_text("memory")
        (tmp_path / "HEARTBEAT.md").write_text("heartbeat")

        ctx = InspectionContext(
            memory_content="memory",
            heartbeat_content="heartbeat",
            idle_hours=3.0,
        )
        inspector.update_state(ctx, InspectionResult())

        ctx_later = InspectionContext(
            memory_content="memory",
            heartbeat_content="heartbeat",
            idle_hours=3.5,
        )
        assert inspector._should_inspect(ctx_later) is False

    def test_should_inspect_true_first_idle_above_1h(self, tmp_path):
        """_should_inspect returns True when first seeing idle >= 1h (no prior state)."""
        inspector = _make_inspector(tmp_path)

        ctx = InspectionContext(
            memory_content="memory",
            heartbeat_content="heartbeat",
            idle_hours=2.0,
        )
        assert inspector._should_inspect(ctx) is True

    def test_should_inspect_skips_idle_below_1h(self, tmp_path):
        """_should_inspect ignores idle_hours below 1.0 for proactive trigger."""
        inspector = _make_inspector(tmp_path)
        (tmp_path / "MEMORY.md").write_text("memory")
        (tmp_path / "HEARTBEAT.md").write_text("heartbeat")

        ctx = InspectionContext(
            memory_content="memory",
            heartbeat_content="heartbeat",
            idle_hours=0.5,
        )
        inspector.update_state(ctx, InspectionResult())

        ctx_later = InspectionContext(
            memory_content="memory",
            heartbeat_content="heartbeat",
            idle_hours=0.9,
        )
        # Should fall through to normal hash-based check (unchanged -> False)
        assert inspector._should_inspect(ctx_later) is False

    def test_idle_hours_persisted_in_state(self, tmp_path):
        """update_state persists last_idle_hours."""
        inspector = _make_inspector(tmp_path)
        ctx = InspectionContext(idle_hours=5.0)
        inspector.update_state(ctx, InspectionResult())

        state = inspector._load_state()
        assert state["last_idle_hours"] == 5.0

    def test_build_reflect_prompt_includes_idle(self, tmp_path):
        """_build_reflect_prompt includes idle duration when present."""
        inspector = _make_inspector(tmp_path)
        ctx = InspectionContext(idle_hours=6.2)
        prompt = inspector._build_reflect_prompt(ctx)
        assert "6.2" in prompt
        assert "用户上次互动" in prompt

    def test_build_reflect_prompt_no_idle_section_when_none(self, tmp_path):
        """_build_reflect_prompt omits idle section when idle_hours is None."""
        inspector = _make_inspector(tmp_path)
        ctx = InspectionContext(idle_hours=None)
        prompt = inspector._build_reflect_prompt(ctx)
        assert "用户活跃状态" not in prompt


# ── Bug regression: inspection_complete must not cause self-reinspect ─────────


class TestInspectionSelfPollutionBug:
    """Regression tests for the inspection_complete event self-pollution bug.

    Previously, update_state(ctx) was called BEFORE _write_event("inspection_complete").
    The saved events hash didn't include the new event, so the next cycle would see
    a different hash and re-run the LLM indefinitely.
    """

    @pytest.mark.asyncio
    async def test_second_inspect_skips_when_context_unchanged(self, tmp_path):
        """After inspect(), a second call with same context must skip LLM.

        Uses real event file writing (no mock on _write_event) to expose the bug.
        """
        stub_udm = _StubUserDataManager(tmp_path / ".alfred")

        with patch(
            "src.everbot.core.runtime.inspector.get_user_data_manager",
            return_value=stub_udm,
        ):
            inspector = Inspector(
                agent_name="test_agent",
                workspace_path=tmp_path,
                routine_manager=RoutineManager(tmp_path),
                auto_register_routines=False,
                inspect_force_interval_hours=24,
            )

        (tmp_path / "MEMORY.md").write_text("memory content")
        (tmp_path / "HEARTBEAT.md").write_text("heartbeat content")

        llm_call_count = 0

        async def _run_agent(agent, msg, **kwargs):
            nonlocal llm_call_count
            llm_call_count += 1
            return _make_reflection_response_v2_format(heartbeat_ok=True)

        inject_context = AsyncMock(return_value="prompt")

        with patch(
            "src.everbot.core.runtime.inspector.get_user_data_manager",
            return_value=stub_udm,
        ):
            result1 = await inspector.inspect(
                run_agent=_run_agent,
                inject_context=inject_context,
                agent=MagicMock(),
                heartbeat_content="heartbeat content",
                run_id="run_001",
            )
            assert result1.skipped is False, "First inspect should run LLM"
            assert llm_call_count == 1

            result2 = await inspector.inspect(
                run_agent=_run_agent,
                inject_context=inject_context,
                agent=MagicMock(),
                heartbeat_content="heartbeat content",
                run_id="run_002",
            )

        assert result2.skipped is True, (
            "Second inspect with unchanged context must skip, "
            "but inspection_complete event caused re-trigger (LLM called again)."
        )
        assert llm_call_count == 1, (
            f"LLM was called {llm_call_count} times but should only be called once. "
            "inspection_complete event is self-polluting the events hash."
        )

    @pytest.mark.asyncio
    async def test_many_consecutive_inspects_only_run_llm_once(self, tmp_path):
        """Multiple inspect() calls with unchanged context run LLM exactly once."""
        stub_udm = _StubUserDataManager(tmp_path / ".alfred")

        with patch(
            "src.everbot.core.runtime.inspector.get_user_data_manager",
            return_value=stub_udm,
        ):
            inspector = Inspector(
                agent_name="test_agent",
                workspace_path=tmp_path,
                routine_manager=RoutineManager(tmp_path),
                auto_register_routines=False,
                inspect_force_interval_hours=24,
            )

        (tmp_path / "MEMORY.md").write_text("stable content")
        (tmp_path / "HEARTBEAT.md").write_text("stable heartbeat")

        llm_call_count = 0

        async def _run_agent(agent, msg, **kwargs):
            nonlocal llm_call_count
            llm_call_count += 1
            return _make_reflection_response_v2_format(heartbeat_ok=True)

        inject_context = AsyncMock(return_value="prompt")

        with patch(
            "src.everbot.core.runtime.inspector.get_user_data_manager",
            return_value=stub_udm,
        ):
            for i in range(4):
                await inspector.inspect(
                    run_agent=_run_agent,
                    inject_context=inject_context,
                    agent=MagicMock(),
                    heartbeat_content="stable heartbeat",
                    run_id=f"run_{i:03d}",
                )

        assert llm_call_count == 1, (
            f"LLM was called {llm_call_count} times across 4 inspect() cycles "
            "with unchanged context. inspection_complete events are self-triggering."
        )


# ---------------------------------------------------------------------------
# Tests: force_push_interval — minimum push frequency
# ---------------------------------------------------------------------------

class TestForcePushInterval:
    """Inspector should force a push_message if none has been sent for too long."""

    @pytest.mark.asyncio
    async def test_prompt_includes_force_push_directive_when_overdue(self, tmp_path):
        """When last_push_at is > force_push_interval ago, the reflection prompt
        should include a directive telling the LLM it MUST generate a push_message."""
        inspector = _make_inspector(tmp_path, inspect_force_interval_hours=3)

        # Seed state: last push was 4 hours ago
        state_path = inspector._state_path
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "last_run_at": (datetime.now() - timedelta(hours=1)).isoformat(),
            "last_push_at": (datetime.now() - timedelta(hours=4)).isoformat(),
            "context_hashes": {},
            "last_idle_hours": 0,
        }
        state_path.write_text(json.dumps(state))

        ctx = InspectionContext(
            heartbeat_content="test",
            idle_hours=0.5,
        )
        prompt = inspector._build_reflect_prompt(ctx)
        assert "必须" in prompt or "MUST" in prompt, (
            "Prompt should contain a force-push directive when overdue"
        )
        assert "push_message" in prompt

    @pytest.mark.asyncio
    async def test_no_force_directive_when_recently_pushed(self, tmp_path):
        """When last_push_at is recent, no force directive should appear."""
        inspector = _make_inspector(tmp_path, inspect_force_interval_hours=3)

        state_path = inspector._state_path
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "last_run_at": (datetime.now() - timedelta(minutes=30)).isoformat(),
            "last_push_at": (datetime.now() - timedelta(hours=1)).isoformat(),
            "context_hashes": {},
            "last_idle_hours": 0,
        }
        state_path.write_text(json.dumps(state))

        ctx = InspectionContext(
            heartbeat_content="test",
            idle_hours=0.5,
        )
        prompt = inspector._build_reflect_prompt(ctx)
        assert "必须" not in prompt and "MUST" not in prompt, (
            "Prompt should NOT contain a force-push directive when recently pushed"
        )

    @pytest.mark.asyncio
    async def test_last_push_at_updated_on_push(self, tmp_path):
        """After a successful push_message, last_push_at should be updated in state."""
        inspector = _make_inspector(
            tmp_path,
            inspect_force_interval_hours=3,
            agent_factory=AsyncMock(),
        )

        state_path = inspector._state_path
        state_path.parent.mkdir(parents=True, exist_ok=True)
        old_push_time = (datetime.now() - timedelta(hours=5)).isoformat()
        state = {
            "last_run_at": (datetime.now() - timedelta(hours=3)).isoformat(),
            "last_push_at": old_push_time,
            "context_hashes": {},
            "last_idle_hours": 0,
        }
        state_path.write_text(json.dumps(state))

        # Mock LLM to return a response with push_message
        llm_response = json.dumps({
            "heartbeat_ok": True,
            "push_message": "一切正常，这是定期汇报。",
            "routines": [],
        })
        inspector._run_llm = AsyncMock(return_value=llm_response)

        result = await inspector.inspect(
            heartbeat_content="test",
            run_id="test_run",
        )

        assert result.push_message is not None

        # Check state was updated
        new_state = json.loads(state_path.read_text())
        assert "last_push_at" in new_state
        assert new_state["last_push_at"] != old_push_time

    @pytest.mark.asyncio
    async def test_fallback_push_when_llm_ignores_force(self, tmp_path):
        """If LLM returns push_message=null despite force directive,
        inspector should generate a default status message."""
        inspector = _make_inspector(
            tmp_path,
            inspect_force_interval_hours=3,
            agent_factory=AsyncMock(),
        )

        state_path = inspector._state_path
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "last_run_at": (datetime.now() - timedelta(hours=4)).isoformat(),
            "last_push_at": (datetime.now() - timedelta(hours=4)).isoformat(),
            "context_hashes": {},
            "last_idle_hours": 0,
        }
        state_path.write_text(json.dumps(state))

        # LLM ignores the force directive
        llm_response = json.dumps({
            "heartbeat_ok": True,
            "push_message": None,
            "routines": [],
        })
        inspector._run_llm = AsyncMock(return_value=llm_response)

        result = await inspector.inspect(
            heartbeat_content="test",
            run_id="test_run",
        )

        assert result.push_message is not None, (
            "Should generate fallback push_message when LLM ignores force directive"
        )
