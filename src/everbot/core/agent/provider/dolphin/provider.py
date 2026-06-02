"""DolphinProvider — 当前唯一的 AgentProvider 实现。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator, Optional

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
        raise_on_error: bool = True,
    ) -> str:
        return await _llm.call_llm(
            context, prompt, temperature=temperature, fast=fast,
            raise_on_error=raise_on_error,
        )

    def ensure_chat_compatibility(self) -> bool:
        return ensure_continue_chat_compatibility()

    async def run_turn(
        self,
        agent: Any,
        message: Any,
        *,
        system_prompt: str = "",
        is_first_turn: bool = False,
        stream_mode: str = "delta",
    ) -> AsyncIterator[Any]:
        """Drive one dolphin turn, yielding raw ``_progress``-style events.

        有 message → ``continue_chat``(消息永不被丢弃);``is_first_turn`` 且无
        message 且 agent 暴露 ``arun`` → ``arun``(自治模式)。事件格式即 dolphin
        ``{"_progress": [...]}``,作为 provider 中立契约由 turn_orchestrator 消费。
        """
        if is_first_turn and hasattr(agent, "arun") and not message:
            stream = agent.arun(run_mode=True, stream_mode=stream_mode, mode="tool_call")
        else:
            stream = agent.continue_chat(
                message=message,
                stream_mode=stream_mode,
                mode="tool_call",
                system_prompt=system_prompt,
            )
        async for event in stream:
            yield event

    # -- context access (收敛 agent.executor.context 裸访问) --------------

    def set_variable(self, agent: Any, key: str, value: Any) -> None:
        agent.executor.context.set_variable(key, value)

    def get_variable(self, agent: Any, key: str) -> Any:
        return agent.executor.context.get_var_value(key)

    def init_trajectory(self, agent: Any, path: str, overwrite: bool = False) -> None:
        agent.executor.context.init_trajectory(path, overwrite=overwrite)
