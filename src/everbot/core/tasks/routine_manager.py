"""Routine management helpers backed by HEARTBEAT.md JSON task block."""

from __future__ import annotations

import fcntl
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import uuid

logger = logging.getLogger(__name__)

from ..session.session import SessionPersistence
from .task_manager import (
    Task,
    TaskList,
    TaskState,
    ParseStatus,
    parse_heartbeat_md,
    write_task_block,
    purge_stale_tasks,
    get_due_tasks,
    claim_task,
    update_task_state,
    heal_stuck_scheduled_tasks,
    parse_iso_datetime,
    _compute_next_run,
)


def _detect_local_iana_timezone() -> str:
    """Best-effort detection of local IANA timezone name (e.g. 'Asia/Shanghai').

    Tries /etc/localtime symlink (macOS/Linux), then falls back to UTC offset string.
    """
    # macOS: /etc/localtime -> /var/db/timezone/zoneinfo/Asia/Shanghai
    # Linux: /etc/localtime -> /usr/share/zoneinfo/Asia/Shanghai
    # resolve() may follow to zoneinfo.default/ — match any zoneinfo* directory
    try:
        target = Path("/etc/localtime").resolve()
        parts = target.parts
        for i, part in enumerate(parts):
            if part.startswith("zoneinfo") and i + 1 < len(parts):
                return "/".join(parts[i + 1 :])
    except Exception:
        pass
    # Fallback: UTC offset like "UTC+08:00"
    try:
        offset = datetime.now().astimezone().strftime("%z")  # e.g. "+0800"
        h, m = int(offset[:3]), int(offset[0] + offset[3:5])
        sign = "+" if h >= 0 else "-"
        return f"UTC{sign}{abs(h):02d}:{abs(m):02d}"
    except Exception:
        return "UTC"


