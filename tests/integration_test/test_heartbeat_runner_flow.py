"""
Integration tests for HeartbeatRunner execution flow.

Covers:
- Retry with incremental backoff
- run_once_with_options force flag & failure callback
- Timeline recording with source metadata
- Sync callback support
- Agent trajectory initialization for cached agents
- Reflection mode when no due structured tasks
- Atomic save for structured task execution
- Corrupted JSON mode detection in _read_heartbeat_md
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.everbot.core.runtime.heartbeat import HeartbeatRunner
from src.everbot.core.tasks.task_manager import (
    Task,
    TaskList,
    TaskState,
    ParseStatus,
)


def _make_runner(workspace_path: Path = Path("."), **overrides) -> HeartbeatRunner:
    session_manager = overrides.pop("session_manager", None)
    if session_manager is None:
        session_manager = SimpleNamespace(
            get_primary_session_id=lambda agent_name: f"web_session_{agent_name}",
            get_heartbeat_session_id=lambda agent_name: f"heartbeat_session_{agent_name}",
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


def _make_session_manager_for_execute():
    """Build a session manager with sufficient mocks for _execute_once."""
    sm = SimpleNamespace(
        get_primary_session_id=lambda agent_name: f"web_session_{agent_name}",
        get_heartbeat_session_id=lambda agent_name: f"heartbeat_session_{agent_name}",
        acquire_session=AsyncMock(return_value=True),
        release_session=MagicMock(),
        load_session=AsyncMock(return_value=None),
        get_cached_agent=MagicMock(return_value=None),
        cache_agent=MagicMock(),
        save_session=AsyncMock(),
        append_timeline_event=MagicMock(),
    )
    sm.persistence = SimpleNamespace(
        restore_to_agent=AsyncMock(),
    )
    return sm


def _build_structured_md(tasks: list[dict] | None = None) -> str:
    """Build a valid HEARTBEAT.md string with optional task list."""
    task_list = {"version": 2, "tasks": tasks or []}
    return f"# HEARTBEAT\n\n## Tasks\n\n```json\n{json.dumps(task_list, indent=2)}\n```\n"


# ============================================================
# _execute_with_retry: incremental backoff
# ============================================================


@pytest.mark.asyncio
async def test_execute_with_retry_uses_incremental_backoff(tmp_path: Path):
    """_execute_with_retry sleeps 5*(attempt+1) between retries."""
    runner = _make_runner(workspace_path=tmp_path, max_retries=3)

    call_count = 0

    async def _failing_execute_once(**kwargs):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("transient error")

    runner._execute_once = _failing_execute_once

    sleep_calls: list[float] = []

    async def _mock_sleep(seconds):
        sleep_calls.append(seconds)

    with patch("asyncio.sleep", new=_mock_sleep):
        with pytest.raises(RuntimeError, match="transient error"):
            await runner._execute_with_retry()

    assert call_count == 3
    # Backoff: 5*1=5 after first failure, 5*2=10 after second failure
    assert sleep_calls == [5, 10]


# ============================================================
# run_once_with_options: force flag
# ============================================================


@pytest.mark.asyncio
async def test_run_once_with_options_respects_force_flag(tmp_path: Path):
    """When force=True, active-hours check is bypassed."""
    # Set active_hours to a window that does NOT include current hour
    runner = _make_runner(
        workspace_path=tmp_path,
        active_hours=(99, 100),  # impossible range
    )

    # Without force, should skip due to inactive hours
    result = await runner.run_once_with_options(force=False)
    assert result == "HEARTBEAT_SKIPPED_INACTIVE"

    # With force, should NOT skip due to active hours
    # But will still need execute_with_retry to work — mock it
    runner._execute_with_retry = AsyncMock(return_value="HEARTBEAT_OK")
    result = await runner.run_once_with_options(force=True)
    assert result == "HEARTBEAT_OK"
    runner._execute_with_retry.assert_awaited_once()


# ============================================================
# run_once_with_options: failure callback
# ============================================================


@pytest.mark.asyncio
async def test_run_once_with_options_reports_failure_via_callback(tmp_path: Path):
    """When execution fails, on_result receives the failure summary."""
    callback_args: list[tuple] = []

    async def _on_result(agent_name: str, result: str):
        callback_args.append((agent_name, result))

    runner = _make_runner(
        workspace_path=tmp_path,
        on_result=_on_result,
        max_retries=1,
    )

    runner._execute_with_retry = AsyncMock(side_effect=RuntimeError("boom"))

    result = await runner.run_once_with_options(force=True)
    assert result == "HEARTBEAT_FAILED"
    assert len(callback_args) == 1
    assert "HEARTBEAT_FAILED" in callback_args[0][1]
    assert "boom" in callback_args[0][1]


# ============================================================
# _execute_once: timeline recording with source metadata
# ============================================================


@pytest.mark.asyncio
async def test_execute_once_records_heartbeat_timeline_with_source_metadata(
    tmp_path: Path, monkeypatch
):
    """_execute_once records timeline events with source_type=heartbeat."""
    sm = _make_session_manager_for_execute()
    runner = _make_runner(workspace_path=tmp_path, session_manager=sm)

    # Write a HEARTBEAT.md with no due tasks to trigger idle path
    # (no HEARTBEAT.md => idle => HEARTBEAT_IDLE)

    monkeypatch.setattr(runner, "_record_timeline_event", MagicMock())
    monkeypatch.setattr(runner, "_write_heartbeat_event", MagicMock())
    monkeypatch.setattr(runner, "_record_runtime_metric", MagicMock())
    monkeypatch.setattr(
        "src.everbot.core.runtime.heartbeat.asyncio.sleep",
        AsyncMock(),
    )

    result = await runner._execute_once()
    assert result == "HEARTBEAT_IDLE"

    # Verify timeline events were recorded with correct arguments
    timeline_calls = runner._record_timeline_event.call_args_list
    assert len(timeline_calls) >= 2  # turn_start + turn_end

    # turn_start call should have trigger="heartbeat"
    start_call = timeline_calls[0]
    assert start_call[0][0] == "turn_start"
    assert start_call[1].get("trigger") == "heartbeat" or (
        len(start_call[0]) >= 3 and "heartbeat" in str(start_call)
    )

    # turn_end call
    end_call = timeline_calls[-1]
    assert end_call[0][0] == "turn_end"


# ============================================================
# run_once_with_options: sync callback support
# ============================================================


@pytest.mark.asyncio
async def test_run_once_with_options_supports_sync_callback(tmp_path: Path):
    """on_result can be a regular (non-async) function."""
    callback_args: list[tuple] = []

    def _sync_on_result(agent_name: str, result: str):
        callback_args.append((agent_name, result))

    runner = _make_runner(
        workspace_path=tmp_path,
        on_result=_sync_on_result,
    )

    runner._execute_with_retry = AsyncMock(return_value="task completed")

    result = await runner.run_once_with_options(force=True)
    assert result == "task completed"
    assert len(callback_args) == 1
    assert callback_args[0] == ("test_agent", "task completed")


# ============================================================
# _get_or_create_agent: non-overwrite trajectory for cached agent
# ============================================================


@pytest.mark.asyncio
async def test_get_or_create_agent_uses_non_overwrite_trajectory_for_cached_agent(
    tmp_path: Path, monkeypatch
):
    """When a cached agent exists, init_trajectory is called with overwrite=False."""
    sm = _make_session_manager_for_execute()

    mock_context = SimpleNamespace(
        set_variable=MagicMock(),
        set_session_id=MagicMock(),
        get_var_value=MagicMock(return_value=""),
        init_trajectory=MagicMock(),
    )
    fake_agent = SimpleNamespace(
        executor=SimpleNamespace(context=mock_context),
    )

    sm.get_cached_agent = MagicMock(return_value=fake_agent)
    sm.load_session = AsyncMock(return_value=None)

    runner = _make_runner(workspace_path=tmp_path, session_manager=sm)

    # Stub _init_session_trajectory to check overwrite parameter
    init_traj_calls: list[dict] = []
    original_init = runner._init_session_trajectory

    def _tracking_init(agent, overwrite=False):
        init_traj_calls.append({"agent": agent, "overwrite": overwrite})

    monkeypatch.setattr(runner, "_init_session_trajectory", _tracking_init)

    agent = await runner._get_or_create_agent()
    assert agent is fake_agent
    assert len(init_traj_calls) == 1
    assert init_traj_calls[0]["overwrite"] is False


# ============================================================
# _execute_once: reflection when no due structured tasks
# ============================================================


@pytest.mark.asyncio
async def test_execute_once_runs_reflection_when_no_due_structured_task(
    tmp_path: Path, monkeypatch
):
    """When HEARTBEAT.md has tasks but none are due, enter reflect mode."""
    sm = _make_session_manager_for_execute()
    runner = _make_runner(
        workspace_path=tmp_path,
        session_manager=sm,
        routine_reflection=True,
    )

    # Write HEARTBEAT.md with a task that is NOT due (next_run_at in far future)
    future_time = (datetime.now() + timedelta(days=30)).isoformat()
    tasks = [{
        "id": "task_1",
        "title": "Future Task",
        "schedule": "1d",
        "state": "pending",
        "enabled": True,
        "next_run_at": future_time,
    }]
    (tmp_path / "HEARTBEAT.md").write_text(
        _build_structured_md(tasks), encoding="utf-8"
    )

    fake_context = SimpleNamespace(
        set_variable=MagicMock(),
        set_session_id=MagicMock(),
        get_var_value=MagicMock(return_value=""),
        init_trajectory=MagicMock(),
    )
    fake_agent = SimpleNamespace(
        executor=SimpleNamespace(context=fake_context),
    )

    monkeypatch.setattr(runner, "_get_or_create_agent", AsyncMock(return_value=fake_agent))
    monkeypatch.setattr(runner, "_run_agent", AsyncMock(return_value="HEARTBEAT_OK"))
    monkeypatch.setattr(runner, "_save_session_atomic", AsyncMock())
    monkeypatch.setattr(runner, "_record_timeline_event", MagicMock())
    monkeypatch.setattr(runner, "_record_runtime_metric", MagicMock())
    monkeypatch.setattr(runner, "_write_heartbeat_event", MagicMock())
    monkeypatch.setattr(runner, "_write_task_snapshot", MagicMock())
    monkeypatch.setattr(runner, "_inject_result_to_primary_history", AsyncMock(return_value=True))
    monkeypatch.setattr(runner, "_deposit_deliver_event_to_primary_session", AsyncMock(return_value=True))

    result = await runner._execute_once()

    # Should have entered structured_reflect mode
    assert runner._heartbeat_mode == "structured_reflect"
    # Reflection with no proposals returns HEARTBEAT_OK
    assert result == "HEARTBEAT_OK"


# ============================================================
# _execute_structured_tasks: uses atomic save
# ============================================================


@pytest.mark.asyncio
async def test_execute_structured_tasks_uses_atomic_save(
    tmp_path: Path, monkeypatch
):
    """_execute_structured_tasks calls _write_heartbeat_file (atomic_save) after task execution."""
    sm = _make_session_manager_for_execute()
    runner = _make_runner(workspace_path=tmp_path, session_manager=sm)

    # Create a due task (use UTC to match get_due_tasks comparison)
    from datetime import timezone
    now = datetime.now(timezone.utc)
    past_time = (now - timedelta(hours=1)).isoformat()
    tasks = [{
        "id": "atomic_task",
        "title": "Test Atomic",
        "schedule": "1h",
        "state": "pending",
        "enabled": True,
        "next_run_at": past_time,
        "timeout_seconds": 60,
    }]
    hb_content = _build_structured_md(tasks)
    (tmp_path / "HEARTBEAT.md").write_text(hb_content, encoding="utf-8")

    # Parse so runner has task_list
    runner._read_heartbeat_md()
    assert runner._heartbeat_mode == "structured_due"

    fake_context = SimpleNamespace(
        set_variable=MagicMock(),
        set_session_id=MagicMock(),
        get_var_value=MagicMock(return_value=""),
        init_trajectory=MagicMock(),
    )
    fake_agent = SimpleNamespace(
        executor=SimpleNamespace(context=fake_context),
    )

    monkeypatch.setattr(runner, "_inject_heartbeat_context", AsyncMock(return_value="execute prompt"))
    monkeypatch.setattr(runner, "_run_agent", AsyncMock(return_value="task result"))
    monkeypatch.setattr(runner, "_record_timeline_event", MagicMock())
    monkeypatch.setattr(runner, "_record_runtime_metric", MagicMock())
    monkeypatch.setattr(runner, "_write_heartbeat_event", MagicMock())

    write_hb_mock = MagicMock()
    monkeypatch.setattr(runner, "_write_heartbeat_file", write_hb_mock)

    result = await runner._execute_structured_tasks(
        fake_agent, hb_content, "run_123"
    )

    assert "task result" in result
    # _flush_task_state writes via _write_heartbeat_file (atomic_save)
    assert write_hb_mock.call_count >= 1


# ============================================================
# _read_heartbeat_md: corrupted JSON mode detection
# ============================================================


def test_read_heartbeat_md_marks_corrupted_json_mode(tmp_path: Path):
    """When HEARTBEAT.md has invalid JSON, mode is set to 'corrupted'."""
    runner = _make_runner(workspace_path=tmp_path)

    # Write HEARTBEAT.md with corrupted JSON
    corrupted_content = (
        "# HEARTBEAT\n\n## Tasks\n\n"
        "```json\n"
        '{"version": 2, "tasks": [INVALID JSON HERE]}\n'
        "```\n"
    )
    (tmp_path / "HEARTBEAT.md").write_text(corrupted_content, encoding="utf-8")

    result = runner._read_heartbeat_md()

    assert result is not None  # content was read
    assert runner._heartbeat_mode == "corrupted"
    assert runner._task_list is None
    assert runner._last_parse_result is not None
    assert runner._last_parse_result.status == ParseStatus.CORRUPTED
