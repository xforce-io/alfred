"""Unit tests for heartbeat reflection strong-constraint routine apply."""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.everbot.core.runtime.heartbeat import (
    HeartbeatRunner,
    _is_permanent_error,
)
from src.everbot.core.runtime.scheduler import AgentSchedule, Scheduler
from src.everbot.core.tasks.task_manager import ParseStatus, parse_heartbeat_md


def _make_runner(workspace_path: Path, **kwargs) -> HeartbeatRunner:
    session_manager = SimpleNamespace(
        get_primary_session_id=lambda agent_name: f"web_session_{agent_name}",
        get_heartbeat_session_id=lambda agent_name: f"heartbeat_session_{agent_name}",
        deposit_mailbox_event=AsyncMock(return_value=True),
    )
    defaults = dict(
        agent_name="demo",
        workspace_path=workspace_path,
        session_manager=session_manager,
        agent_factory=AsyncMock(),
        interval_minutes=1,
        active_hours=(0, 24),
        max_retries=1,
        on_result=None,
    )
    defaults.update(kwargs)
    return HeartbeatRunner(**defaults)


def _write_empty_task_block(workspace_path: Path) -> None:
    content = """# HEARTBEAT

## Tasks

```json
{
  "version": 2,
  "tasks": []
}
```
"""
    (workspace_path / "HEARTBEAT.md").write_text(content, encoding="utf-8")


def test_extract_reflection_routine_proposals_from_json_block(tmp_path: Path):
    runner = _make_runner(tmp_path)
    response = """
HEARTBEAT_OK
```json
{
  "routines": [
    {"title": "Daily digest", "description": "summary", "schedule": "1d", "execution_mode": "isolated"}
  ]
}
```
"""
    proposals = runner._extract_reflection_routine_proposals(response)
    assert len(proposals) == 1
    assert proposals[0]["title"] == "Daily digest"


def test_apply_reflection_routine_proposals_adds_new_routine(tmp_path: Path):
    _write_empty_task_block(tmp_path)
    runner = _make_runner(tmp_path)
    response = """```json
{"routines":[{"title":"Daily digest","description":"summary","schedule":"1d","execution_mode":"isolated"}]}
```"""

    updated = runner._apply_reflection_routine_proposals(response, "run_1")
    assert "Registered 1 routine(s)" in updated

    parsed = parse_heartbeat_md((tmp_path / "HEARTBEAT.md").read_text(encoding="utf-8"))
    assert parsed.status == ParseStatus.OK
    assert parsed.task_list is not None
    assert len(parsed.task_list.tasks) == 1
    task = parsed.task_list.tasks[0]
    assert task.title == "Daily digest"
    assert task.source == "heartbeat_reflect"
    assert task.execution_mode == "isolated"


def test_apply_reflection_routine_proposals_skips_duplicate_and_keeps_response(tmp_path: Path):
    _write_empty_task_block(tmp_path)
    runner = _make_runner(tmp_path)
    initial = """```json
{"routines":[{"title":"Daily digest","schedule":"1d","execution_mode":"inline"}]}
```"""
    runner._apply_reflection_routine_proposals(initial, "run_1")

    duplicate = """```json
{"routines":[{"title":"Daily digest","schedule":"1d","execution_mode":"inline"}]}
```"""
    updated = runner._apply_reflection_routine_proposals(duplicate, "run_2")
    assert updated == duplicate

    parsed = parse_heartbeat_md((tmp_path / "HEARTBEAT.md").read_text(encoding="utf-8"))
    assert parsed.status == ParseStatus.OK
    assert parsed.task_list is not None
    assert len(parsed.task_list.tasks) == 1


def test_apply_reflection_routine_proposals_invalid_mode_falls_back_to_auto(tmp_path: Path):
    _write_empty_task_block(tmp_path)
    runner = _make_runner(tmp_path)
    long_desc = "x" * 260
    response = f"""```json
{{"routines":[{{"title":"Auto inferred mode","description":"{long_desc}","schedule":"1d","execution_mode":"invalid_mode"}}]}}
```"""

    runner._apply_reflection_routine_proposals(response, "run_3")

    parsed = parse_heartbeat_md((tmp_path / "HEARTBEAT.md").read_text(encoding="utf-8"))
    assert parsed.status == ParseStatus.OK
    assert parsed.task_list is not None
    assert len(parsed.task_list.tasks) == 1
    assert parsed.task_list.tasks[0].execution_mode == "isolated"


# ── Permanent error detection ────────────────────────────────────────

class TestPermanentErrorDetection:
    def test_insufficient_balance_is_permanent(self):
        exc = RuntimeError("Error code: 402 - insufficient balance for request")
        assert _is_permanent_error(exc) is True

    def test_invalid_api_key_is_permanent(self):
        exc = RuntimeError("Error code: 401 - invalid_api_key")
        assert _is_permanent_error(exc) is True

    def test_403_is_permanent(self):
        exc = RuntimeError("HTTP 403 Forbidden")
        assert _is_permanent_error(exc) is True

    def test_transient_500_is_not_permanent(self):
        exc = RuntimeError("HTTP 500 Internal Server Error")
        assert _is_permanent_error(exc) is False

    def test_connection_error_is_not_permanent(self):
        exc = ConnectionError("Connection refused")
        assert _is_permanent_error(exc) is False

    def test_permanent_error_only_retries_once(self):
        """When a permanent error is raised, _execute_with_retry should not retry."""
        runner = _make_runner(Path("/tmp/test_perm"))
        call_count = 0

        async def _fake_execute_once(**kwargs):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("402 insufficient balance")

        runner._execute_once = _fake_execute_once
        runner.max_retries = 5

        with pytest.raises(RuntimeError, match="402"):
            asyncio.run(runner._execute_with_retry())
        assert call_count == 1


