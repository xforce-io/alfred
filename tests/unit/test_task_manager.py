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
