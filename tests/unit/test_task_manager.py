"""Tests for structured HEARTBEAT.md task parsing and state machine."""

import json
import pytest
from datetime import datetime, timezone

from src.everbot.core.tasks.task_manager import (
    Task,
    TaskList,
    TaskState,
    ParseStatus,
    parse_heartbeat_md,
    get_due_tasks,
    claim_task,
    update_task_state,
    write_task_block,
    heal_stuck_scheduled_tasks,
)


# ── Fixtures ──────────────────────────────────────────────────────

def _sample_task(**overrides) -> Task:
    defaults = {
        "id": "daily_report",
        "title": "Generate daily report",
        "schedule": None,
        "state": "pending",
        "next_run_at": None,
        "timeout_seconds": 120,
        "retry": 0,
        "max_retry": 3,
    }
    defaults.update(overrides)
    return Task(**defaults)


def _sample_md(task_dict=None) -> str:
    if task_dict is None:
        task_dict = {
            "version": 1,
            "tasks": [
                {
                    "id": "daily_report",
                    "title": "Generate daily report",
                    "schedule": "0 9 * * *",
                    "state": "pending",
                    "last_run_at": None,
                    "next_run_at": "2026-02-11T09:00:00+00:00",
                    "timeout_seconds": 120,
                    "retry": 0,
                    "max_retry": 3,
                    "error_message": None,
                }
            ],
        }
    block = json.dumps(task_dict, indent=2)
    return f"# HEARTBEAT\n\n## Tasks\n\n```json\n{block}\n```\n"


# ── Parsing ───────────────────────────────────────────────────────

class TestParseHeartbeatMd:
    def test_parse_valid_json_block(self):
        md = _sample_md()
        result = parse_heartbeat_md(md)
        assert result.status == ParseStatus.OK
        assert result.task_list is not None
        assert len(result.task_list.tasks) == 1
        assert result.task_list.tasks[0].id == "daily_report"
        assert result.task_list.tasks[0].state == "pending"

    def test_no_json_block_returns_none(self):
        md = "# HEARTBEAT\n\n## TODO\n\n- [ ] Do something\n"
        result = parse_heartbeat_md(md)
        assert result.status == ParseStatus.EMPTY

    def test_malformed_json_returns_none(self):
        md = "```json\n{not valid json\n```\n"
        result = parse_heartbeat_md(md)
        assert result.status == ParseStatus.CORRUPTED
        assert result.parse_error

    def test_json_without_tasks_key_returns_none(self):
        md = '```json\n{"version": 1}\n```\n'
        result = parse_heartbeat_md(md)
        assert result.status == ParseStatus.CORRUPTED

    def test_multiple_tasks_parsed(self):
        data = {
            "version": 1,
            "tasks": [
                {"id": "t1", "title": "Task 1"},
                {"id": "t2", "title": "Task 2"},
            ],
        }
        result = parse_heartbeat_md(_sample_md(data))
        assert result.status == ParseStatus.OK
        assert result.task_list is not None
        assert len(result.task_list.tasks) == 2

    def test_v1_fields_are_backfilled(self):
        data = {
            "version": 1,
            "tasks": [{"id": "t1", "title": "Task 1"}],
        }
        result = parse_heartbeat_md(_sample_md(data))
        assert result.status == ParseStatus.OK
        task = result.task_list.tasks[0]
        assert task.description == ""
        assert task.source == "manual"
        assert task.enabled is True
        assert task.timezone is None
        assert task.execution_mode == "inline"


# ── Due tasks ─────────────────────────────────────────────────────