class RoutineManager:
    """CRUD helper for structured routines in HEARTBEAT.md."""

    DEFAULT_CONTENT = "# HEARTBEAT\n\n## Tasks\n\n"
    ACTIVE_ROUTINE_SOFT_LIMIT = 20

    def __init__(self, workspace_path: Path):
        self.workspace_path = Path(workspace_path)
        self.heartbeat_path = self.workspace_path / "HEARTBEAT.md"

    def _read_content(self) -> str:
        if not self.heartbeat_path.exists():
            return self.DEFAULT_CONTENT
        try:
            return self.heartbeat_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise ValueError("HEARTBEAT.md contains invalid UTF-8 bytes")

    def _load_task_list(self) -> tuple[str, TaskList]:
        content = self._read_content()
        parsed = parse_heartbeat_md(content)
        if parsed.status == ParseStatus.CORRUPTED:
            # Auto-recover from .bak if available, instead of staying broken
            # indefinitely (production: 95 heartbeats over 35h reported anomaly).
            bak_path = self.heartbeat_path.with_suffix(
                self.heartbeat_path.suffix + ".bak"
            )
            if bak_path.exists():
                logger.warning(
                    "HEARTBEAT.md corrupted; recovering from %s", bak_path,
                )
                try:
                    bak_content = bak_path.read_text(encoding="utf-8")
                    bak_parsed = parse_heartbeat_md(bak_content)
                    if bak_parsed.status == ParseStatus.OK and bak_parsed.task_list:
                        # Restore the good backup over the corrupted file
                        SessionPersistence.atomic_save(
                            self.heartbeat_path, bak_content.encode("utf-8"),
                        )
                        content = bak_content
                        parsed = bak_parsed
                except Exception:
                    logger.exception("Failed to recover from .bak")
            if parsed.status == ParseStatus.CORRUPTED:
                raise ValueError("HEARTBEAT.md task block is corrupted")
        if parsed.status == ParseStatus.OK and parsed.task_list is not None:
            task_list = parsed.task_list
        else:
            task_list = TaskList(version=2, tasks=[])
        if int(float(task_list.version or 0)) < 2:
            task_list.version = 2
        return content, task_list

    def _save_task_list(self, base_content: str, task_list: TaskList) -> None:
        """Persist task list with file-level lock to prevent concurrent corruption.

        Production bug: rapid CLI calls (3 in 24s) caused a read-modify-write
        race that corrupted HEARTBEAT.md with invalid control characters.
        """
        task_list.version = max(2, int(float(task_list.version or 0)))
        # File lock prevents concurrent read-modify-write races between
        # heartbeat runtime and CLI processes writing to the same file.
        lock_path = self.heartbeat_path.with_suffix(
            self.heartbeat_path.suffix + ".lock"
        )
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                # Re-read inside lock to get the latest markdown structure,
                # so we don't overwrite concurrent changes to non-task sections.
                fresh_content = self._read_content()
                updated = write_task_block(fresh_content, task_list)
                SessionPersistence.atomic_save(
                    self.heartbeat_path, updated.encode("utf-8"),
                )
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)

    @staticmethod
    def infer_execution_mode(
        *,
        description: str = "",
        timeout_seconds: Optional[int] = None,
    ) -> str:
        """Infer execution mode when caller does not specify one explicitly."""
        try:
            if timeout_seconds is not None and int(timeout_seconds) > 60:
                return "isolated"
        except Exception:
            pass
        if len(str(description or "").strip()) > 200:
            return "isolated"
        return "inline"

    @classmethod
    def _normalize_execution_mode(
        cls,
        value: Optional[str],
        *,
        description: str = "",
        timeout_seconds: Optional[int] = None,
    ) -> str:
        mode = str(value or "auto").strip().lower()
        if mode == "auto":
            return cls.infer_execution_mode(
                description=description,
                timeout_seconds=timeout_seconds,
            )
        if mode not in {"inline", "isolated"}:
            raise ValueError(f"Invalid execution_mode: {value!r}")
        return mode

    @staticmethod
    def _dedupe_key(task: Task) -> tuple[str, str]:
        return (str(task.title or "").strip().lower(), str(task.schedule or "").strip())

    def list_routines(self, *, include_disabled: bool = True) -> List[Dict[str, Any]]:
        """List routines from HEARTBEAT.md."""
        _, task_list = self._load_task_list()
        routines: List[Dict[str, Any]] = []
        for task in task_list.tasks:
            if (not include_disabled) and (task.enabled is False):
                continue
            routines.append(task.to_dict())
        return routines

    def add_routine(
        self,
        *,
        title: str,
        description: str = "",
        schedule: Optional[str] = None,
        execution_mode: str = "auto",
        timezone_name: Optional[str] = None,
        source: str = "manual",
        enabled: bool = True,
        timeout_seconds: Optional[int] = None,
        task_id: Optional[str] = None,
        allow_duplicate: bool = False,
        now: Optional[datetime] = None,
        next_run_at: Optional[str] = None,
        skill: Optional[str] = None,
        scanner: Optional[str] = None,
        min_execution_interval: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Add one routine and persist task block."""
        title = str(title or "").strip()
        if not title:
            raise ValueError("title is required")
        description = str(description or "")
        timeout_value = max(1, int(timeout_seconds or 120))
        mode = self._normalize_execution_mode(
            execution_mode,
            description=description,
            timeout_seconds=timeout_seconds,
        )
        task_id = str(task_id or f"routine_{uuid.uuid4().hex[:8]}")
        now_dt = now or datetime.now(timezone.utc)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)

        # Default timezone to local system timezone when not specified for scheduled tasks
        if schedule and not timezone_name:
            timezone_name = _detect_local_iana_timezone()
            logger.warning(
                "No timezone specified for scheduled routine '%s'; "
                "defaulting to '%s'. Pass --timezone explicitly for reliability.",
                title, timezone_name,
            )

        content, task_list = self._load_task_list()
        if any(str(task.id) == task_id for task in task_list.tasks):
            raise ValueError(f"task_id already exists: {task_id}")

        if bool(enabled):
            active_routines = sum(1 for task in task_list.tasks if task.enabled is not False)
            if active_routines >= self.ACTIVE_ROUTINE_SOFT_LIMIT:
                raise ValueError(
                    f"active routine soft limit exceeded ({self.ACTIVE_ROUTINE_SOFT_LIMIT})"
                )

        if not allow_duplicate:
            new_key = (title.lower(), str(schedule or "").strip())
            for existing in task_list.tasks:
                if existing.enabled is False:
                    continue
                if self._dedupe_key(existing) == new_key:
                    raise ValueError("duplicate routine detected")

        if min_execution_interval is not None:
            if not re.fullmatch(r"\d+[mhd]", min_execution_interval.strip()):
                raise ValueError(
                    f"Invalid min_execution_interval: {min_execution_interval!r}. "
                    "Use e.g. '30m', '2h', '1d'."
                )

        # High-frequency schedules (< 30m) require skill + scanner to prevent
        # uncontrolled LLM execution every cycle.
        if schedule:
            _freq_match = re.fullmatch(r"(\d+)([mhd])", schedule.strip())
            if _freq_match:
                _amount = int(_freq_match.group(1))
                _unit = _freq_match.group(2)
                _total_minutes = {"m": _amount, "h": _amount * 60, "d": _amount * 1440}[_unit]
                if _total_minutes < 30 and not (skill and scanner):
                    raise ValueError(
                        f"High-frequency schedule '{schedule}' (< 30m) requires "
                        "--skill and --scanner to prevent uncontrolled execution."
                    )

        if next_run_at is not None:
            computed_next_run = next_run_at
        elif schedule:
            computed_next_run = _compute_next_run(schedule, now_dt, timezone_name)
        else:
            computed_next_run = None
        task = Task(
            id=task_id,
            title=title,
            description=description,
            source=str(source or "manual"),
            enabled=bool(enabled),
            schedule=schedule,
            timezone=timezone_name,
            execution_mode=mode,
            state="pending",
            next_run_at=computed_next_run,
            timeout_seconds=timeout_value,
            created_at=now_dt.isoformat(),
            skill=skill,
            scanner=scanner,
            min_execution_interval=min_execution_interval,
        )
        task_list.tasks.append(task)
        self._save_task_list(content, task_list)
        return task.to_dict()

    def update_routine(
        self,
        task_id: str,
        *,
        title: Optional[str] = None,
        description: Optional[str] = None,
        schedule: Optional[str] = None,
        execution_mode: Optional[str] = None,
        timezone_name: Optional[str] = None,
        source: Optional[str] = None,
        enabled: Optional[bool] = None,
        timeout_seconds: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> Optional[Dict[str, Any]]:
        """Update one routine by task_id."""
        target_id = str(task_id or "").strip()
        if not target_id:
            return None
        now_dt = now or datetime.now(timezone.utc)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)

        content, task_list = self._load_task_list()
        for task in task_list.tasks:
            if str(task.id) != target_id:
                continue
            if title is not None:
                new_title = str(title).strip()
                if not new_title:
                    raise ValueError("title cannot be empty")
                task.title = new_title
            if description is not None:
                task.description = str(description)
            if execution_mode is not None:
                inferred_description = str(description) if description is not None else str(task.description or "")
                inferred_timeout = (
                    max(1, int(timeout_seconds))
                    if timeout_seconds is not None
                    else int(task.timeout_seconds or 120)
                )
                task.execution_mode = self._normalize_execution_mode(
                    execution_mode,
                    description=inferred_description,
                    timeout_seconds=inferred_timeout,
                )
            if source is not None:
                task.source = str(source)
            if enabled is not None:
                task.enabled = bool(enabled)
            if timeout_seconds is not None:
                task.timeout_seconds = max(1, int(timeout_seconds))
            if schedule is not None:
                task.schedule = schedule
            if timezone_name is not None:
                task.timezone = timezone_name
            if (schedule is not None) or (timezone_name is not None):
                task.next_run_at = _compute_next_run(task.schedule, now_dt, task.timezone) if task.schedule else None
                task.state = "pending"
                task.retry = 0
                task.error_message = None
            self._save_task_list(content, task_list)
            return task.to_dict()
        return None

    def remove_routine(self, task_id: str, *, soft_disable: bool = True) -> bool:
        """Remove one routine by task_id, or disable it if soft_disable is True."""
        target_id = str(task_id or "").strip()
        if not target_id:
            return False
        content, task_list = self._load_task_list()
        for idx, task in enumerate(task_list.tasks):
            if str(task.id) != target_id:
                continue
            if soft_disable:
                task.enabled = False
                task.state = "pending"
            else:
                task_list.tasks.pop(idx)
            self._save_task_list(content, task_list)
            return True
        return False

    # ── Cron execution interface ─────────────────────────────────
    # These methods provide the task-state operations that CronExecutor
    # needs, keeping RoutineManager as the single writer to HEARTBEAT.md.

    def load_task_list(self) -> Optional[TaskList]:
        """Load and return the current TaskList, or None if empty/missing."""
        try:
            _, task_list = self._load_task_list()
            return task_list
        except ValueError:
            return None

    def get_due_tasks(self, now: Optional[datetime] = None) -> List[Task]:
        """Return tasks whose next_run_at <= now and state is pending."""
        task_list = self.load_task_list()
        if task_list is None:
            return []
        return get_due_tasks(task_list, now=now)

    def claim_task(self, task: Task, now: Optional[datetime] = None) -> bool:
        """Claim a task for execution (set state to running)."""
        return claim_task(task, now=now)

    def update_task_state(
        self,
        task: Task,
        new_state: TaskState,
        *,
        error_message: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> None:
        """Transition a task to a new state."""
        update_task_state(task, new_state, error_message=error_message, now=now)

    def flush(self, task_list: TaskList) -> None:
        """Persist task_list state to HEARTBEAT.md atomically.

        Purges stale one-shot tasks before writing.
        """
        hb_path = self.heartbeat_path
        try:
            purged = purge_stale_tasks(task_list)
            if purged:
                logger.info("Purged %d stale task(s) from HEARTBEAT.md", purged)
            content = self._read_content()
            updated = write_task_block(content, task_list)
            SessionPersistence.atomic_save(hb_path, updated.encode("utf-8"))
        except Exception as exc:
            logger.warning("Failed to flush task state to HEARTBEAT.md: %s", exc)

    def heal_stuck_tasks(self, now: Optional[datetime] = None) -> int:
        """Re-arm scheduled tasks stuck in 'failed' state. Returns count healed."""
        task_list = self.load_task_list()
        if task_list is None:
            return 0
        healed = heal_stuck_scheduled_tasks(task_list, now=now)
        if healed:
            self.flush(task_list)
        return healed

    def recover_stuck_running_tasks(
        self,
        task_list: TaskList,
        *,
        timeout_multiplier: int = 2,
        now: Optional[datetime] = None,
    ) -> List[Task]:
        """Reset tasks stuck in 'running' beyond timeout_multiplier * timeout.

        Returns list of recovered tasks.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        recovered: List[Task] = []
        for task in task_list.tasks:
            if task.state != TaskState.RUNNING.value:
                continue
            last_run = parse_iso_datetime(task.last_run_at) if task.last_run_at else None
            if last_run is None:
                continue
            timeout = max(int(getattr(task, "timeout_seconds", 600) or 600), 60)
            stuck_threshold = timedelta(seconds=timeout * timeout_multiplier)
            if now - last_run > stuck_threshold:
                logger.warning(
                    "Recovering stuck task %s (%s): running since %s, threshold=%ss",
                    task.id, task.title, task.last_run_at, timeout * timeout_multiplier,
                )
                update_task_state(
                    task, TaskState.FAILED,
                    error_message="recovered: stuck in running state",
                    now=now,
                )
                recovered.append(task)

        if recovered:
            self.flush(task_list)
        return recovered
