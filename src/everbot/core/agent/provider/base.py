"""Neutral AgentProvider port. MUST NOT import dolphin."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

# Neutral constants (values identical to dolphin's current values).
KEY_HISTORY: str = "history"
KEY_HISTORY_COMPACT_ON_PERSIST: str = "_history_compact_on_persist"
KEY_HISTORY_COMPACT_RECENT_TURNS: str = "_history_compact_recent_turns"


@runtime_checkable
class AgentProvider(Protocol):
    """Provider-neutral port for agent-runtime capabilities.

    The active implementation (currently :class:`DolphinProvider`) hides all
    dolphin-specific types behind this surface so that alfred's mainline code
    never imports dolphin directly.
    """

    async def create_agent(
        self,
        agent_name: str,
        workspace_path: Path,
        model_name: Optional[str] = None,
        extra_variables: Optional[dict] = None,
        tools_override: Optional[list[str]] = None,
    ) -> Any: ...

    def is_paused(self, agent: Any) -> bool: ...

    def is_error(self, agent: Any) -> bool: ...

    def is_user_interrupt_paused(self, agent: Any) -> bool: ...

    async def call_llm(
        self,
        context: Any,
        prompt: str,
        temperature: float = 0.3,
        fast: bool = False,
    ) -> str: ...

    def ensure_chat_compatibility(self) -> bool: ...
