"""
Integration tests for heartbeat skill watermark advancement.

Covers:
- Scanner gate blocks skill when no changes (watermark up-to-date)
- Watermark is advanced after successful skill execution
- Watermark is NOT advanced on skill failure
- Second cycle skips when no new sessions since watermark
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.everbot.core.runtime.heartbeat import HeartbeatRunner
from src.everbot.core.scanners.base import ScanResult
from src.everbot.core.scanners.reflection_state import ReflectionState
from src.everbot.core.tasks.task_manager import (
    Task,
    TaskList,
    TaskState,
    ParseStatus,
    get_due_tasks,
)


def _make_runner(workspace_path: Path, **overrides) -> HeartbeatRunner:
    session_manager = overrides.pop("session_manager", None)
    if session_manager is None:
        from contextlib import contextmanager

        @contextmanager
        def _file_lock(session_id, blocking=False):
            yield True

        session_manager = SimpleNamespace(
            get_primary_session_id=lambda agent_name: f"web_session_{agent_name}",
            get_heartbeat_session_id=lambda agent_name: f"heartbeat_session_{agent_name}",
            acquire_session=AsyncMock(return_value=True),
            release_session=MagicMock(),
            file_lock=_file_lock,
            load_session=AsyncMock(return_value=None),
            get_cached_agent=MagicMock(return_value=None),
            cache_agent=MagicMock(),
            save_session=AsyncMock(),
            append_timeline_event=MagicMock(),
            record_metric=MagicMock(),
            migrate_legacy_sessions_for_agent=AsyncMock(return_value=False),
            deposit_mailbox_event=AsyncMock(return_value=True),
            inject_history_message=AsyncMock(return_value=True),
            mark_session_archived=AsyncMock(return_value=True),
            restore_timeline=MagicMock(),
            restore_to_agent=AsyncMock(),
            update_atomic=AsyncMock(return_value=None),
        )
        session_manager.persistence = SimpleNamespace(
            restore_to_agent=AsyncMock(),
        )
    defaults = {
        "agent_name": "test_agent",
        "workspace_path": workspace_path,
        "session_manager": session_manager,
        "agent_factory": AsyncMock(),
        "interval_minutes": 1,
        "active_hours": (0, 24),
        "max_retries": 3,
        "on_result": None,
    }
    defaults.update(overrides)
    return HeartbeatRunner(**defaults)


def _build_structured_md(tasks: list[dict] | None = None) -> str:
    task_list = {"version": 2, "tasks": tasks or []}
    return f"# HEARTBEAT\n\n## Tasks\n\n```json\n{json.dumps(task_list, indent=2)}\n```\n"


def _make_skill_task(execution_mode: str = "inline") -> dict:
    """Create a skill task with scanner gate."""
    return {
        "id": "routine_test_001",
        "title": "Test skill task",
        "description": "Test skill with scanner",
        "source": "manual",
        "enabled": True,
        "schedule": "2m",
        "timezone": "Asia/Shanghai",
        "execution_mode": execution_mode,
        "state": "pending",
        "last_run_at": "2026-01-01T00:00:00",
        "next_run_at": "2026-01-01T00:00:00+08:00",
        "timeout_seconds": 120,
        "retry": 0,
        "max_retry": 3,
        "error_message": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "skill": "test-skill",
        "scanner": "session",
        "min_execution_interval": None,
    }


def _setup_runner_with_tasks(runner: HeartbeatRunner, tmp_path: Path, tasks: list[dict]):
    """Write HEARTBEAT.md and load task list into the runner."""
    heartbeat_md = tmp_path / "HEARTBEAT.md"
    heartbeat_md.write_text(_build_structured_md(tasks))
    runner._read_heartbeat_md()


@pytest.mark.asyncio
async def test_watermark_advanced_after_successful_skill_execution(tmp_path: Path):
    """After a skill executes successfully, watermark should be advanced
    so the next scanner check returns has_changes=False for the same sessions."""

    runner = _make_runner(workspace_path=tmp_path)
    _setup_runner_with_tasks(runner, tmp_path, [_make_skill_task()])

    session_updated_at = "2026-03-04T12:00:00"

    fake_scan_result = ScanResult(
        has_changes=True,
        change_summary="1 new session",
        payload=[
            SimpleNamespace(
                id="web_session_test_agent",
                path=tmp_path / "sessions" / "web_session_test_agent.json",
                updated_at=session_updated_at,
                session_type="primary",
            )
        ],
    )

    fake_scanner = MagicMock()
    fake_scanner.check.return_value = fake_scan_result

    runner._get_scanner = MagicMock(return_value=fake_scanner)
    runner._invoke_skill_task = AsyncMock(return_value="skill ok")

    agent = AsyncMock()
    await runner._execute_structured_tasks(agent, "heartbeat content", "run-001")

    # Verify: watermark should have been set (to current time, not scan-time)
    state = ReflectionState.load(tmp_path)
    wm = state.get_watermark("test-skill")
    assert wm != "", "Watermark should be set after successful skill execution"


@pytest.mark.asyncio
async def test_scanner_gate_blocks_when_no_changes(tmp_path: Path):
    """When scanner returns has_changes=False, skill should be skipped."""

    runner = _make_runner(workspace_path=tmp_path)
    _setup_runner_with_tasks(runner, tmp_path, [_make_skill_task()])

    fake_scanner = MagicMock()
    fake_scanner.check.return_value = ScanResult(
        has_changes=False,
        change_summary="No new sessions since last scan",
    )
    runner._get_scanner = MagicMock(return_value=fake_scanner)
    runner._invoke_skill_task = AsyncMock()

    agent = AsyncMock()
    await runner._execute_structured_tasks(agent, "heartbeat content", "run-001")

    # Skill should NOT have been invoked
    runner._invoke_skill_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_second_cycle_skipped_after_watermark_advanced(tmp_path: Path):
    """Simulate two consecutive heartbeat cycles. After the first successful
    execution advances watermark, the second cycle should skip (no new changes)."""

    runner = _make_runner(workspace_path=tmp_path)

    session_updated_at = "2026-03-04T12:00:00"

    fake_scan_result_with_changes = ScanResult(
        has_changes=True,
        change_summary="1 new session",
        payload=[
            SimpleNamespace(
                id="web_session_test_agent",
                path=tmp_path / "sessions" / "web_session_test_agent.json",
                updated_at=session_updated_at,
                session_type="primary",
            )
        ],
    )
    fake_scan_result_no_changes = ScanResult(
        has_changes=False,
        change_summary="No new sessions since last scan",
    )

    call_count = 0

    def dynamic_check(watermark, agent_name=""):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            assert watermark == ""
            return fake_scan_result_with_changes
        else:
            # Watermark should be advanced past session_updated_at (set to current time)
            assert watermark > session_updated_at, (
                f"Expected watermark > {session_updated_at}, got {watermark}"
            )
            return fake_scan_result_no_changes

    fake_scanner = MagicMock()
    fake_scanner.check.side_effect = dynamic_check
    runner._get_scanner = MagicMock(return_value=fake_scanner)
    runner._invoke_skill_task = AsyncMock(return_value="skill ok")

    # Cycle 1
    _setup_runner_with_tasks(runner, tmp_path, [_make_skill_task()])
    agent = AsyncMock()
    await runner._execute_structured_tasks(agent, "heartbeat content", "run-001")

    # Cycle 2: reload tasks (task rearmed to pending by _rearm_skill_task)
    _setup_runner_with_tasks(runner, tmp_path, [_make_skill_task()])
    await runner._execute_structured_tasks(agent, "heartbeat content", "run-002")

    # Skill should only have been invoked once (first cycle)
    assert runner._invoke_skill_task.await_count == 1
    assert call_count == 2


@pytest.mark.asyncio
async def test_no_self_triggering_loop_when_skill_updates_session(tmp_path: Path):
    """Regression: skill execution injects results into primary session, updating
    its updated_at. If watermark is set to scan-time updated_at (T1), the next
    scanner check sees updated_at=T2 > T1 and returns has_changes=True, creating
    an infinite self-triggering loop.

    Fix: watermark is set to current time (after execution), so T2 < watermark.
    """

    runner = _make_runner(workspace_path=tmp_path)

    session_updated_at_before = "2025-01-01T06:00:00+00:00"
    # Simulate: after skill execution, session updated_at moves forward
    session_updated_at_after = "2025-01-01T06:01:30+00:00"

    call_count = 0

    def realistic_scanner_check(watermark, agent_name=""):
        """Simulate a scanner that actually compares watermark vs session updated_at."""
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First call: no watermark, session has data
            return ScanResult(
                has_changes=True,
                change_summary="1 new session",
                payload=[
                    SimpleNamespace(
                        id="web_session_test_agent",
                        path=tmp_path / "sessions" / "web_session_test_agent.json",
                        updated_at=session_updated_at_before,
                        session_type="primary",
                    )
                ],
            )
        else:
            # Second call: session updated_at moved forward due to skill execution
            # side-effect (result injection). The watermark must be > this value
            # to prevent re-triggering.
            if watermark > session_updated_at_after:
                return ScanResult(has_changes=False, change_summary="No new sessions")
            else:
                return ScanResult(
                    has_changes=True,
                    change_summary="1 updated session (self-trigger!)",
                    payload=[
                        SimpleNamespace(
                            id="web_session_test_agent",
                            path=tmp_path / "sessions" / "web_session_test_agent.json",
                            updated_at=session_updated_at_after,
                            session_type="primary",
                        )
                    ],
                )

    fake_scanner = MagicMock()
    fake_scanner.check.side_effect = realistic_scanner_check
    runner._get_scanner = MagicMock(return_value=fake_scanner)
    runner._invoke_skill_task = AsyncMock(return_value="skill ok")

    # Cycle 1: should execute
    _setup_runner_with_tasks(runner, tmp_path, [_make_skill_task()])
    agent = AsyncMock()
    await runner._execute_structured_tasks(agent, "heartbeat content", "run-001")

    # Cycle 2: should skip (watermark > session_updated_at_after)
    _setup_runner_with_tasks(runner, tmp_path, [_make_skill_task()])
    await runner._execute_structured_tasks(agent, "heartbeat content", "run-002")

    # Skill should only have been invoked once — no self-triggering loop
    assert runner._invoke_skill_task.await_count == 1, (
        f"Skill invoked {runner._invoke_skill_task.await_count} times, expected 1. "
        "Self-triggering loop detected!"
    )
    assert call_count == 2


@pytest.mark.asyncio
async def test_watermark_not_advanced_on_skill_failure(tmp_path: Path):
    """If skill execution fails, watermark should NOT be advanced."""

    runner = _make_runner(workspace_path=tmp_path)
    _setup_runner_with_tasks(runner, tmp_path, [_make_skill_task()])

    session_updated_at = "2026-03-04T12:00:00"

    fake_scan_result = ScanResult(
        has_changes=True,
        change_summary="1 new session",
        payload=[
            SimpleNamespace(
                id="web_session_test_agent",
                path=tmp_path / "sessions" / "web_session_test_agent.json",
                updated_at=session_updated_at,
                session_type="primary",
            )
        ],
    )

    fake_scanner = MagicMock()
    fake_scanner.check.return_value = fake_scan_result
    runner._get_scanner = MagicMock(return_value=fake_scanner)
    runner._invoke_skill_task = AsyncMock(side_effect=RuntimeError("skill crashed"))

    agent = AsyncMock()
    await runner._execute_structured_tasks(agent, "heartbeat content", "run-001")

    # Watermark should remain empty (not advanced on failure)
    state = ReflectionState.load(tmp_path)
    assert state.get_watermark("test-skill") == "", (
        "Watermark should NOT be advanced when skill execution fails"
    )


# ============================================================
# Isolated execution mode tests
# ============================================================


@pytest.mark.asyncio
async def test_isolated_scanner_gate_blocks_when_no_changes(tmp_path: Path):
    """Isolated skill tasks should also respect scanner gate."""

    runner = _make_runner(workspace_path=tmp_path)
    _setup_runner_with_tasks(runner, tmp_path, [_make_skill_task("isolated")])

    fake_scanner = MagicMock()
    fake_scanner.check.return_value = ScanResult(
        has_changes=False,
        change_summary="No new sessions since last scan",
    )
    runner._get_scanner = MagicMock(return_value=fake_scanner)
    runner._execute_isolated_task = AsyncMock()

    agent = AsyncMock()
    await runner._execute_structured_tasks(agent, "heartbeat content", "run-001")

    # Isolated task should NOT have been executed
    runner._execute_isolated_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_isolated_watermark_advanced_after_success(tmp_path: Path):
    """Isolated skill tasks should advance watermark after successful execution."""

    runner = _make_runner(workspace_path=tmp_path)
    _setup_runner_with_tasks(runner, tmp_path, [_make_skill_task("isolated")])

    session_updated_at = "2026-03-04T12:00:00"

    fake_scan_result = ScanResult(
        has_changes=True,
        change_summary="1 new session",
        payload=[
            SimpleNamespace(
                id="web_session_test_agent",
                path=tmp_path / "sessions" / "web_session_test_agent.json",
                updated_at=session_updated_at,
                session_type="primary",
            )
        ],
    )

    fake_scanner = MagicMock()
    fake_scanner.check.return_value = fake_scan_result
    runner._get_scanner = MagicMock(return_value=fake_scanner)
    runner._execute_isolated_task = AsyncMock(return_value="isolated skill ok")

    agent = AsyncMock()
    await runner._execute_structured_tasks(agent, "heartbeat content", "run-001")

    # Verify: watermark should have been set
    state = ReflectionState.load(tmp_path)
    wm = state.get_watermark("test-skill")
    assert wm != "", "Watermark should be set after successful isolated skill execution"


@pytest.mark.asyncio
async def test_isolated_task_respects_min_execution_interval(tmp_path: Path):
    """Isolated skill task with min_execution_interval should be skipped if last run is too recent."""
    from datetime import timedelta

    task_data = _make_skill_task("isolated")
    task_data["min_execution_interval"] = "2h"
    task_data["last_run_at"] = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()

    runner = _make_runner(workspace_path=tmp_path)
    _setup_runner_with_tasks(runner, tmp_path, [task_data])

    fake_scanner = MagicMock()
    fake_scanner.check.return_value = ScanResult(
        has_changes=True,
        change_summary="1 new session",
        payload=[
            SimpleNamespace(
                id="web_session_test_agent",
                path=tmp_path / "sessions" / "web_session_test_agent.json",
                updated_at="2026-03-04T12:00:00",
                session_type="primary",
            )
        ],
    )
    runner._get_scanner = MagicMock(return_value=fake_scanner)
    runner._execute_isolated_task = AsyncMock()

    agent = AsyncMock()
    await runner._execute_structured_tasks(agent, "heartbeat content", "run-001")

    # Task should be skipped due to min_execution_interval
    runner._execute_isolated_task.assert_not_awaited()


# ============================================================
# Stuck running task tests
# ============================================================


def test_stuck_running_task_not_healed_by_file_manager(tmp_path: Path):
    """read_heartbeat_md does NOT heal running tasks. Running tasks are
    handled by _recover_stuck_running_tasks (heartbeat_tasks.py) which
    checks 2x timeout before recovery."""
    from src.everbot.core.runtime.heartbeat_file import HeartbeatFileManager

    task_data = _make_skill_task()
    task_data["state"] = "running"  # Stuck from crash/timeout
    heartbeat_md = tmp_path / "HEARTBEAT.md"
    heartbeat_md.write_text(_build_structured_md([task_data]))

    mgr = HeartbeatFileManager(tmp_path)
    mgr.read_heartbeat_md()

    # Running task stays running — file manager doesn't heal it
    assert mgr.task_list.tasks[0].state == "running"
    # No pending/due tasks → reflect mode (the problematic state)
    assert mgr.heartbeat_mode == "structured_reflect"


def test_stuck_running_task_recovered_in_execute_once(tmp_path: Path):
    """_execute_once calls _recover_stuck_running_tasks when mode is
    structured_reflect, then re-evaluates mode to structured_due if
    recovered tasks become due.
    Note: _recover_stuck_running_tasks only handles isolated tasks."""
    runner = _make_runner(workspace_path=tmp_path)

    # Must be isolated — _recover_stuck_running_tasks only handles isolated tasks
    task_data = _make_skill_task("isolated")
    task_data["state"] = "running"
    task_data["last_run_at"] = "2020-01-01T00:00:00+00:00"  # Long ago (2x timeout passed)
    _setup_runner_with_tasks(runner, tmp_path, [task_data])

    # Confirm initial mode is structured_reflect (running task not due)
    assert runner._file_mgr.heartbeat_mode == "structured_reflect"

    # Simulate what _execute_once does: recover + re-evaluate
    runner._recover_stuck_running_tasks()
    task = runner._file_mgr.task_list.tasks[0]

    # _recover_stuck_running_tasks sets FAILED, then update_task_state
    # re-arms scheduled tasks to pending with recomputed next_run_at
    assert task.state == "pending"

    # Re-evaluate mode (same logic as _execute_once)
    due = get_due_tasks(runner._file_mgr.task_list)
    if due:
        runner._file_mgr.heartbeat_mode = "structured_due"
        assert runner._file_mgr.heartbeat_mode == "structured_due"


# ============================================================
# Path equivalence: isolated claimed path respects scanner gate
# ============================================================


@pytest.mark.asyncio
async def test_path_b_isolated_claimed_respects_scanner_gate(tmp_path: Path):
    """Verify execute_isolated_claimed_task (Path B / unified dispatch) applies
    the same scanner gate as _execute_structured_tasks (Path A).

    Before fix: Path B skipped scanner gate entirely, causing unconditional
    skill execution on every heartbeat cycle.
    """
    runner = _make_runner(workspace_path=tmp_path)

    # Scanner returns no_changes — both paths should skip
    fake_scanner = MagicMock()
    fake_scanner.check.return_value = ScanResult(
        has_changes=False,
        change_summary="No new sessions since last scan",
    )
    runner._get_scanner = MagicMock(return_value=fake_scanner)
    runner._execute_isolated_task = AsyncMock()
    runner._write_heartbeat_event = MagicMock()

    # Write HEARTBEAT.md so _update_isolated_task_state can find the task
    task_data = _make_skill_task("isolated")
    _setup_runner_with_tasks(runner, tmp_path, [task_data])

    # Build snapshot (same as what list_due_isolated_tasks returns)
    from src.everbot.core.runtime.heartbeat_utils import task_snapshot as build_snapshot
    task_obj = runner._file_mgr.task_list.tasks[0]
    snapshot = build_snapshot(task_obj)

    await runner.execute_isolated_claimed_task(snapshot)

    # Skill should NOT have been executed (gate blocked)
    runner._execute_isolated_task.assert_not_awaited()
    # Event should record the skip
    runner._write_heartbeat_event.assert_any_call(
        "skill_skipped", skill="test-skill", reason="no_changes",
    )
