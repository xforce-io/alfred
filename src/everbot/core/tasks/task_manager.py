"""
Structured task management for HEARTBEAT.md.

Parses JSON task blocks from HEARTBEAT.md, manages task state machine,
and falls back to legacy regex parsing when no JSON block is found.
"""

import json
import re
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from enum import Enum
from typing import List, Optional, Dict, Any

logger = logging.getLogger(__name__)


class TaskState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Task:
    id: str
    title: str
    description: str = ""
    source: str = "manual"
    enabled: bool = True
    schedule: Optional[str] = None  # cron expression or interval string
    timezone: Optional[str] = None  # IANA timezone for schedule interpretation
    execution_mode: str = "inline"
    state: str = TaskState.PENDING.value
    last_run_at: Optional[str] = None
    next_run_at: Optional[str] = None
    timeout_seconds: int = 120
    retry: int = 0
    max_retry: int = 3
    error_message: Optional[str] = None
    created_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Task":
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        # Backward compatibility for v1 blocks
        filtered.setdefault("description", "")
        filtered.setdefault("source", "manual")
        filtered.setdefault("enabled", True)
        filtered.setdefault("timezone", None)
        filtered.setdefault("execution_mode", "inline")
        return cls(**filtered)


@dataclass
class TaskList:
    version: int = 1
    tasks: List[Task] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"version": self.version, "tasks": [t.to_dict() for t in self.tasks]}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskList":
        version = data.get("version", 1)
        tasks = [Task.from_dict(t) for t in data.get("tasks", [])]
        return cls(version=version, tasks=tasks)


class ParseStatus(str, Enum):
    OK = "ok"
    CORRUPTED = "corrupted"
    EMPTY = "empty"


@dataclass
class ParseResult:
    status: ParseStatus
    task_list: Optional[TaskList] = None
    parse_error: Optional[str] = None
    raw_json_content: Optional[str] = None

    @property
    def is_ok(self) -> bool:
        return self.status == ParseStatus.OK and self.task_list is not None


# ── JSON block markers ────────────────────────────────────────────
_JSON_BLOCK_RE = re.compile(
    r"```json\s*\n(.*?)\n\s*```",
    re.DOTALL,
)


def parse_heartbeat_md(content: str) -> ParseResult:
    """Parse structured JSON block from HEARTBEAT.md.

    Returns a ParseResult with one of:
    - ok: valid JSON block with task payload
    - corrupted: JSON block exists but cannot be parsed/validated
    - empty: no JSON block found
    """
    match = _JSON_BLOCK_RE.search(content)
    if not match:
        return ParseResult(status=ParseStatus.EMPTY)

    raw_json = match.group(1)
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        logger.warning("Failed to parse JSON task block: %s", exc)
        return ParseResult(
            status=ParseStatus.CORRUPTED,
            parse_error=str(exc),
            raw_json_content=raw_json,
        )

    if not isinstance(data, dict):
        return ParseResult(
            status=ParseStatus.CORRUPTED,
            parse_error="JSON block must be an object.",
            raw_json_content=raw_json,
        )
    if "tasks" not in data or not isinstance(data.get("tasks"), list):
        return ParseResult(
            status=ParseStatus.CORRUPTED,
            parse_error='JSON block must include list field "tasks".',
            raw_json_content=raw_json,
        )

    try:
        task_list = TaskList.from_dict(data)
    except (TypeError, KeyError, ValueError) as exc:
        logger.warning("Failed to parse JSON task block: %s", exc)
        return ParseResult(
            status=ParseStatus.CORRUPTED,
            parse_error=str(exc),
            raw_json_content=raw_json,
        )
    return ParseResult(status=ParseStatus.OK, task_list=task_list)


def _resolve_schedule_timezone(task_timezone: Optional[str], now: datetime):
    """Resolve timezone for schedule computation."""
    if task_timezone:
        try:
            return ZoneInfo(task_timezone)
        except Exception:
            logger.warning("Invalid task timezone: %s, fallback to UTC", task_timezone)
    if now.tzinfo is not None:
        return now.tzinfo
    return timezone.utc


def _compute_next_run(
    schedule: Optional[str],
    now: datetime,
    task_timezone: Optional[str] = None,
) -> Optional[str]:
    """Compute next run time from a cron expression or interval string.

    Supports:
    - croniter-style cron expressions (if croniter is installed)
    - Simple interval strings like "30m", "1h", "2d"
    """
    if not schedule:
        return None

    tz = _resolve_schedule_timezone(task_timezone, now)
    now_local = now.astimezone(tz) if now.tzinfo else now.replace(tzinfo=tz)

    # Try simple interval first: "30m", "1h", "2d"
    interval_match = re.fullmatch(r"(\d+)([mhd])", schedule.strip())
    if interval_match:
        amount = int(interval_match.group(1))
        unit = interval_match.group(2)
        delta = {"m": timedelta(minutes=amount),
                 "h": timedelta(hours=amount),
                 "d": timedelta(days=amount)}[unit]
        return (now_local + delta).isoformat()

    # Try croniter for cron expressions
    try:
        from croniter import croniter
        cron = croniter(schedule, now_local)
        next_dt = cron.get_next(datetime)
        if next_dt.tzinfo is None:
            next_dt = next_dt.replace(tzinfo=tz)
        return next_dt.isoformat()
    except Exception:
        logger.debug("croniter unavailable or invalid schedule: %s", schedule)
        return None


