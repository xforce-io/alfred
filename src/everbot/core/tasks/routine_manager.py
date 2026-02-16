"""Routine management helpers backed by HEARTBEAT.md JSON task block."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import uuid

from ..session.session import SessionPersistence
from .task_manager import (
    Task,
    TaskList,
    ParseStatus,
    parse_heartbeat_md,
    write_task_block,
    _compute_next_run,
)


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
        return self.heartbeat_path.read_text(encoding="utf-8")

    def _load_task_list(self) -> tuple[str, TaskList]:
        content = self._read_content()
        parsed = parse_heartbeat_md(content)
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
        task_list.version = max(2, int(float(task_list.version or 0)))
        updated = write_task_block(base_content, task_list)
        SessionPersistence.atomic_save(self.heartbeat_path, updated.encode("utf-8"))

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
