"""
Unit tests for HeartbeatRunner methods with zero coverage.

Covers:
- _is_time_reminder_task (static)
- _try_deterministic_task
- _extract_llm_result (static)
- _normalize_reflection_routine (static)
- _merge_heartbeat_instruction
- list_due_isolated_tasks / list_due_inline_tasks
- _execute_once lock contention
- _execute_structured_tasks timeout / failure handling
- claim_isolated_task
- execute_isolated_claimed_task
"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.everbot.core.runtime.heartbeat import HeartbeatRunner, _is_permanent_error
from src.everbot.core.tasks.task_manager import (
    Task,
    TaskList,
    TaskState,
)


# ── Helpers ──────────────────────────────────────────────────


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


def _build_structured_md(tasks: list[dict] | None = None) -> str:
    """Build a valid HEARTBEAT.md string with optional task list."""
    task_list = {"version": 2, "tasks": tasks or []}
    return f"# HEARTBEAT\n\n## Tasks\n\n```json\n{json.dumps(task_list, indent=2)}\n```\n"


def _make_task(**overrides) -> Task:
    """Create a Task with sensible defaults."""
    defaults = {
        "id": "task_1",
        "title": "Test Task",
        "description": "",
        "schedule": "1h",
        "state": TaskState.PENDING.value,
        "enabled": True,
        "next_run_at": None,
        "timeout_seconds": 120,
        "execution_mode": "inline",
    }
    defaults.update(overrides)
    return Task(**defaults)


def _make_session_manager_with_locks(
    acquire_returns: bool = True,
    file_lock_acquired: bool = True,
):
    """Build a session manager with acquire_session, release_session, file_lock."""
    sm = SimpleNamespace(
        get_primary_session_id=lambda agent_name: f"web_session_{agent_name}",
        get_heartbeat_session_id=lambda agent_name: f"heartbeat_session_{agent_name}",
        acquire_session=AsyncMock(return_value=acquire_returns),
        release_session=MagicMock(),
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
    sm.persistence = SimpleNamespace(restore_to_agent=AsyncMock())

    @contextmanager
    def _file_lock(session_id, blocking=False):
        yield file_lock_acquired

    sm.file_lock = _file_lock
    return sm


# ============================================================
# 1. _is_time_reminder_task
# ============================================================


class TestIsTimeReminderTask:
    """Tests for the static _is_time_reminder_task method."""

    def test_match_time_reminder_in_id(self):
        task = _make_task(id="time_reminder_daily", title="Check")
        assert HeartbeatRunner._is_time_reminder_task(task) is True

    def test_match_time_reminder_with_space_in_title(self):
        task = _make_task(id="task_1", title="Time Reminder for user")
        assert HeartbeatRunner._is_time_reminder_task(task) is True

    def test_match_chinese_current_time_in_description(self):
        task = _make_task(id="task_2", title="Alert", description="播报当前时间")
        assert HeartbeatRunner._is_time_reminder_task(task) is True

    def test_match_chinese_baoshi_in_title(self):
        task = _make_task(id="task_3", title="每日报时")
        assert HeartbeatRunner._is_time_reminder_task(task) is True

    def test_no_match_regular_task(self):
        task = _make_task(id="weather_check", title="Check Weather", description="fetch forecast")
        assert HeartbeatRunner._is_time_reminder_task(task) is False

    def test_no_match_empty_task(self):
        task = SimpleNamespace(id="", title="", description="")
        assert HeartbeatRunner._is_time_reminder_task(task) is False

    def test_no_match_missing_attributes(self):
        task = SimpleNamespace()
        assert HeartbeatRunner._is_time_reminder_task(task) is False


# ============================================================
# 2. _try_deterministic_task
# ============================================================


class TestTryDeterministicTask:
    """Tests for _try_deterministic_task."""

    def test_time_reminder_returns_time_string(self, tmp_path: Path):
        runner = _make_runner(workspace_path=tmp_path)
        task = _make_task(id="time_reminder", title="Report Time")
        result = runner._try_deterministic_task(task)
        assert result is not None
        assert "HEARTBEAT_OK" in result
        # Should contain date-like content
        assert datetime.now().strftime("%Y") in result

    def test_non_time_reminder_returns_none(self, tmp_path: Path):
        runner = _make_runner(workspace_path=tmp_path)
        task = _make_task(id="weather_check", title="Check Weather")
        result = runner._try_deterministic_task(task)
        assert result is None


# ============================================================
# 3. _extract_llm_result
# ============================================================


class TestExtractLlmResult:
    """Tests for the static _extract_llm_result method."""

    def test_empty_events(self):
        assert HeartbeatRunner._extract_llm_result([]) == ""

    def test_no_llm_stage(self):
        events = [{"_progress": [{"stage": "tool", "delta": "hello"}]}]
        assert HeartbeatRunner._extract_llm_result(events) == ""

    def test_deltas_concatenated(self):
        events = [
            {"_progress": [{"stage": "llm", "delta": "Hello "}]},
            {"_progress": [{"stage": "llm", "delta": "World"}]},
        ]
        assert HeartbeatRunner._extract_llm_result(events) == "Hello World"

    def test_answer_takes_priority(self):
        events = [
            {"_progress": [{"stage": "llm", "delta": "partial"}]},
            {"_progress": [{"stage": "llm", "delta": "more", "answer": "Full Answer"}]},
        ]
        assert HeartbeatRunner._extract_llm_result(events) == "Full Answer"

    def test_non_dict_events_skipped(self):
        events = [None, "string_event", 42]
        assert HeartbeatRunner._extract_llm_result(events) == ""

    def test_progress_not_list_skipped(self):
        events = [{"_progress": "not_a_list"}]
        assert HeartbeatRunner._extract_llm_result(events) == ""

    def test_progress_item_not_dict_skipped(self):
        events = [{"_progress": ["not_a_dict"]}]
        assert HeartbeatRunner._extract_llm_result(events) == ""

    def test_mixed_stages(self):
        events = [
            {"_progress": [
                {"stage": "tool", "delta": "ignored"},
                {"stage": "llm", "delta": "kept"},
            ]},
        ]
        assert HeartbeatRunner._extract_llm_result(events) == "kept"


# ============================================================
# 4. _normalize_reflection_routine
# ============================================================


class TestNormalizeReflectionRoutine:
    """Tests for the static _normalize_reflection_routine method."""

    def test_normal_input(self):
        item = {
            "title": "Daily Report",
            "description": "Generate daily summary",
            "schedule": "0 9 * * *",
            "execution_mode": "inline",
            "timezone": "Asia/Shanghai",
            "timeout_seconds": 60,
        }
        result = HeartbeatRunner._normalize_reflection_routine(item)
        assert result is not None
        assert result["title"] == "Daily Report"
        assert result["description"] == "Generate daily summary"
        assert result["schedule"] == "0 9 * * *"
        assert result["execution_mode"] == "inline"
        assert result["timezone_name"] == "Asia/Shanghai"
        assert result["timeout_seconds"] == 60
        assert result["source"] == "heartbeat_reflect"
        assert result["allow_duplicate"] is False

    def test_empty_title_returns_none(self):
        item = {"title": "", "description": "something"}
        assert HeartbeatRunner._normalize_reflection_routine(item) is None

    def test_missing_title_returns_none(self):
        item = {"description": "no title"}
        assert HeartbeatRunner._normalize_reflection_routine(item) is None

    def test_invalid_execution_mode_defaults_to_auto(self):
        item = {"title": "Task", "execution_mode": "INVALID_MODE"}
        result = HeartbeatRunner._normalize_reflection_routine(item)
        assert result is not None
        assert result["execution_mode"] == "auto"

    def test_non_integer_timeout_defaults_to_120(self):
        item = {"title": "Task", "timeout_seconds": "not_a_number"}
        result = HeartbeatRunner._normalize_reflection_routine(item)
        assert result is not None
        assert result["timeout_seconds"] == 120

    def test_missing_optional_fields_filled_with_defaults(self):
        item = {"title": "Minimal Task"}
        result = HeartbeatRunner._normalize_reflection_routine(item)
        assert result is not None
        assert result["description"] == ""
        assert result["schedule"] is None
        assert result["execution_mode"] == "auto"
        assert result["timezone_name"] is None
        assert result["timeout_seconds"] == 120

    def test_whitespace_title_returns_none(self):
        item = {"title": "   "}
        assert HeartbeatRunner._normalize_reflection_routine(item) is None

    def test_isolated_execution_mode_preserved(self):
        item = {"title": "Task", "execution_mode": "isolated"}
        result = HeartbeatRunner._normalize_reflection_routine(item)
        assert result["execution_mode"] == "isolated"

    def test_negative_timeout_clamped_to_1(self):
        item = {"title": "Task", "timeout_seconds": -5}
        result = HeartbeatRunner._normalize_reflection_routine(item)
        assert result["timeout_seconds"] == 1


# ============================================================
# 5. _merge_heartbeat_instruction
# ============================================================


class TestMergeHeartbeatInstruction:
    """Tests for _merge_heartbeat_instruction."""

    def test_first_call_returns_instruction_block(self, tmp_path: Path):
        runner = _make_runner(workspace_path=tmp_path)
        result = runner._merge_heartbeat_instruction("")
        assert HeartbeatRunner._HEARTBEAT_INST_START in result
        assert HeartbeatRunner._HEARTBEAT_INST_END in result
        assert "Heartbeat Mode" in result

    def test_idempotent_no_duplication(self, tmp_path: Path):
        runner = _make_runner(workspace_path=tmp_path)
        first = runner._merge_heartbeat_instruction("")
        second = runner._merge_heartbeat_instruction(first)
        assert second.count(HeartbeatRunner._HEARTBEAT_INST_START) == 1
        assert second.count(HeartbeatRunner._HEARTBEAT_INST_END) == 1

    def test_existing_instructions_preserved(self, tmp_path: Path):
        runner = _make_runner(workspace_path=tmp_path)
        existing = "You are a helpful assistant."
        result = runner._merge_heartbeat_instruction(existing)
        assert existing in result
        assert HeartbeatRunner._HEARTBEAT_INST_START in result

    def test_existing_block_is_replaced(self, tmp_path: Path):
        runner = _make_runner(workspace_path=tmp_path)
        old_block = (
            f"{HeartbeatRunner._HEARTBEAT_INST_START}\n"
            "OLD CONTENT\n"
            f"{HeartbeatRunner._HEARTBEAT_INST_END}"
        )
        existing = f"Preamble.\n\n{old_block}"
        result = runner._merge_heartbeat_instruction(existing)
        assert "OLD CONTENT" not in result
        assert "Heartbeat Mode" in result
        assert result.count(HeartbeatRunner._HEARTBEAT_INST_START) == 1

    def test_none_input_treated_as_empty(self, tmp_path: Path):
        runner = _make_runner(workspace_path=tmp_path)
        result = runner._merge_heartbeat_instruction(None)
        assert HeartbeatRunner._HEARTBEAT_INST_START in result


# ============================================================
# 6. list_due_isolated_tasks / list_due_inline_tasks
# ============================================================


class TestListDueTasks:
    """Tests for list_due_isolated_tasks and list_due_inline_tasks."""

    def _write_heartbeat_with_mixed_tasks(self, tmp_path: Path, now: datetime):
        """Write a HEARTBEAT.md with both inline and isolated due tasks."""
        past = (now - timedelta(hours=1)).isoformat()
        tasks = [
            {
                "id": "inline_task",
                "title": "Inline Job",
                "schedule": "1h",
                "state": "pending",
                "enabled": True,
                "next_run_at": past,
                "execution_mode": "inline",
                "timeout_seconds": 60,
            },
            {
                "id": "isolated_task",
                "title": "Isolated Job",
                "schedule": "1h",
                "state": "pending",
                "enabled": True,
                "next_run_at": past,
                "execution_mode": "isolated",
                "timeout_seconds": 120,
            },
        ]
        (tmp_path / "HEARTBEAT.md").write_text(
            _build_structured_md(tasks), encoding="utf-8"
        )

    def test_list_due_isolated_returns_only_isolated(self, tmp_path: Path):
        now = datetime.now(timezone.utc)
        self._write_heartbeat_with_mixed_tasks(tmp_path, now)
        runner = _make_runner(workspace_path=tmp_path)
        isolated = runner.list_due_isolated_tasks(now=now)
        assert len(isolated) == 1
        assert isolated[0]["id"] == "isolated_task"
        assert isolated[0]["execution_mode"] == "isolated"

    def test_list_due_inline_returns_only_inline(self, tmp_path: Path):
        now = datetime.now(timezone.utc)
        self._write_heartbeat_with_mixed_tasks(tmp_path, now)
        runner = _make_runner(workspace_path=tmp_path)
        inline = runner.list_due_inline_tasks(now=now)
        assert len(inline) == 1
        assert inline[0]["id"] == "inline_task"

    def test_list_due_returns_empty_when_no_heartbeat(self, tmp_path: Path):
        runner = _make_runner(workspace_path=tmp_path)
        assert runner.list_due_isolated_tasks() == []
        assert runner.list_due_inline_tasks() == []

    def test_list_due_returns_empty_when_no_tasks_due(self, tmp_path: Path):
        now = datetime.now(timezone.utc)
        future = (now + timedelta(days=30)).isoformat()
        tasks = [{
            "id": "future_task",
            "title": "Future",
            "schedule": "1d",
            "state": "pending",
            "enabled": True,
            "next_run_at": future,
            "execution_mode": "inline",
        }]
        (tmp_path / "HEARTBEAT.md").write_text(
            _build_structured_md(tasks), encoding="utf-8"
        )
        runner = _make_runner(workspace_path=tmp_path)
        assert runner.list_due_inline_tasks(now=now) == []
        assert runner.list_due_isolated_tasks(now=now) == []


# ============================================================
# 7. _execute_once — lock contention
# ============================================================


class TestExecuteOnceLockContention:
    """Tests for _execute_once when locks cannot be acquired."""

    @pytest.mark.asyncio
    async def test_acquire_session_fails_returns_skipped(self, tmp_path: Path):
        """When acquire_session returns False, result is HEARTBEAT_SKIPPED."""
        sm = _make_session_manager_with_locks(acquire_returns=False)
        runner = _make_runner(workspace_path=tmp_path, session_manager=sm)
        result = await runner._execute_once()
        assert result == "HEARTBEAT_SKIPPED"

    @pytest.mark.asyncio
    async def test_file_lock_not_acquired_returns_skipped(self, tmp_path: Path):
        """When file_lock yields False, result is HEARTBEAT_SKIPPED."""
        sm = _make_session_manager_with_locks(
            acquire_returns=True,
            file_lock_acquired=False,
        )
        runner = _make_runner(workspace_path=tmp_path, session_manager=sm)
        result = await runner._execute_once()
        assert result == "HEARTBEAT_SKIPPED"
        sm.release_session.assert_called_once()


# ============================================================
# 8. _execute_structured_tasks — timeout and failure handling
# ============================================================


class TestExecuteStructuredTasksErrors:
    """Tests for timeout and exception handling in _execute_structured_tasks."""

    def _setup_runner_with_due_task(
        self, tmp_path: Path, task_id: str = "test_task", monkeypatch=None
    ):
        """Create runner with a single due inline task and mocked internals."""
        now = datetime.now(timezone.utc)
        past = (now - timedelta(hours=1)).isoformat()
        tasks = [{
            "id": task_id,
            "title": "Due Task",
            "schedule": "1h",
            "state": "pending",
            "enabled": True,
            "next_run_at": past,
            "execution_mode": "inline",
            "timeout_seconds": 1,  # Short timeout
        }]
        hb_content = _build_structured_md(tasks)
        (tmp_path / "HEARTBEAT.md").write_text(hb_content, encoding="utf-8")

        runner = _make_runner(workspace_path=tmp_path)
        runner._read_heartbeat_md()
        return runner, hb_content

    @pytest.mark.asyncio
    async def test_inline_task_timeout(self, tmp_path: Path, monkeypatch):
        """Inline task that times out produces TASK_TIMEOUT marker internally."""
        runner, hb_content = self._setup_runner_with_due_task(tmp_path, "timeout_task")

        fake_context = SimpleNamespace(
            set_variable=MagicMock(),
            set_session_id=MagicMock(),
            get_var_value=MagicMock(return_value=""),
        )
        fake_agent = SimpleNamespace(
            executor=SimpleNamespace(context=fake_context),
        )

        async def _slow_run(*args, **kwargs):
            await asyncio.sleep(100)
            return "should not reach"

        monkeypatch.setattr(runner, "_inject_heartbeat_context", AsyncMock(return_value="execute"))
        monkeypatch.setattr(runner, "_run_agent", _slow_run)
        monkeypatch.setattr(runner, "_record_timeline_event", MagicMock())
        monkeypatch.setattr(runner, "_record_runtime_metric", MagicMock())
        monkeypatch.setattr(runner, "_write_heartbeat_event", MagicMock())
        monkeypatch.setattr(runner, "_write_heartbeat_file", MagicMock())

        result = await runner._execute_structured_tasks(
            fake_agent, hb_content, "run_test"
        )
        # TASK_TIMEOUT: prefix is filtered from user-visible output
        assert result == "HEARTBEAT_OK"
        # Verify update_task_state was called with FAILED: the task re-arms
        # as pending because it has retries left and a schedule, so we check
        # that error_message was set (indicating timeout was handled).
        task = runner._task_list.tasks[0]
        assert task.error_message == "timeout"
        # The write_heartbeat_event should have recorded the failure
        runner._write_heartbeat_event.assert_any_call(
            "task_failed", task_id="timeout_task", title="Due Task", error="timeout"
        )

    @pytest.mark.asyncio
    async def test_inline_task_exception(self, tmp_path: Path, monkeypatch):
        """Inline task that raises produces TASK_FAILED marker internally."""
        runner, hb_content = self._setup_runner_with_due_task(tmp_path, "fail_task")

        fake_context = SimpleNamespace(
            set_variable=MagicMock(),
            set_session_id=MagicMock(),
            get_var_value=MagicMock(return_value=""),
        )
        fake_agent = SimpleNamespace(
            executor=SimpleNamespace(context=fake_context),
        )

        async def _failing_run(*args, **kwargs):
            raise RuntimeError("agent crashed")

        monkeypatch.setattr(runner, "_inject_heartbeat_context", AsyncMock(return_value="execute"))
        monkeypatch.setattr(runner, "_run_agent", _failing_run)
        monkeypatch.setattr(runner, "_record_timeline_event", MagicMock())
        monkeypatch.setattr(runner, "_record_runtime_metric", MagicMock())
        monkeypatch.setattr(runner, "_write_heartbeat_event", MagicMock())
        monkeypatch.setattr(runner, "_write_heartbeat_file", MagicMock())

        result = await runner._execute_structured_tasks(
            fake_agent, hb_content, "run_test"
        )
        # TASK_FAILED: prefix is filtered from user-visible output
        assert result == "HEARTBEAT_OK"
        # Verify error was recorded on the task (re-armed as pending due to retries/schedule)
        task = runner._task_list.tasks[0]
        assert task.error_message == "agent crashed"
        runner._write_heartbeat_event.assert_any_call(
            "task_failed", task_id="fail_task", title="Due Task", error="agent crashed"
        )


# ============================================================
# 9. claim_isolated_task
# ============================================================


class TestClaimIsolatedTask:
    """Tests for claim_isolated_task."""

    @pytest.mark.asyncio
    async def test_empty_task_id_returns_false(self, tmp_path: Path):
        runner = _make_runner(workspace_path=tmp_path)
        assert await runner.claim_isolated_task("") is False
        assert await runner.claim_isolated_task(None) is False

    @pytest.mark.asyncio
    async def test_successful_claim(self, tmp_path: Path):
        """Claiming a due isolated task returns True and updates state."""
        now = datetime.now(timezone.utc)
        past = (now - timedelta(hours=1)).isoformat()
        tasks = [{
            "id": "iso_1",
            "title": "Isolated Job",
            "schedule": "1h",
            "state": "pending",
            "enabled": True,
            "next_run_at": past,
            "execution_mode": "isolated",
            "timeout_seconds": 60,
        }]
        (tmp_path / "HEARTBEAT.md").write_text(
            _build_structured_md(tasks), encoding="utf-8"
        )

        sm = _make_session_manager_with_locks(acquire_returns=True, file_lock_acquired=True)
        runner = _make_runner(workspace_path=tmp_path, session_manager=sm)

        result = await runner.claim_isolated_task("iso_1", now=now)
        assert result is True

    @pytest.mark.asyncio
    async def test_claim_nonexistent_task_returns_false(self, tmp_path: Path):
        """Claiming a task that doesn't exist returns False."""
        now = datetime.now(timezone.utc)
        past = (now - timedelta(hours=1)).isoformat()
        tasks = [{
            "id": "iso_1",
            "title": "Isolated Job",
            "schedule": "1h",
            "state": "pending",
            "enabled": True,
            "next_run_at": past,
            "execution_mode": "isolated",
        }]
        (tmp_path / "HEARTBEAT.md").write_text(
            _build_structured_md(tasks), encoding="utf-8"
        )

        sm = _make_session_manager_with_locks()
        runner = _make_runner(workspace_path=tmp_path, session_manager=sm)

        result = await runner.claim_isolated_task("nonexistent", now=now)
        assert result is False

    @pytest.mark.asyncio
    async def test_claim_fails_when_lock_unavailable(self, tmp_path: Path):
        """When file_lock is not acquired, claim returns False."""
        now = datetime.now(timezone.utc)
        past = (now - timedelta(hours=1)).isoformat()
        tasks = [{
            "id": "iso_1",
            "title": "Isolated Job",
            "schedule": "1h",
            "state": "pending",
            "enabled": True,
            "next_run_at": past,
            "execution_mode": "isolated",
        }]
        (tmp_path / "HEARTBEAT.md").write_text(
            _build_structured_md(tasks), encoding="utf-8"
        )

        sm = _make_session_manager_with_locks(acquire_returns=True, file_lock_acquired=False)
        runner = _make_runner(workspace_path=tmp_path, session_manager=sm)

        result = await runner.claim_isolated_task("iso_1", now=now)
        assert result is False


