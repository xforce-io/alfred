"""Pure utility functions for heartbeat processing."""

import uuid
from datetime import datetime
from typing import Optional, Any


def is_time_reminder_task(task: Any) -> bool:
    """Identify time reminder tasks by id/title/description heuristics."""
    parts = []
    for attr in ("id", "title", "description"):
        value = getattr(task, attr, "")
        if isinstance(value, str) and value.strip():
            parts.append(value.lower())
    if not parts:
        return False
    joined = " ".join(parts)
    markers = (
        "time_reminder",
        "time reminder",
        "当前时间",
        "报时",
    )
    return any(marker in joined for marker in markers)


def try_deterministic_task(task: Any) -> Optional[str]:
    """Return programmatic output for deterministic tasks, or None to fall through to LLM."""
    if is_time_reminder_task(task):
        return f"当前时间：{datetime.now().strftime('%Y年%m月%d日 %H:%M')}\nHEARTBEAT_OK"
    return None


def task_snapshot(task: Any) -> dict[str, Any]:
    """Build one lightweight task snapshot for scheduler handoff."""
    return {
        "id": str(getattr(task, "id", "")),
        "title": str(getattr(task, "title", "")),
        "description": str(getattr(task, "description", "") or ""),
        "execution_mode": str(getattr(task, "execution_mode", "inline") or "inline"),
        "timeout_seconds": int(getattr(task, "timeout_seconds", 120) or 120),
        "schedule": getattr(task, "schedule", None),
        "timezone": getattr(task, "timezone", None),
    }


def build_job_session_id(task: Any) -> str:
    """Build one isolated job session id."""
    task_id = str(getattr(task, "id", "task"))
    return f"job_{task_id}_{uuid.uuid4().hex[:8]}"
