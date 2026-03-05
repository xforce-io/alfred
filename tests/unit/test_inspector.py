"""Unit tests for Inspector — heartbeat reflection observation engine."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.everbot.core.runtime.inspector import Inspector, InspectionResult
from src.everbot.core.runtime.reflection import ReflectionManager
from src.everbot.core.tasks.routine_manager import RoutineManager


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
    return Inspector(**defaults)


def _make_reflection_response_with_proposals(proposals: list[dict]) -> str:
    """Build an LLM response containing a JSON routine proposal block."""
    payload = json.dumps({"routines": proposals}, ensure_ascii=False, indent=2)
    return f"Here are my suggestions:\n```json\n{payload}\n```"


def _make_reflection_response_no_proposals() -> str:
    """Build an LLM response with no routine proposals."""
    return "Everything looks good. No new routines needed."


class TestShouldSkip:
    """Tests for Inspector.should_skip() — file hash + time interval logic."""

    def test_skip_when_files_unchanged(self, tmp_path):
        """After update_state, should_skip returns True if files haven't changed."""
        # Seed MEMORY.md so hashes are stable
        (tmp_path / "MEMORY.md").write_text("some memory content")
        (tmp_path / "HEARTBEAT.md").write_text("some heartbeat content")

        inspector = _make_inspector(tmp_path)
        # First call: never reflected before, should NOT skip
        assert inspector.should_skip() is False

        # Record state (simulates having just reflected)
        inspector.update_state()

        # Files unchanged → should skip
        assert inspector.should_skip() is True

    def test_no_skip_when_force_interval_elapsed(self, tmp_path):
        """When force interval has elapsed, should_skip returns False."""
        (tmp_path / "MEMORY.md").write_text("content")
        (tmp_path / "HEARTBEAT.md").write_text("content")

        inspector = _make_inspector(tmp_path, reflect_force_interval_hours=1)
        inspector.update_state()

        # Simulate time passage beyond force interval
        inspector._reflection.last_reflect_at = datetime.now() - timedelta(hours=2)

        assert inspector.should_skip() is False

    def test_no_skip_when_files_changed(self, tmp_path):
        """When files change after update_state, should_skip returns False."""
        (tmp_path / "MEMORY.md").write_text("v1")
        (tmp_path / "HEARTBEAT.md").write_text("v1")

        inspector = _make_inspector(tmp_path)
        inspector.update_state()

        # Modify a file
        (tmp_path / "MEMORY.md").write_text("v2 - updated")

        assert inspector.should_skip() is False

    def test_no_skip_when_never_reflected(self, tmp_path):
        """First call should never skip."""
        inspector = _make_inspector(tmp_path)
        assert inspector.should_skip() is False


class TestInspect:
    """Tests for Inspector.inspect() — the main inspection cycle."""

    @pytest.mark.asyncio
    async def test_skipped_returns_skipped_result(self, tmp_path):
        """When should_skip is True, inspect returns a skipped result without calling LLM."""
        (tmp_path / "MEMORY.md").write_text("content")
        (tmp_path / "HEARTBEAT.md").write_text("content")

        inspector = _make_inspector(tmp_path)
        inspector.update_state()  # Mark as just reflected, files unchanged

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
        assert result.skip_reason == "file_unchanged"
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
        assert "Registered 1 routine" in result.output
        assert "Daily digest" in result.output

    @pytest.mark.asyncio
    async def test_proposals_deposit_to_mailbox(self, tmp_path):
        """When auto_register_routines=False, proposals are deposited to mailbox."""
        inspector = _make_inspector(tmp_path, auto_register_routines=False)

        proposals = [
            {"title": "Weekly report", "schedule": "168h", "description": "Generate weekly report"},
        ]
        run_agent = AsyncMock(return_value=_make_reflection_response_with_proposals(proposals))
        inject_context = AsyncMock(return_value="reflected prompt")

        session_manager = AsyncMock()
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
        assert "proposed 1 routine" in result.output
        session_manager.deposit_mailbox_event.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_proposals_deposit_without_session_manager(self, tmp_path):
        """When no session_manager is provided, deposit logs a warning but doesn't crash."""
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
                # no session_manager or primary_session_id
            )

        # Should still succeed — just can't deposit
        assert result.deposited == 1
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

        # First inspection — registers the routine
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

        # Second inspection — same proposal, should be skipped as duplicate
        # Reset the reflection state so it doesn't skip the inspection itself
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

        # After inspect, should_skip should return True (files unchanged)
        assert inspector.should_skip() is True