# ============================================================
# 10. execute_isolated_claimed_task
# ============================================================


class TestExecuteIsolatedClaimedTask:
    """Tests for execute_isolated_claimed_task."""

    @pytest.mark.asyncio
    async def test_successful_execution_updates_state_to_done(self, tmp_path: Path, monkeypatch):
        """Successful execution calls _update_isolated_task_state with DONE."""
        sm = _make_session_manager_with_locks()
        runner = _make_runner(workspace_path=tmp_path, session_manager=sm)

        update_calls: list[tuple] = []
        original_update = runner._update_isolated_task_state

        async def _tracking_update(task_id, state, **kwargs):
            update_calls.append((task_id, state))

        monkeypatch.setattr(runner, "_update_isolated_task_state", _tracking_update)
        monkeypatch.setattr(runner, "_execute_isolated_task", AsyncMock(return_value="result ok"))

        task_snapshot = {"id": "iso_1", "title": "Isolated Job", "execution_mode": "isolated"}
        await runner.execute_isolated_claimed_task(task_snapshot)

        assert len(update_calls) == 1
        assert update_calls[0] == ("iso_1", TaskState.DONE)

    @pytest.mark.asyncio
    async def test_execution_failure_updates_state_to_failed_and_reraises(
        self, tmp_path: Path, monkeypatch
    ):
        """Failed execution calls _update_isolated_task_state with FAILED and re-raises."""
        sm = _make_session_manager_with_locks()
        runner = _make_runner(workspace_path=tmp_path, session_manager=sm)

        update_calls: list[tuple] = []

        async def _tracking_update(task_id, state, **kwargs):
            update_calls.append((task_id, state, kwargs.get("error_message")))

        monkeypatch.setattr(runner, "_update_isolated_task_state", _tracking_update)
        monkeypatch.setattr(
            runner,
            "_execute_isolated_task",
            AsyncMock(side_effect=RuntimeError("execution exploded")),
        )

        task_snapshot = {"id": "iso_2", "title": "Failing Job", "execution_mode": "isolated"}
        with pytest.raises(RuntimeError, match="execution exploded"):
            await runner.execute_isolated_claimed_task(task_snapshot)

        assert len(update_calls) == 1
        assert update_calls[0][0] == "iso_2"
        assert update_calls[0][1] == TaskState.FAILED
        assert "execution exploded" in update_calls[0][2]

    @pytest.mark.asyncio
    async def test_empty_task_id_returns_early(self, tmp_path: Path, monkeypatch):
        """Task snapshot with empty id returns without executing."""
        sm = _make_session_manager_with_locks()
        runner = _make_runner(workspace_path=tmp_path, session_manager=sm)

        execute_mock = AsyncMock()
        monkeypatch.setattr(runner, "_execute_isolated_task", execute_mock)

        await runner.execute_isolated_claimed_task({"id": "", "title": "No ID"})
        execute_mock.assert_not_called()