class TestGetDueTasks:
    def test_due_when_next_run_in_past(self):
        now = datetime(2026, 2, 11, 10, 0, tzinfo=timezone.utc)
        task = _sample_task(
            next_run_at="2026-02-11T09:00:00+00:00",
        )
        tl = TaskList(tasks=[task])
        due = get_due_tasks(tl, now)
        assert len(due) == 1
        assert due[0].id == "daily_report"

    def test_not_due_when_next_run_in_future(self):
        now = datetime(2026, 2, 11, 8, 0, tzinfo=timezone.utc)
        task = _sample_task(
            next_run_at="2026-02-11T09:00:00+00:00",
        )
        tl = TaskList(tasks=[task])
        due = get_due_tasks(tl, now)
        assert len(due) == 0

    def test_due_when_next_run_is_none(self):
        """Tasks with no next_run_at are always due."""
        task = _sample_task(next_run_at=None)
        tl = TaskList(tasks=[task])
        due = get_due_tasks(tl)
        assert len(due) == 1

    def test_skips_non_pending_tasks(self):
        task = _sample_task(state="running")
        tl = TaskList(tasks=[task])
        due = get_due_tasks(tl)
        assert len(due) == 0

    def test_skips_disabled_tasks(self):
        task = _sample_task(enabled=False)
        tl = TaskList(tasks=[task])
        due = get_due_tasks(tl)
        assert len(due) == 0


class TestTaskClaim:
    def test_claim_due_task_moves_to_running(self):
        task = _sample_task(next_run_at=None, state="pending")
        ok = claim_task(task, now=datetime(2026, 2, 11, 9, 0, tzinfo=timezone.utc))
        assert ok is True
        assert task.state == "running"

    def test_claim_future_task_returns_false(self):
        task = _sample_task(next_run_at="2099-01-01T00:00:00+00:00", state="pending")
        ok = claim_task(task, now=datetime(2026, 2, 11, 9, 0, tzinfo=timezone.utc))
        assert ok is False
        assert task.state == "pending"


# ── State machine ────────────────────────────────────────────────

class TestTaskStateTransitions:
    def test_pending_to_running(self):
        task = _sample_task()
        now = datetime(2026, 2, 11, 9, 0, tzinfo=timezone.utc)
        update_task_state(task, TaskState.RUNNING, now=now)
        assert task.state == "running"
        assert task.last_run_at == now.isoformat()

    def test_running_to_done(self):
        task = _sample_task(state="running")
        now = datetime(2026, 2, 11, 9, 5, tzinfo=timezone.utc)
        update_task_state(task, TaskState.DONE, now=now)
        assert task.error_message is None
        assert task.retry == 0

    def test_done_with_schedule_rearms_pending(self):
        task = _sample_task(state="running", schedule="30m")
        now = datetime(2026, 2, 11, 9, 5, tzinfo=timezone.utc)
        update_task_state(task, TaskState.DONE, now=now)
        assert task.state == "pending"
        assert task.next_run_at is not None

    def test_failed_with_retry_rearms_pending(self):
        task = _sample_task(retry=0, max_retry=3)
        update_task_state(task, TaskState.FAILED, error_message="timeout")
        assert task.state == "pending"
        assert task.retry == 1
        assert task.error_message == "timeout"

    def test_failed_at_max_retry_stays_failed(self):
        task = _sample_task(retry=2, max_retry=3)
        update_task_state(task, TaskState.FAILED, error_message="timeout")
        assert task.state == "failed"
        assert task.retry == 3

    def test_timeout_marks_failed(self):
        """Simulates the heartbeat timeout → failed flow."""
        task = _sample_task()
        update_task_state(task, TaskState.RUNNING)
        assert task.state == "running"

        update_task_state(task, TaskState.FAILED, error_message="timeout")
        assert task.state == "pending"  # retryable
        assert task.retry == 1

    def test_done_with_interval_uses_task_timezone(self):
        task = _sample_task(state="running", schedule="1h")
        task.timezone = "America/Los_Angeles"
        now = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
        update_task_state(task, TaskState.DONE, now=now)
        assert task.next_run_at is not None
        assert task.next_run_at.endswith("-08:00")

    def test_scheduled_task_at_max_retry_rearms_for_next_cycle(self):
        """Scheduled task reaching max_retry should auto-reset, not stay failed.

        Production bug: demo_agent had two 1d-scheduled tasks stuck in 'failed'
        state with retry=3/3. The code at task_manager.py:287-293 should handle
        this by resetting retry=0 and re-arming as pending.
        """
        task = _sample_task(retry=2, max_retry=3, schedule="1d")
        task.timezone = "Asia/Shanghai"
        now = datetime(2026, 2, 25, 12, 0, tzinfo=timezone.utc)
        update_task_state(task, TaskState.FAILED, error_message="Request timed out.", now=now)
        assert task.state == "pending", (
            f"Scheduled task should re-arm as pending after max_retry, got '{task.state}'"
        )
        assert task.retry == 0, (
            f"Retry counter should reset to 0 for next cycle, got {task.retry}"
        )
        assert task.next_run_at is not None, (
            "next_run_at should be recalculated for next cycle"
        )

    def test_scheduled_task_at_max_retry_clears_error_on_rearm(self):
        """After auto-reset, the error_message should still be set (for diagnostics)
        but the task should be schedulable."""
        task = _sample_task(retry=2, max_retry=3, schedule="1d")
        now = datetime(2026, 2, 25, 12, 0, tzinfo=timezone.utc)
        update_task_state(task, TaskState.FAILED, error_message="Connection error.", now=now)
        # Task is re-armed; error_message is preserved for diagnostics
        assert task.state == "pending"
        # next_run_at should be ~1 day in the future
        next_run = datetime.fromisoformat(task.next_run_at)
        assert next_run > now

    def test_scheduled_task_progressive_retry_then_reset(self):
        """Full lifecycle: 3 failures → auto-reset → pending for next cycle."""
        task = _sample_task(retry=0, max_retry=3, schedule="1d")
        now = datetime(2026, 2, 25, 12, 0, tzinfo=timezone.utc)

        # Fail 1: re-arm as pending (retry < max)
        update_task_state(task, TaskState.FAILED, error_message="err1", now=now)
        assert task.state == "pending"
        assert task.retry == 1

        # Fail 2: re-arm as pending (retry < max)
        update_task_state(task, TaskState.FAILED, error_message="err2", now=now)
        assert task.state == "pending"
        assert task.retry == 2

        # Fail 3: max_retry reached → scheduled task auto-resets for next cycle
        update_task_state(task, TaskState.FAILED, error_message="err3", now=now)
        assert task.state == "pending", "Scheduled task should reset, not stay failed"
        assert task.retry == 0, "Retry counter should reset after max_retry cycle"
        assert task.next_run_at is not None


