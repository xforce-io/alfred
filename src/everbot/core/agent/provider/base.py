"""Neutral AgentProvider port. MUST NOT import dolphin.

This module defines only provider-neutral *capabilities* (the methods any
agent runtime must offer).  Dolphin-specific storage details (e.g. the history
variable key) intentionally do NOT live here — consumers that need them import
from the allowlisted compat layer instead.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable


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
        raise_on_error: bool = True,
    ) -> str: ...

    def ensure_chat_compatibility(self) -> bool: ...