# ============================================================
# 10b. _update_isolated_task_state — lock failure leaves task stuck
# ============================================================


class TestUpdateIsolatedTaskStateLockFailure:
    """Tests for _update_isolated_task_state when lock acquisition fails.

    After fix: the method retries 3 times and raises RuntimeError on failure,
    preventing tasks from being silently stuck in 'running' forever.
    """

    @pytest.mark.asyncio
    async def test_acquire_session_fails_raises_error(self, tmp_path: Path):
        """When acquire_session fails all retries, RuntimeError is raised."""
        sm = _make_session_manager_with_locks(acquire_returns=False)
        runner = _make_runner(workspace_path=tmp_path, session_manager=sm)

        tasks = [{
            "id": "stuck_1", "title": "Stuck Task", "state": "running",
            "schedule": "1d", "enabled": True, "execution_mode": "isolated",
        }]
        (tmp_path / "HEARTBEAT.md").write_text(
            _build_structured_md(tasks), encoding="utf-8"
        )
        runner._read_heartbeat_md()

        with pytest.raises(RuntimeError, match="Failed to acquire session lock"):
            await runner._update_isolated_task_state("stuck_1", TaskState.DONE)

    @pytest.mark.asyncio
    async def test_file_lock_fails_raises_error(self, tmp_path: Path):
        """When file_lock fails all retries, RuntimeError is raised."""
        sm = _make_session_manager_with_locks(
            acquire_returns=True, file_lock_acquired=False,
        )
        runner = _make_runner(workspace_path=tmp_path, session_manager=sm)

        tasks = [{
            "id": "stuck_2", "title": "Stuck Task 2", "state": "running",
            "schedule": "1d", "enabled": True, "execution_mode": "isolated",
        }]
        (tmp_path / "HEARTBEAT.md").write_text(
            _build_structured_md(tasks), encoding="utf-8"
        )
        runner._read_heartbeat_md()

        with pytest.raises(RuntimeError, match="Failed to acquire file lock"):
            await runner._update_isolated_task_state("stuck_2", TaskState.FAILED,
                                                      error_message="timed out")


