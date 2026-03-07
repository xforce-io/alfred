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
)
from src.everbot.core.runtime.reflection import ReflectionManager
from src.everbot.core.tasks.routine_manager import RoutineManager


class _StubUserDataManager:
    """Test double for user data paths."""

    def __init__(self, base_dir: Path):
        self._base_dir = base_dir

    def get_agent_tmp_dir(self, agent_name: str) -> Path:
        return self._base_dir / agent_name / "tmp"


def _make_inspector(tmp_path: Path, **overrides) -> Inspector:
    """Create an Inspector with sensible defaults for testing."""
    defaults = dict(
        agent_name="test_agent",
        workspace_path=tmp_path,
        routine_manager=RoutineManager(tmp_path),
        auto_register_routines=False,
        reflect_force_interval_hours=24,
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

        inspector = _make_inspector(tmp_path, reflect_force_interval_hours=1)
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

    def test_detects_events_change(self, tmp_path):
        """_should_inspect returns True when recent_events changes."""
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
        assert inspector._should_inspect(context2) is True

    def test_respects_force_interval(self, tmp_path):
        """_should_inspect returns True when force interval elapsed even if context unchanged."""
        inspector = _make_inspector(tmp_path, reflect_force_interval_hours=1)
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
                "events": "events_hash",
            },
        }
        state_file = tmp_path / ".alfred" / "test_agent" / "tmp" / INSPECTOR_STATE_FILE
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(state))

        inspector = _make_inspector(tmp_path)
        loaded = inspector._load_state()

        assert loaded["context_hashes"]["session_summary"] == "session_hash"
