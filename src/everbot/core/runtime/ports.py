"""Protocol interfaces for runtime module boundaries.

Defines the structural sub-typing contracts that HeartbeatRunner (and other
runtime components) require from the session layer, replacing ad-hoc
``hasattr`` capability probing with explicit, type-checkable interfaces.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Protocol, runtime_checkable


@runtime_checkable
class HeartbeatSessionPort(Protocol):
    """Structural interface that HeartbeatRunner expects from its session_manager."""

    # --- Session ID helpers ---------------------------------------------------

    @staticmethod
    def get_primary_session_id(agent_name: str) -> str: ...

    @staticmethod
    def get_heartbeat_session_id(agent_name: str) -> str: ...

    # --- Locking --------------------------------------------------------------

    async def acquire_session(self, session_id: str, timeout: float = ...) -> bool: ...

    def release_session(self, session_id: str) -> None: ...

    def file_lock(self, session_id: str, **kwargs: Any) -> Any: ...

    # --- Persistence ----------------------------------------------------------

    async def save_session(self, session_id: str, agent: Any, **kwargs: Any) -> None: ...

    async def load_session(self, session_id: str) -> Any: ...

    async def mark_session_archived(self, session_id: str, **kwargs: Any) -> bool: ...

    # --- Agent cache ----------------------------------------------------------

    def cache_agent(self, session_id: str, agent: Any, agent_name: str, model_name: str) -> None: ...

    def get_cached_agent(self, session_id: str) -> Optional[Any]: ...

    # --- Atomic / mailbox / history -------------------------------------------

    async def update_atomic(self, session_id: str, mutator: Callable, **kwargs: Any) -> Any: ...

    async def deposit_mailbox_event(self, session_id: str, event: Dict[str, Any], **kwargs: Any) -> bool: ...

    async def inject_history_message(self, session_id: str, message: dict, **kwargs: Any) -> bool: ...

    # --- Timeline -------------------------------------------------------------

    def append_timeline_event(self, session_id: str, event: Dict[str, Any]) -> None: ...

    def restore_timeline(self, session_id: str, events: list) -> None: ...

    # --- Restore / migrate ----------------------------------------------------

    async def restore_to_agent(self, agent: Any, session_data: Any) -> None: ...

    async def migrate_legacy_sessions_for_agent(self, agent_name: str) -> bool: ...

    # --- Metrics --------------------------------------------------------------

    def record_metric(self, name: str, delta: float = ...) -> None: ...

    # --- Persistence accessor -------------------------------------------------

    @property
    def persistence(self) -> Any: ...