# ============================================================
# 10c. End-to-end: execute succeeds but state update lock fails
# ============================================================


class TestExecuteIsolatedE2ELockFailure:
    """E2E test: isolated task executes but state update lock fails.

    After fix: lock failure raises RuntimeError which propagates to caller,
    making the failure visible instead of silently leaving task stuck.
    """

    @pytest.mark.asyncio
    async def test_execution_ok_but_state_update_lock_fails_raises(
        self, tmp_path: Path, monkeypatch
    ):
        """Task executes successfully, but lock failure in DONE state update
        raises RuntimeError — caught by except branch which also fails to
        update FAILED state, re-raising the lock error."""
        sm = _make_session_manager_with_locks(acquire_returns=False)
        runner = _make_runner(workspace_path=tmp_path, session_manager=sm)

        monkeypatch.setattr(
            runner, "_execute_isolated_task",
            AsyncMock(return_value="result ok"),
        )

        tasks = [{
            "id": "e2e_1", "title": "E2E Task", "state": "running",
            "schedule": "1d", "enabled": True, "execution_mode": "isolated",
        }]
        (tmp_path / "HEARTBEAT.md").write_text(
            _build_structured_md(tasks), encoding="utf-8"
        )

        task_snapshot = {
            "id": "e2e_1", "title": "E2E Task", "execution_mode": "isolated",
        }
        # After fix: the lock failure in _update_isolated_task_state raises
        with pytest.raises(RuntimeError, match="Failed to acquire session lock"):
            await runner.execute_isolated_claimed_task(task_snapshot)

    @pytest.mark.asyncio
    async def test_execution_fails_and_state_update_lock_also_fails(
        self, tmp_path: Path, monkeypatch
    ):
        """Task execution fails AND state update lock fails.
        The lock error from FAILED update is raised (wrapping the original)."""
        sm = _make_session_manager_with_locks(acquire_returns=False)
        runner = _make_runner(workspace_path=tmp_path, session_manager=sm)

        monkeypatch.setattr(
            runner, "_execute_isolated_task",
            AsyncMock(side_effect=RuntimeError("LLM crashed")),
        )

        tasks = [{
            "id": "e2e_2", "title": "E2E Fail Task", "state": "running",
            "schedule": "1d", "enabled": True, "execution_mode": "isolated",
        }]
        (tmp_path / "HEARTBEAT.md").write_text(
            _build_structured_md(tasks), encoding="utf-8"
        )

        task_snapshot = {
            "id": "e2e_2", "title": "E2E Fail Task",
            "execution_mode": "isolated",
        }
        # The FAILED state update also fails with lock error, which is raised
        with pytest.raises(RuntimeError):
            await runner.execute_isolated_claimed_task(task_snapshot)