# ── _compute_next_run edge cases ─────────────────────────────────

class TestComputeNextRun:
    """Test interval format parsing used by demo_agent's scheduled tasks."""

    @pytest.mark.parametrize("schedule,expected_unit", [
        ("30m", "minutes"),
        ("1h", "hours"),
        ("1d", "days"),
        ("7d", "days"),
        ("14d", "days"),
    ])
    def test_interval_formats_produce_valid_next_run(self, schedule, expected_unit):
        from src.everbot.core.tasks.task_manager import _compute_next_run
        now = datetime(2026, 2, 25, 12, 0, tzinfo=timezone.utc)
        result = _compute_next_run(schedule, now)
        assert result is not None, f"Schedule '{schedule}' should produce a valid next_run"
        next_dt = datetime.fromisoformat(result)
        assert next_dt > now, f"next_run should be in the future for '{schedule}'"

    def test_interval_1d_with_timezone(self):
        from src.everbot.core.tasks.task_manager import _compute_next_run
        now = datetime(2026, 2, 25, 4, 0, tzinfo=timezone.utc)  # noon in Asia/Shanghai
        result = _compute_next_run("1d", now, "Asia/Shanghai")
        assert result is not None
        next_dt = datetime.fromisoformat(result)
        assert next_dt > now

    def test_none_schedule_returns_none(self):
        from src.everbot.core.tasks.task_manager import _compute_next_run
        now = datetime(2026, 2, 25, 12, 0, tzinfo=timezone.utc)
        assert _compute_next_run(None, now) is None

    def test_empty_schedule_returns_none(self):
        from src.everbot.core.tasks.task_manager import _compute_next_run
        now = datetime(2026, 2, 25, 12, 0, tzinfo=timezone.utc)
        assert _compute_next_run("", now) is None

    def test_invalid_schedule_returns_none(self):
        from src.everbot.core.tasks.task_manager import _compute_next_run
        now = datetime(2026, 2, 25, 12, 0, tzinfo=timezone.utc)
        assert _compute_next_run("every tuesday", now) is None


# ── Write back ────────────────────────────────────────────────────