def _parse_iso_datetime(s: str) -> Optional[datetime]:
    """Parse an ISO-8601 datetime string to a timezone-aware datetime."""
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def get_due_tasks(task_list: TaskList, now: Optional[datetime] = None) -> List[Task]:
    """Return tasks whose next_run_at <= now and state is pending."""
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    due = []
    for task in task_list.tasks:
        if task.enabled is False:
            continue
        if task.state != TaskState.PENDING.value:
            continue
        if task.next_run_at is None:
            due.append(task)
            continue
        next_run_dt = _parse_iso_datetime(task.next_run_at)
        if next_run_dt is not None and next_run_dt <= now:
            due.append(task)
    return due


def claim_task(task: Task, now: Optional[datetime] = None) -> bool:
    """Atomically-like in-memory claim helper for scheduler usage.

    Returns True only when the task is pending and due at claim time.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    if task.enabled is False:
        return False
    if task.state != TaskState.PENDING.value:
        return False
    if task.next_run_at:
        next_run_dt = _parse_iso_datetime(task.next_run_at)
        if next_run_dt is not None and next_run_dt > now:
            return False

    update_task_state(task, TaskState.RUNNING, now=now)
    return True


def update_task_state(
    task: Task,
    new_state: TaskState,
    *,
    error_message: Optional[str] = None,
    now: Optional[datetime] = None,
) -> None:
    """Transition a task to a new state, updating metadata fields."""
    if now is None:
        now = datetime.now(timezone.utc)

    task.state = new_state.value

    if new_state == TaskState.RUNNING:
        task.last_run_at = now.isoformat()
    elif new_state == TaskState.DONE:
        task.last_run_at = now.isoformat()
        task.retry = 0
        task.error_message = None
        # Schedule next run
        next_run = _compute_next_run(task.schedule, now, task.timezone)
        if next_run:
            task.next_run_at = next_run
            task.state = TaskState.PENDING.value  # re-arm for next cycle
    elif new_state == TaskState.FAILED:
        task.error_message = error_message
        task.retry += 1
        if task.retry < task.max_retry:
            # Re-arm as pending for retry
            task.state = TaskState.PENDING.value
        elif task.schedule:
            # Scheduled task: reset retry counter and re-arm for next cycle
            task.retry = 0
            task.state = TaskState.PENDING.value
            next_run = _compute_next_run(task.schedule, now, task.timezone)
            if next_run:
                task.next_run_at = next_run
        else:
            task.state = TaskState.FAILED.value


_STALE_THRESHOLD = timedelta(days=7)


def purge_stale_tasks(
    task_list: TaskList,
    now: Optional[datetime] = None,
    threshold: timedelta = _STALE_THRESHOLD,
) -> int:
    """Remove terminal tasks older than *threshold* from *task_list*.

    Purges:
    - One-shot done tasks (state=done, no schedule) with last_run_at older than threshold
    - One-shot failed tasks (state=failed, no schedule, retry exhausted) with last_run_at older than threshold

    Scheduled tasks are never purged (they re-arm automatically).

    Returns the number of removed tasks.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    cutoff = now - threshold
    keep: List[Task] = []
    removed = 0

    for task in task_list.tasks:
        if task.schedule is None and task.state == TaskState.DONE.value:
            last_run = _parse_iso_datetime(task.last_run_at) if task.last_run_at else None
            if last_run is not None and last_run < cutoff:
                removed += 1
                continue
        elif task.schedule is None and task.state == TaskState.FAILED.value and task.retry >= task.max_retry:
            last_run = _parse_iso_datetime(task.last_run_at) if task.last_run_at else None
            if last_run is not None and last_run < cutoff:
                removed += 1
                continue
        keep.append(task)

    task_list.tasks = keep
    return removed


def heal_stuck_scheduled_tasks(
    task_list: TaskList,
    now: Optional[datetime] = None,
) -> int:
    """Re-arm scheduled tasks stuck in 'failed' state.

    This handles a legacy edge case: tasks that exhausted retries before the
    auto-reset logic was added (commit c2d67b8) remain stuck because
    ``get_due_tasks`` skips non-pending tasks.  Calling this at heartbeat
    startup resets them so they can be scheduled for the next cycle.

    Returns the number of healed tasks.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    healed = 0
    for task in task_list.tasks:
        if (
            task.schedule
            and task.state == TaskState.FAILED.value
            and task.retry >= task.max_retry
        ):
            task.retry = 0
            task.state = TaskState.PENDING.value
            task.error_message = None
            next_run = _compute_next_run(task.schedule, now, task.timezone)
            if next_run:
                task.next_run_at = next_run
            healed += 1
            logger.info(
                "Healed stuck scheduled task %s (%s): re-armed as pending, next_run=%s",
                task.id, task.title, task.next_run_at,
            )
    return healed


def write_task_block(content: str, task_list: TaskList) -> str:
    """Replace the JSON task block in HEARTBEAT.md content, or append one."""
    block_json = json.dumps(task_list.to_dict(), indent=2, ensure_ascii=False)
    new_block = f"```json\n{block_json}\n```"

    if _JSON_BLOCK_RE.search(content):
        return _JSON_BLOCK_RE.sub(new_block, content, count=1)

    # Append under a Tasks heading
    return content.rstrip() + f"\n\n## Tasks\n\n{new_block}\n"
