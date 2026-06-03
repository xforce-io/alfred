"""Neutral AgentProvider port. MUST NOT import dolphin.

This module defines only provider-neutral *capabilities* (the methods any
agent runtime must offer).  Dolphin-specific storage details (e.g. the history
variable key) intentionally do NOT live here — consumers that need them import
from the allowlisted compat layer instead.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator, Optional, Protocol, runtime_checkable


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

    def run_turn(
        self,
        agent: Any,
        message: Any,
        *,
        system_prompt: str = "",
        is_first_turn: bool = False,
        stream_mode: str = "delta",
    ) -> AsyncIterator[Any]:
        """Drive one turn, yielding raw provider events (``{"_progress": [...]}``
        for dolphin). turn_orchestrator applies provider-neutral policy on top."""
        ...

    # -- context access (收敛 agent.executor.context 裸访问) --------------

    def set_variable(self, agent: Any, key: str, value: Any) -> None: ...

    def get_variable(self, agent: Any, key: str) -> Any: ...

    def init_trajectory(self, agent: Any, path: str, overwrite: bool = False) -> None: ...

    def set_session_id(self, agent: Any, session_id: str) -> None: ...

    def finalize_trajectory_on_error(self, agent: Any) -> None: ...

    def has_skill(self, agent: Any, name: str) -> bool: ...

    def register_skillkit(self, agent: Any, skillkit: Any) -> None: ...

    def export_session(self, agent: Any) -> dict:
        """会话可移植导出 ``{history_messages, variables}``。

        同步(其中一处调用点在同步函数 ``_extract_context_trace`` 里;MilkieProvider
        用 sync httpx,与 set_variable/get_variable 一致)。
        """
        ...

    def needs_history_restore(self) -> bool:
        """是否需要 alfred 把存档历史灌回 agent。

        dolphin(进程内)True;milkie(serve 自持久化,重启从 checkpoint 恢复)False。
        ``restore_to_agent`` 据此 short-circuit。
        """
        ...

    async def interrupt(self, agent: Any) -> None:
        """中断 agent 当前运行(用户 stop / 介入)。"""
        ...

    async def resume(self, agent: Any, message: str) -> None:
        """向已暂停(用户中断)的 agent 注入消息并继续。"""
        ...