# ============================================================
# 10d. _recover_stuck_running_tasks
# ============================================================


class TestRecoverStuckRunningTasks:
    """Tests for stuck task auto-recovery in list_due_isolated_tasks."""

    def test_stuck_running_task_recovered(self, tmp_path: Path):
        """Task stuck in 'running' beyond 2x timeout is reset to pending."""
        now = datetime.now(timezone.utc)
        # last_run_at was 30 minutes ago, timeout is 600s → 2x = 1200s = 20min
        long_ago = (now - timedelta(minutes=30)).isoformat()
        tasks = [{
            "id": "stuck_1", "title": "Stuck Task", "state": "running",
            "schedule": "1d", "enabled": True, "execution_mode": "isolated",
            "timeout_seconds": 600, "last_run_at": long_ago,
            "retry": 0, "max_retry": 3,
        }]
        (tmp_path / "HEARTBEAT.md").write_text(
            _build_structured_md(tasks), encoding="utf-8"
        )

        runner = _make_runner(workspace_path=tmp_path)
        runner.list_due_isolated_tasks(now=now)

        # Re-read to check — task should be recovered to pending
        runner._read_heartbeat_md()
        task = runner._file_mgr.task_list.tasks[0]
        assert task.state == "pending"
        assert task.error_message == "recovered: stuck in running state"

    def test_recently_running_task_not_recovered(self, tmp_path: Path):
        """Task running within 2x timeout is left alone."""
        now = datetime.now(timezone.utc)
        # last_run_at was 5 minutes ago, timeout is 600s → 2x = 1200s = 20min
        recent = (now - timedelta(minutes=5)).isoformat()
        tasks = [{
            "id": "active_1", "title": "Active Task", "state": "running",
            "schedule": "1d", "enabled": True, "execution_mode": "isolated",
            "timeout_seconds": 600, "last_run_at": recent,
        }]
        (tmp_path / "HEARTBEAT.md").write_text(
            _build_structured_md(tasks), encoding="utf-8"
        )

        runner = _make_runner(workspace_path=tmp_path)
        runner.list_due_isolated_tasks(now=now)

        runner._read_heartbeat_md()
        task = runner._file_mgr.task_list.tasks[0]
        assert task.state == "running"  # Not recovered

    def test_inline_running_task_not_recovered(self, tmp_path: Path):
        """Only isolated tasks are recovered, not inline."""
        now = datetime.now(timezone.utc)
        long_ago = (now - timedelta(minutes=30)).isoformat()
        tasks = [{
            "id": "inline_stuck", "title": "Inline Stuck", "state": "running",
            "schedule": "1d", "enabled": True, "execution_mode": "inline",
            "timeout_seconds": 600, "last_run_at": long_ago,
        }]
        (tmp_path / "HEARTBEAT.md").write_text(
            _build_structured_md(tasks), encoding="utf-8"
        )

        runner = _make_runner(workspace_path=tmp_path)
        runner.list_due_isolated_tasks(now=now)

        runner._read_heartbeat_md()
        task = runner._file_mgr.task_list.tasks[0]
        assert task.state == "running"  # Not recovered (inline)


# ============================================================
# 11. _is_permanent_error — status code + string matching
# ============================================================


class TestIsPermanentError:
    """Tests for the _is_permanent_error helper."""

    def test_status_code_402(self):
        exc = Exception("payment required")
        exc.status_code = 402
        assert _is_permanent_error(exc) is True

    def test_response_status_code_401(self):
        resp = SimpleNamespace(status_code=401)
        exc = Exception("unauthorized")
        exc.response = resp
        assert _is_permanent_error(exc) is True

    def test_status_attribute_403(self):
        exc = Exception("forbidden")
        exc.status = 403
        assert _is_permanent_error(exc) is True

    def test_status_code_429_transient(self):
        """429 is not in the permanent set; falls through to string matching."""
        exc = Exception("rate limited")
        exc.status_code = 429
        assert _is_permanent_error(exc) is False

    def test_string_match_without_status_code(self):
        exc = Exception("insufficient balance on account")
        assert _is_permanent_error(exc) is True

    def test_no_match(self):
        exc = Exception("connection timeout")
        assert _is_permanent_error(exc) is False
