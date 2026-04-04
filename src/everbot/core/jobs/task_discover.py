"""Task discovery skill — find actionable tasks from conversation history.

Notification strategy: notifies user via mailbox when new tasks are found.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List

from ..runtime.skill_context import SkillContext
from ..scanners.session_scanner import SessionScanner
from ..scanners.reflection_state import ReflectionState
from .llm_utils import parse_json_response

logger = logging.getLogger(__name__)

_STATE_FILENAME = ".task_discover_state.json"
_SIMILARITY_THRESHOLD = 0.5
_MAX_PENDING_TASKS = 3
_EXPIRE_DAYS = 7


@dataclass
class DiscoveredTask:
    """A task discovered from conversation analysis."""

    title: str
    description: str
    urgency: str  # high | medium | low
    source_session_id: str
    discovered_at: str
    expires_at: str

    @property
    def expired(self) -> bool:
        try:
            return datetime.fromisoformat(self.expires_at) < datetime.now(timezone.utc)
        except (ValueError, TypeError):
            return False

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "description": self.description,
            "urgency": self.urgency,
            "source_session_id": self.source_session_id,
            "discovered_at": self.discovered_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DiscoveredTask":
        return cls(
            title=data.get("title", ""),
            description=data.get("description", ""),
            urgency=data.get("urgency", "medium"),
            source_session_id=data.get("source_session_id", ""),
            discovered_at=data.get("discovered_at", ""),
            expires_at=data.get("expires_at", ""),
        )


@dataclass
class TaskDiscoverState:
    """Persistent state for task discovery."""

    pending_tasks: List[DiscoveredTask] = field(default_factory=list)

    @classmethod
    def load(cls, workspace_path: Path) -> "TaskDiscoverState":
        state_file = Path(workspace_path) / _STATE_FILENAME
        if not state_file.exists():
            return cls()
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            tasks = [DiscoveredTask.from_dict(t) for t in data.get("pending_tasks", [])]
            return cls(pending_tasks=tasks)
        except Exception as e:
            logger.warning("Failed to load task discover state: %s", e)
            return cls()

    def save(self, workspace_path: Path) -> None:
        state_file = Path(workspace_path) / _STATE_FILENAME
        state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_file.with_suffix(".json.tmp")
        data = json.dumps(
            {"pending_tasks": [t.to_dict() for t in self.pending_tasks]},
            ensure_ascii=False, indent=2,
        )
        try:
            tmp.write_text(data, encoding="utf-8")
            os.replace(tmp, state_file)
        except Exception as e:
            logger.error("Failed to save task discover state: %s", e)
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass


async def run(context: SkillContext) -> str:
    """Execute task discovery from recent sessions."""
    scanner = SessionScanner(context.sessions_dir)
    state = ReflectionState.load(context.workspace_path)

    # Get sessions: reuse gate result if available, otherwise query directly
    skill_wm = state.get_watermark("task-discover")
    if context.scan_result and context.scan_result.payload:
        sessions = context.scan_result.payload
    else:
        sessions = scanner.get_reviewable_sessions(skill_wm, agent_name=context.agent_name)
    if not sessions:
        return "No sessions to analyze"

    digests = []
    last_successful_session = None
    for s in sessions:
        try:
            digests.append(scanner.extract_digest(s.path))
            last_successful_session = s
        except Exception as e:
            logger.warning("Failed to extract session %s: %s, skipping", s.id, e)
            continue

    if not digests:
        return "All sessions failed to extract"

    # LLM analysis
    task_state = TaskDiscoverState.load(context.workspace_path)
    existing_titles = [t.title for t in task_state.pending_tasks if not t.expired]
    new_tasks = await _discover_tasks(context.llm, digests, existing_titles)

    # Clean expired + append new + hard limit
    task_state.pending_tasks = [t for t in task_state.pending_tasks if not t.expired]
    if new_tasks:
        # Dedup against existing
        new_tasks = _dedup_tasks(new_tasks, task_state.pending_tasks)
        task_state.pending_tasks = (task_state.pending_tasks + new_tasks)[:_MAX_PENDING_TASKS]
        if new_tasks:
            await context.mailbox.deposit(
                summary=f"发现 {len(new_tasks)} 个待办任务",
                detail=_format_task_proposals(new_tasks),
            )
    task_state.save(context.workspace_path)

    # Advance watermark
    if last_successful_session:
        state.set_watermark("task-discover", last_successful_session.updated_at)
        state.save(context.workspace_path)

    return f"Discovered {len(new_tasks)} tasks"


async def _discover_tasks(llm, digests: List[str], existing_titles: List[str]) -> List[DiscoveredTask]:
    """Use LLM to discover actionable tasks from session digests."""
    context_text = "\n".join(d[:800] for d in digests[:5])
    existing_text = "\n".join(f"- {t}" for t in existing_titles) if existing_titles else "(none)"

    prompt = f"""Analyze these recent conversations and identify actionable tasks that the user mentioned but hasn't completed.

## Recent Conversations
{context_text}

## Already Tracked Tasks
{existing_text}

## Rules
- Only identify tasks the user explicitly mentioned wanting to do
- Skip tasks that seem already completed in the conversations
- Skip vague wishes — only include actionable, specific tasks
- Do NOT duplicate already tracked tasks
- Maximum 3 new tasks

Output format:
```json
{{
  "tasks": [
    {{
      "title": "Short task title",
      "description": "What needs to be done",
      "urgency": "high|medium|low",
      "source_hint": "Brief reference to the conversation"
    }}
  ]
}}
```"""

    response = await llm.complete(prompt, system="You are a task discovery engine. Output valid JSON only.")
    result = parse_json_response(response)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=_EXPIRE_DAYS)

    tasks = []
    for item in result.get("tasks", [])[:3]:
        tasks.append(DiscoveredTask(
            title=item.get("title", ""),
            description=item.get("description", ""),
            urgency=item.get("urgency", "medium"),
            source_session_id="",
            discovered_at=now.isoformat(),
            expires_at=expires.isoformat(),
        ))
    return tasks


def _dedup_tasks(new_tasks: List[DiscoveredTask], existing: List[DiscoveredTask]) -> List[DiscoveredTask]:
    """Remove tasks too similar to existing ones (Jaccard >= 0.5)."""
    from ..memory.merger import token_similarity

    result = []
    existing_titles = [t.title for t in existing]
    for task in new_tasks:
        is_dup = any(
            token_similarity(task.title, et) >= _SIMILARITY_THRESHOLD
            for et in existing_titles
        )
        if not is_dup:
            result.append(task)
            existing_titles.append(task.title)
    return result


def _format_task_proposals(tasks: List[DiscoveredTask]) -> str:
    """Format discovered tasks for mailbox notification."""
    lines = ["## 发现的待办任务\n"]
    for i, task in enumerate(tasks, 1):
        urgency_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(task.urgency, "⚪")
        lines.append(f"### {i}. {urgency_icon} {task.title}")
        lines.append(f"{task.description}\n")
    return "\n".join(lines)

