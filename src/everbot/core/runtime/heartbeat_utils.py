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
        "skill": getattr(task, "skill", None),
        "scanner": getattr(task, "scanner", None),
        "min_execution_interval": getattr(task, "min_execution_interval", None),
        "retry": int(getattr(task, "retry", 0) or 0),
        "max_retry": int(getattr(task, "max_retry", 3) or 3),
        "last_run_at": getattr(task, "last_run_at", None),
    }


def build_job_session_id(task: Any) -> str:
    """Build one isolated job session id."""
    task_id = str(getattr(task, "id", "task"))
    return f"job_{task_id}_{uuid.uuid4().hex[:8]}"


def build_isolated_task_prompt(task: Any) -> str:
    """Build the user-message prompt for an isolated task execution.

    Single source of truth — used by both heartbeat.py and cron.py.
    """
    task_id = str(getattr(task, "id", "task"))
    task_title = str(getattr(task, "title", "") or "")
    task_desc = str(getattr(task, "description", "") or "")
    return (
        "Execute this scheduled isolated routine task and summarize the result briefly.\n"
        "IMPORTANT: Respond in the SAME language as the task title/description. "
        "Use a consistent, structured format (headings + bullet points).\n\n"
        f"Task ID: {task_id}\n"
        f"Title: {task_title}\n"
        f"Description: {task_desc}\n"
    )
