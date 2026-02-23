"""IsolatedTaskMixin — isolated task lifecycle management for HeartbeatRunner."""

import uuid
from datetime import datetime
from typing import Optional, Any
import logging

from ..tasks.task_manager import (
    Task,
    get_due_tasks,
    claim_task,
    update_task_state,
    TaskState,
)
from .heartbeat_utils import task_snapshot

logger = logging.getLogger(__name__)


class IsolatedTaskMixin:
    """Mixin providing isolated task listing, claiming, and execution lifecycle.

    Expects the host class to provide:
        - self.session_manager
        - self.session_id (property)
        - self._file_mgr (HeartbeatFileManager with .task_list)
        - self._read_heartbeat_md()
        - self._flush_task_state()
        - self._task_snapshot(task) (static)
        - self._execute_isolated_task(task, run_id) (async)
    """

    def list_due_isolated_tasks(self, now: Optional[datetime] = None) -> list[dict[str, Any]]:
        """List due isolated tasks for external scheduler routing."""
        heartbeat_content = self._read_heartbeat_md()
        if not heartbeat_content or self._file_mgr.task_list is None:
            return []
        due = get_due_tasks(self._file_mgr.task_list, now=now)
        isolated = []
        for task in due:
            mode = str(getattr(task, "execution_mode", "inline") or "inline")
            if mode != "isolated":
                continue
            snapshot = self._task_snapshot(task)
            if snapshot["id"]:
                isolated.append(snapshot)
        return isolated

    def list_due_inline_tasks(self, now: Optional[datetime] = None) -> list[dict[str, Any]]:
        """List due inline tasks for external scheduler routing."""
        heartbeat_content = self._read_heartbeat_md()
        if not heartbeat_content or self._file_mgr.task_list is None:
            return []
        due = get_due_tasks(self._file_mgr.task_list, now=now)
        inline = []
        for task in due:
            mode = str(getattr(task, "execution_mode", "inline") or "inline")
            if mode == "isolated":
                continue
            snapshot = self._task_snapshot(task)
            if snapshot["id"]:
                inline.append(snapshot)
        return inline

    def _claim_isolated_task_under_lock(self, task_id: str, now: Optional[datetime] = None) -> bool:
        """Claim one isolated task while heartbeat lock is held."""
        heartbeat_content = self._read_heartbeat_md()
        if not heartbeat_content or self._file_mgr.task_list is None:
            return False
        due = get_due_tasks(self._file_mgr.task_list, now=now)
        for task in due:
            mode = str(getattr(task, "execution_mode", "inline") or "inline")
            if mode != "isolated":
                continue
            if str(getattr(task, "id", "")) != task_id:
                continue
            if not claim_task(task, now=now):
                return False
            self._flush_task_state()
            return True
        return False

    async def claim_isolated_task(self, task_id: str, now: Optional[datetime] = None) -> bool:
        """Claim one isolated task with heartbeat session lock protection."""
        task_id = str(task_id or "").strip()
        if not task_id:
            return False

        inproc_acquired = await self.session_manager.acquire_session(self.session_id, timeout=0.1)
        if not inproc_acquired:
            return False
        try:
            with self.session_manager.file_lock(self.session_id, blocking=False) as acquired:
                if not acquired:
                    return False
                return self._claim_isolated_task_under_lock(task_id, now=now)
        finally:
            self.session_manager.release_session(self.session_id)

    async def _update_isolated_task_state(
        self,
        task_id: str,
        state: TaskState,
        *,
        error_message: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> None:
        """Update one isolated task state under heartbeat lock and flush file."""
        inproc_acquired = await self.session_manager.acquire_session(self.session_id, timeout=5.0)
        if not inproc_acquired:
            return
        try:
            with self.session_manager.file_lock(self.session_id, blocking=True) as acquired:
                if not acquired:
                    return
                self._apply_isolated_task_state_under_lock(
                    task_id, state, error_message=error_message, now=now,
                )
        finally:
            self.session_manager.release_session(self.session_id)

    def _apply_isolated_task_state_under_lock(
        self,
        task_id: str,
        state: TaskState,
        *,
        error_message: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> None:
        """Apply isolated-task state change while heartbeat lock is held."""
        heartbeat_content = self._read_heartbeat_md()
        if not heartbeat_content or self._file_mgr.task_list is None:
            return
        for task in self._file_mgr.task_list.tasks:
            if str(getattr(task, "id", "")) != task_id:
                continue
            update_task_state(task, state, error_message=error_message, now=now)
            self._flush_task_state()
            return

    async def execute_isolated_claimed_task(
        self,
        task_snapshot_dict: dict[str, Any],
        *,
        run_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> None:
        """Execute one already-claimed isolated task and persist final state."""
        task_id = str(task_snapshot_dict.get("id") or "").strip()
        if not task_id:
            return
        try:
            task = Task.from_dict(task_snapshot_dict)
            task.execution_mode = "isolated"
            active_run_id = run_id or f"heartbeat_isolated_{uuid.uuid4().hex[:12]}"
            await self._execute_isolated_task(task, active_run_id)
            await self._update_isolated_task_state(task_id, TaskState.DONE, now=now)
        except Exception as exc:
            await self._update_isolated_task_state(
                task_id,
                TaskState.FAILED,
                error_message=str(exc),
                now=now,
            )
            raise
