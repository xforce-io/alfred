"""DolphinProvider — 当前唯一的 AgentProvider 实现。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from . import state as _state
from . import llm as _llm
from .compat import ensure_continue_chat_compatibility


class DolphinProvider:
    """AgentProvider backed by the dolphin SDK."""

    async def create_agent(
        self,
        agent_name: str,
        workspace_path: Path,
        model_name: Optional[str] = None,
        extra_variables: Optional[dict] = None,
        tools_override: Optional[list[str]] = None,
    ) -> Any:
        from .factory import get_agent_factory
        return await get_agent_factory().create_agent(
            agent_name,
            workspace_path,
            model_name=model_name,
            extra_variables=extra_variables,
            tools_override=tools_override,
        )

    def is_paused(self, agent: Any) -> bool:
        return _state.is_paused(agent)

    def is_error(self, agent: Any) -> bool:
        return _state.is_error(agent)

    def is_user_interrupt_paused(self, agent: Any) -> bool:
        return _state.is_user_interrupt_paused(agent)

    async def call_llm(
        self,
        context: Any,
        prompt: str,
        temperature: float = 0.3,
        fast: bool = False,
    ) -> str:
        return await _llm.call_llm(context, prompt, temperature=temperature, fast=fast)

    def ensure_chat_compatibility(self) -> bool:
        return ensure_continue_chat_compatibility()
