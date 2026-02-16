"""Context strategy abstractions for runtime session types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol

from .mailbox import compose_message_with_mailbox_updates


@dataclass
class BuildMessageResult:
    """Build result for one turn message."""

    message: str
    mailbox_ack_ids: List[str] = field(default_factory=list)


@dataclass
class RuntimeDeps:
    """Runtime dependencies used by context strategies."""

    load_workspace_instructions: Callable[[str], str]
    list_due_tasks: Optional[Callable[[str], Iterable[Dict[str, Any]]]] = None
    heartbeat_instructions: str = ""


class ContextStrategy(Protocol):
    """Protocol for session-type-specific prompt and message construction."""

    def build_system_prompt(self, session: Any, deps: RuntimeDeps) -> str:
        """Build system prompt for one turn."""
        ...

    def build_message(self, session: Any, trigger: str, deps: RuntimeDeps) -> BuildMessageResult:
        """Build user message and mailbox ack ids for one turn."""
        ...


def _agent_name_from_session(session: Any) -> str:
    """Extract agent_name from a session-like object safely."""
    return str(getattr(session, "agent_name", "") or "")


class PrimaryContextStrategy:
    """Primary chat strategy: stable prompt + mailbox message prefix."""

    def build_system_prompt(self, session: Any, deps: RuntimeDeps) -> str:
        return deps.load_workspace_instructions(_agent_name_from_session(session))

    def build_message(self, session: Any, trigger: str, deps: RuntimeDeps) -> BuildMessageResult:
        mailbox = getattr(session, "mailbox", []) or []
        message, ack_ids = compose_message_with_mailbox_updates(trigger, mailbox)
        return BuildMessageResult(message=message, mailbox_ack_ids=ack_ids)


class HeartbeatContextStrategy:
    """Heartbeat strategy: stable heartbeat prompt + dynamic due tasks in message."""

    def build_system_prompt(self, session: Any, deps: RuntimeDeps) -> str:
        base = deps.load_workspace_instructions(_agent_name_from_session(session))
        hb = (deps.heartbeat_instructions or "").strip()
        if not hb:
            return base
        if not base:
            return hb
        return f"{base}\n\n{hb}"

    def build_message(self, session: Any, trigger: str, deps: RuntimeDeps) -> BuildMessageResult:
        parts = [trigger]
        if deps.list_due_tasks is not None:
            due_tasks = list(deps.list_due_tasks(_agent_name_from_session(session)) or [])
            if due_tasks:
                parts.append("")
                parts.append("## Due Tasks")
                for task in due_tasks:
                    task_id = str(task.get("id") or "")
                    title = str(task.get("title") or "")
                    desc = str(task.get("description") or "")
                    parts.append(f"- [{task_id}] {title}: {desc}")
        return BuildMessageResult(message="\n".join(parts))


class JobContextStrategy:
    """Job strategy: base instructions + per-job instructions."""

    def build_system_prompt(self, session: Any, deps: RuntimeDeps) -> str:
        base = deps.load_workspace_instructions(_agent_name_from_session(session))
        variables = getattr(session, "variables", {}) or {}
        job_instructions = str(variables.get("job_instructions") or "").strip()
        if not job_instructions:
            return base
        if not base:
            return job_instructions
        return f"{base}\n\n{job_instructions}"

    def build_message(self, session: Any, trigger: str, deps: RuntimeDeps) -> BuildMessageResult:
        return BuildMessageResult(message=trigger)


def build_default_context_strategies() -> Dict[str, ContextStrategy]:
    """Build default strategy registry keyed by session type."""
    return {
        "primary": PrimaryContextStrategy(),
        "heartbeat": HeartbeatContextStrategy(),
        "job": JobContextStrategy(),
        "sub": PrimaryContextStrategy(),
    }

