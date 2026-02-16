"""Turn executor with session-level lock and strategy-based context build."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .context_strategy import ContextStrategy, RuntimeDeps, build_default_context_strategies


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

            async for event in agent.continue_chat(
                message=built.message,
                stream_mode=stream_mode,
                system_prompt=system_prompt,
            ):
                yield event

            await save_session(session_id, agent)
            if ack_mailbox_events is not None and built.mailbox_ack_ids:
                await ack_mailbox_events(session_id, built.mailbox_ack_ids)

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