class TestWriteTaskBlock:
    def test_replaces_existing_block(self):
        md = _sample_md()
        parsed = parse_heartbeat_md(md)
        assert parsed.task_list is not None
        parsed.task_list.tasks[0].state = "done"

        updated = write_task_block(md, parsed.task_list)
        parsed2 = parse_heartbeat_md(updated)
        assert parsed2.status == ParseStatus.OK
        assert parsed2.task_list is not None
        assert parsed2.task_list.tasks[0].state == "done"

    def test_appends_block_when_none_exists(self):
        md = "# HEARTBEAT\n\nSome text.\n"
        tl = TaskList(tasks=[_sample_task()])
        updated = write_task_block(md, tl)
        assert "```json" in updated
        parsed = parse_heartbeat_md(updated)
        assert parsed.status == ParseStatus.OK
        assert parsed.task_list is not None
        assert len(parsed.task_list.tasks) == 1


# ── Round-trip ────────────────────────────────────────────────────

class TestRoundTrip:
    def test_task_dict_round_trip(self):
        task = _sample_task(schedule="1h")
        d = task.to_dict()
        task2 = Task.from_dict(d)
        assert task2.id == task.id
        assert task2.schedule == "1h"

    def test_task_list_dict_round_trip(self):
        tl = TaskList(tasks=[_sample_task(), _sample_task(id="t2", title="T2")])
        d = tl.to_dict()
        tl2 = TaskList.from_dict(d)
        assert len(tl2.tasks) == 2
        assert tl2.tasks[1].id == "t2"


# ── heal_stuck_scheduled_tasks ────────────────────────────────────

class TestHealStuckScheduledTasks:
    """Verify heal_stuck_scheduled_tasks re-arms scheduled tasks stuck in failed.

    Root cause: tasks that exhausted retries before auto-reset logic existed
    (pre-c2d67b8) remain in state=failed because get_due_tasks skips them.
    """

    def test_heals_failed_scheduled_task(self):
        task = _sample_task(
            state="failed", retry=3, max_retry=3, schedule="1d",
        )
        task.timezone = "Asia/Shanghai"
        task.error_message = "Request timed out."
        tl = TaskList(tasks=[task])
        now = datetime(2026, 2, 26, 12, 0, tzinfo=timezone.utc)

        healed = heal_stuck_scheduled_tasks(tl, now=now)
        assert healed == 1
        assert task.state == "pending"
        assert task.retry == 0
        assert task.error_message is None
        assert task.next_run_at is not None
        next_dt = datetime.fromisoformat(task.next_run_at)
        assert next_dt > now

    def test_does_not_heal_one_shot_failed_task(self):
        task = _sample_task(
            state="failed", retry=3, max_retry=3, schedule=None,
        )
        tl = TaskList(tasks=[task])
        healed = heal_stuck_scheduled_tasks(tl)
        assert healed == 0
        assert task.state == "failed"

    def test_does_not_heal_pending_task(self):
        task = _sample_task(
            state="pending", retry=0, max_retry=3, schedule="1d",
        )
        tl = TaskList(tasks=[task])
        healed = heal_stuck_scheduled_tasks(tl)
        assert healed == 0

    def test_does_not_heal_running_task(self):
        task = _sample_task(
            state="running", retry=1, max_retry=3, schedule="1d",
        )
        tl = TaskList(tasks=[task])
        healed = heal_stuck_scheduled_tasks(tl)
        assert healed == 0

    def test_does_not_heal_failed_with_retries_remaining(self):
        """Task with retry < max_retry isn't stuck — it will be retried normally."""
        task = _sample_task(
            state="failed", retry=1, max_retry=3, schedule="1d",
        )
        tl = TaskList(tasks=[task])
        healed = heal_stuck_scheduled_tasks(tl)
        assert healed == 0

    def test_heals_multiple_stuck_tasks(self):
        t1 = _sample_task(id="t1", state="failed", retry=3, max_retry=3, schedule="1d")
        t2 = _sample_task(id="t2", state="failed", retry=3, max_retry=3, schedule="7d")
        t3 = _sample_task(id="t3", state="pending", schedule="1d")
        tl = TaskList(tasks=[t1, t2, t3])
        now = datetime(2026, 2, 26, 12, 0, tzinfo=timezone.utc)

        healed = heal_stuck_scheduled_tasks(tl, now=now)
        assert healed == 2
        assert t1.state == "pending"
        assert t2.state == "pending"
        assert t3.state == "pending"  # unchanged
