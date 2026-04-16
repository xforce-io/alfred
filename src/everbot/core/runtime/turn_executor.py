"""Turn executor with session-level lock and strategy-based context build."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .context_strategy import ContextStrategy, RuntimeDeps, build_default_context_strategies
from .turn_orchestrator import TurnOrchestrator
from .turn_policy import (
    TurnPolicy,
    HEARTBEAT_POLICY,
    JOB_POLICY,
    WORKFLOW_POLICY,
    TurnEventType,
)

# Map session types to their turn policies.
_SESSION_TYPE_POLICIES: Dict[str, TurnPolicy] = {
    "heartbeat": HEARTBEAT_POLICY,
    "job": JOB_POLICY,
    "workflow": WORKFLOW_POLICY,
}


@dataclass
class TurnResult:
    """Aggregated turn execution result."""

    session_id: str
    events: List[Dict[str, Any]] = field(default_factory=list)


class TurnExecutor:
    """Execute one session turn with lock + context strategy."""

    _MAX_CACHED_LOCKS = 200

    def __init__(
        self,
        runtime_deps: RuntimeDeps,
        *,
        strategies: Optional[Dict[str, ContextStrategy]] = None,
    ):
        self._runtime_deps = runtime_deps
        self._strategies = strategies or build_default_context_strategies()
        self._session_locks: Dict[str, asyncio.Lock] = {}

    def _get_lock(self, session_id: str) -> asyncio.Lock:
        if session_id not in self._session_locks:
            # Evict unlocked entries when cache is full
            if len(self._session_locks) >= self._MAX_CACHED_LOCKS:
                to_remove = [
                    k for k, v in self._session_locks.items()
                    if not v.locked()
                ]
                for k in to_remove[:len(to_remove) // 2]:
                    del self._session_locks[k]
            self._session_locks[session_id] = asyncio.Lock()
        return self._session_locks[session_id]

    @staticmethod
    def _get_session_type(session: Any) -> str:
        return str(getattr(session, "session_type", "") or "primary")

    async def stream_turn(
        self,
        *,
        session_id: str,
        trigger: str,
        load_session: Callable[[str], Awaitable[Any]],
        get_or_create_agent: Callable[[Any], Awaitable[Any]],
        save_session: Callable[[str, Any], Awaitable[None]],
        ack_mailbox_events: Optional[Callable[[str, List[str]], Awaitable[Any]]] = None,
        stream_mode: str = "delta",
    ):
        """Execute one turn and stream low-level events."""
        async with self._get_lock(session_id):
            session = await load_session(session_id)
            if session is None:
                raise ValueError(f"Session not found: {session_id}")

            agent = await get_or_create_agent(session)
            session_type = self._get_session_type(session)
            strategy = self._strategies.get(session_type, self._strategies["primary"])

            system_prompt = strategy.build_system_prompt(session, self._runtime_deps)
            built = strategy.build_message(session, trigger, self._runtime_deps)

            policy = _SESSION_TYPE_POLICIES.get(session_type)
            if policy is not None:
                # Build tool-name lookup for phantom-tool guard.
                # Re-evaluate get_toolkit_raw() on each call so dynamically
                # registered tools are visible immediately.
                get_tools = None
                if hasattr(agent, "get_toolkit_raw") and agent.get_toolkit_raw() is not None:
                    get_tools = lambda: set(agent.get_toolkit_raw().getToolNames())  # noqa: E731
                orchestrator = TurnOrchestrator(policy, get_registered_tools=get_tools)
                async for event in orchestrator.run_turn(
                    agent,
                    built.message,
                    system_prompt=system_prompt,
                    stream_mode=stream_mode,
                ):
                    if event.type == TurnEventType.TURN_ERROR:
                        yield {"_turn_error": event.error}
                        break
                    elif event.type == TurnEventType.TURN_COMPLETE:
                        break
                    else:
                        yield self._turn_event_to_raw(event)
            else:
                async for event in agent.continue_chat(
                    message=built.message,
                    stream_mode=stream_mode,
                    system_prompt=system_prompt,
                ):
                    yield event

            await save_session(session_id, agent)
            if ack_mailbox_events is not None and built.mailbox_ack_ids:
                await ack_mailbox_events(session_id, built.mailbox_ack_ids)

    @staticmethod
    def _turn_event_to_raw(event: Any) -> Dict[str, Any]:
        """Convert a TurnEvent back to raw dolphin-style event dict for consumers."""
        progress: Dict[str, Any] = {}
        if event.type == TurnEventType.LLM_DELTA:
            progress = {"stage": "llm", "delta": event.content, "answer": ""}
        elif event.type == TurnEventType.LLM_ROUND_RESET:
            progress = {"stage": "round_reset"}
        elif event.type == TurnEventType.TOOL_CALL:
            progress = {"stage": "tool_call", "tool_name": event.tool_name, "args": event.tool_args,
                        "id": event.pid, "status": event.status}
        elif event.type == TurnEventType.TOOL_OUTPUT:
            progress = {"stage": "tool_output", "tool_name": event.tool_name, "output": event.tool_output,
                        "id": event.pid, "status": event.status, "reference_id": event.reference_id}
        elif event.type == TurnEventType.SKILL:
            progress = {"stage": "skill", "skill_info": {"name": event.skill_name, "args": event.skill_args},
                        "answer": event.skill_output, "id": event.pid, "status": event.status}
        elif event.type == TurnEventType.STATUS:
            progress = {"stage": "llm", "delta": "", "answer": ""}
        else:
            return {}
        return {"_progress": [progress]}

    async def execute_turn(
        self,
        *,
        session_id: str,
        trigger: str,
        load_session: Callable[[str], Awaitable[Any]],
        get_or_create_agent: Callable[[Any], Awaitable[Any]],
        save_session: Callable[[str, Any], Awaitable[None]],
        ack_mailbox_events: Optional[Callable[[str, List[str]], Awaitable[Any]]] = None,
        stream_mode: str = "delta",
    ) -> TurnResult:
        """Execute one turn and return aggregated result."""
        events: List[Dict[str, Any]] = []
        async for event in self.stream_turn(
            session_id=session_id,
            trigger=trigger,
            load_session=load_session,
            get_or_create_agent=get_or_create_agent,
            save_session=save_session,
            ack_mailbox_events=ack_mailbox_events,
            stream_mode=stream_mode,
        ):
            if isinstance(event, dict):
                events.append(event)
        return TurnResult(session_id=session_id, events=events)
