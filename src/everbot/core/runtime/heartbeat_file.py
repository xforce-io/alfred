"""HeartbeatFileManager — HEARTBEAT.md file I/O and task snapshot management."""

import json
from datetime import datetime
from pathlib import Path
from typing import Optional
import logging

from ..session.persistence import SessionPersistence
from ..tasks.task_manager import (
    TaskList,
    ParseResult,
    ParseStatus,
    parse_heartbeat_md,
    get_due_tasks,
    write_task_block,
    purge_stale_tasks,
)

logger = logging.getLogger(__name__)


class HeartbeatFileManager:
    """Manages HEARTBEAT.md reading/writing and task snapshots."""

    def __init__(self, workspace_path: Path):
        self.workspace_path = Path(workspace_path)
        self.task_list: Optional[TaskList] = None
        self.heartbeat_mode: str = "idle"
        self.last_parse_result: Optional[ParseResult] = None

    def read_heartbeat_md(self) -> Optional[str]:
        """Read HEARTBEAT.md and decide heartbeat execution mode."""
        path = self.workspace_path / "HEARTBEAT.md"
        if not path.exists():
            self.task_list = None
            self.last_parse_result = ParseResult(status=ParseStatus.EMPTY)
            self.heartbeat_mode = "idle"
            return None

        try:
            content = path.read_text(encoding="utf-8")
            parse_result = parse_heartbeat_md(content)
            self.last_parse_result = parse_result

            if parse_result.status == ParseStatus.OK and parse_result.task_list is not None:
                task_list = parse_result.task_list
                self.task_list = task_list
                due = get_due_tasks(task_list)
                if due:
                    self.heartbeat_mode = "structured_due"
                    return content
                self.heartbeat_mode = "structured_reflect"
                return content

            if parse_result.status == ParseStatus.CORRUPTED:
                self.task_list = None
                self.heartbeat_mode = "corrupted"
                return content

            # No structured JSON block found — treat as idle
            self.task_list = None
            self.heartbeat_mode = "idle"
            return None
        except Exception as e:
            logger.error("Failed to read HEARTBEAT.md: %s", e)
            self.task_list = None
            self.last_parse_result = None
            self.heartbeat_mode = "idle"
            return None

    def write_heartbeat_file(self, content: str) -> None:
        """Persist HEARTBEAT.md atomically with .bak rotation."""
        hb_path = self.workspace_path / "HEARTBEAT.md"
        SessionPersistence.atomic_save(hb_path, content.encode("utf-8"))

    def flush_task_state(self) -> None:
        """Persist current task_list state to HEARTBEAT.md atomically."""
        task_list = self.task_list
        if task_list is None:
            return
        hb_path = self.workspace_path / "HEARTBEAT.md"
        try:
            purged = purge_stale_tasks(task_list)
            if purged:
                logger.info("Purged %d stale task(s) from HEARTBEAT.md", purged)
            content = hb_path.read_text(encoding="utf-8")
            updated = write_task_block(content, task_list)
            self.write_heartbeat_file(updated)
        except Exception as exc:
            logger.warning("Failed to update HEARTBEAT.md task state: %s", exc)

    def snapshot_path(self) -> Path:
        return self.workspace_path / ".heartbeat_snapshot.json"

    def write_task_snapshot(self, task_list: TaskList) -> None:
        """Persist latest parsed task list as recovery snapshot."""
        payload = {
            "saved_at": datetime.now().isoformat(),
            "task_list": task_list.to_dict(),
        }
        serialized = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        SessionPersistence.atomic_save(self.snapshot_path(), serialized)

    def load_task_snapshot(self) -> Optional[dict]:
        """Load task snapshot for corruption-repair context."""
        snap_path = self.snapshot_path()
        if not snap_path.exists():
            return None
        try:
            return json.loads(snap_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to read heartbeat snapshot: %s", exc)
            return None

    def render_snapshot_summary(self) -> str:
        """Render compact task summary from snapshot for prompt injection."""
        snapshot = self.load_task_snapshot()
        if not snapshot:
            return "(no snapshot available)"
        task_list = snapshot.get("task_list", {})
        tasks = task_list.get("tasks", []) if isinstance(task_list, dict) else []
        if not tasks:
            return "(snapshot exists but contains no tasks)"
        lines = []
        for task in tasks[:10]:
            if not isinstance(task, dict):
                continue
            task_id = task.get("id", "unknown")
            title = task.get("title", "")
            schedule = task.get("schedule", "")
            lines.append(f"- {task_id}: {title} ({schedule})")
        return "\n".join(lines) if lines else "(snapshot has no valid tasks)"