# ── Scheduler exponential backoff ────────────────────────────────────

class TestSchedulerBackoff:
    def test_consecutive_failures_increases_backoff(self):
        call_count = 0

        async def _fail_heartbeat(agent_name: str, ts: datetime):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("test error")

        schedule = AgentSchedule(
            agent_name="test_agent",
            interval_minutes=1,
            active_hours=(0, 24),
            max_backoff_minutes=60,
        )
        scheduler = Scheduler(
            run_heartbeat=_fail_heartbeat,
            agent_schedules={"test_agent": schedule},
        )

        ts = datetime(2026, 2, 15, 12, 0)
        asyncio.run(scheduler._tick_heartbeats(ts))
        assert schedule.consecutive_failures == 1
        # After 1 failure: interval * 2^1 = 2 minutes
        assert schedule.next_heartbeat_at is not None

        # Simulate second failure
        schedule.next_heartbeat_at = ts  # force due
        asyncio.run(scheduler._tick_heartbeats(ts))
        assert schedule.consecutive_failures == 2

    def test_success_resets_consecutive_failures(self):
        async def _ok_heartbeat(agent_name: str, ts: datetime):
            pass

        schedule = AgentSchedule(
            agent_name="test_agent",
            interval_minutes=1,
            active_hours=(0, 24),
            consecutive_failures=5,
        )
        scheduler = Scheduler(
            run_heartbeat=_ok_heartbeat,
            agent_schedules={"test_agent": schedule},
        )

        ts = datetime(2026, 2, 15, 12, 0)
        asyncio.run(scheduler._tick_heartbeats(ts))
        assert schedule.consecutive_failures == 0

    def test_backoff_capped_at_max(self):
        async def _fail_heartbeat(agent_name: str, ts: datetime):
            raise RuntimeError("test")

        schedule = AgentSchedule(
            agent_name="test_agent",
            interval_minutes=1,
            active_hours=(0, 24),
            max_backoff_minutes=10,
            consecutive_failures=99,  # Already high
        )
        schedule.next_heartbeat_at = None  # Force due
        scheduler = Scheduler(
            run_heartbeat=_fail_heartbeat,
            agent_schedules={"test_agent": schedule},
        )

        ts = datetime(2026, 2, 15, 12, 0)
        asyncio.run(scheduler._tick_heartbeats(ts))
        # Next heartbeat should be at most max_backoff_minutes from ts
        assert schedule.next_heartbeat_at is not None
        delta = schedule.next_heartbeat_at - ts
        assert delta <= timedelta(minutes=10, seconds=5)

    def test_state_persistence_with_consecutive_failures(self, tmp_path: Path):
        state_file = tmp_path / "scheduler_state.json"
        schedule = AgentSchedule(
            agent_name="test_agent",
            interval_minutes=1,
            active_hours=(0, 24),
            consecutive_failures=3,
        )
        schedule.next_heartbeat_at = datetime(2026, 2, 15, 12, 0)
        scheduler = Scheduler(
            agent_schedules={"test_agent": schedule},
            state_file=state_file,
        )
        scheduler._save_state()

        # Restore into new scheduler
        schedule2 = AgentSchedule(agent_name="test_agent", interval_minutes=1)
        scheduler2 = Scheduler(
            agent_schedules={"test_agent": schedule2},
            state_file=state_file,
        )
        assert schedule2.consecutive_failures == 3
        assert schedule2.next_heartbeat_at == datetime(2026, 2, 15, 12, 0)

    def test_restore_state_handles_legacy_format(self, tmp_path: Path):
        """Legacy state file has plain ISO string values."""
        state_file = tmp_path / "scheduler_state.json"
        import json
        state_file.write_text(json.dumps({
            "test_agent": "2026-02-15T12:00:00"
        }), encoding="utf-8")

        schedule = AgentSchedule(agent_name="test_agent", interval_minutes=1)
        Scheduler(
            agent_schedules={"test_agent": schedule},
            state_file=state_file,
        )
        assert schedule.next_heartbeat_at == datetime(2026, 2, 15, 12, 0)
        assert schedule.consecutive_failures == 0


# ── auto_register_routines=False → mailbox ───────────────────────────

class TestAutoRegisterRoutinesDisabled:
    def test_proposals_not_registered_when_disabled(self, tmp_path: Path):
        _write_empty_task_block(tmp_path)
        runner = _make_runner(tmp_path, auto_register_routines=False)
        response = """```json
{"routines":[{"title":"Should not register","schedule":"1d","execution_mode":"inline"}]}
```"""

        result = asyncio.run(
            runner._deposit_routine_proposals_to_mailbox(
                runner._extract_reflection_routine_proposals(response),
                "run_test",
            )
        )
        assert "proposed" in result.lower()
        assert "Should not register" in result

        # Task should NOT be in HEARTBEAT.md
        parsed = parse_heartbeat_md(
            (tmp_path / "HEARTBEAT.md").read_text(encoding="utf-8")
        )
        assert parsed.task_list is not None
        assert len(parsed.task_list.tasks) == 0

    def test_proposals_registered_when_enabled(self, tmp_path: Path):
        _write_empty_task_block(tmp_path)
        runner = _make_runner(tmp_path, auto_register_routines=True)
        response = """```json
{"routines":[{"title":"Auto registered","schedule":"1d","execution_mode":"inline"}]}
```"""

        result = runner._apply_reflection_routine_proposals(response, "run_test")
        assert "Registered" in result

        parsed = parse_heartbeat_md(
            (tmp_path / "HEARTBEAT.md").read_text(encoding="utf-8")
        )
        assert parsed.task_list is not None
        assert len(parsed.task_list.tasks) == 1
