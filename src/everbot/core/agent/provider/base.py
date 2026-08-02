"""Neutral AgentProvider port.

This module defines only provider-neutral *capabilities* (the methods any
agent runtime must offer). Storage details (e.g. the history variable key)
intentionally do NOT live here — consumers that need them import from the
compat constants module instead.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class SkillObservationBatch:
    """Provider-neutral skill loads observed during the latest turn.

    ``complete`` distinguishes a genuine zero-skill turn from a provider that
    could not prove what happened.  Consumers must never treat an incomplete
    batch as an empty successful observation.
    """

    skill_names: tuple[str, ...]
    complete: bool
    reason: str = ""


@runtime_checkable
class AgentProvider(Protocol):
    """Provider-neutral port for agent-runtime capabilities.

The active implementation is :class:`MilkieProvider`. Implementations hide
    runtime-specific types behind this surface so alfred mainline stays
    provider-neutral.
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

    def capture_trace(self, agent: Any) -> Optional[Any]:
        """#47:为这一(失败)轮留证一份带外诊断 trace,返回产物路径或 None。

        通用能力:中立调用方(cron 失败分支)只调它、不碰任何 provider 私有标识。
        无此能力 / 无可留证的 provider 返回 None(默认)。MilkieProvider 经
        ``milkie trace`` 渲染落盘 run 的 HTML 报告。"""
        return None

    def is_user_interrupt_paused(self, agent: Any) -> bool: ...

    async def call_llm(
        self,
        context: Any,
        prompt: str,
        temperature: float = 0.3,
        fast: bool = False,
        raise_on_error: bool = True,
    ) -> str: ...

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

    def get_skill_observations(self, agent: Any) -> SkillObservationBatch:
        """Return successful skill loads from the agent's latest turn."""
        ...

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

    def import_session(self, agent: Any, portable_state: dict) -> None:
        """Apply portable session / compacted history into the live provider.

        Optional capability (#166). Implementations may accept alfred-style
        ``{history_messages, variables}`` and/or provider-native portable
        payloads. Missing method is tolerated by callers.
        """
        ...

    def needs_history_restore(self) -> bool:
        """Whether alfred must push archived history back into the live agent.

        milkie serve self-persists and restores from its own checkpoint, so the
        implementation returns False and restore callers short-circuit.
        """
        ...

    async def interrupt(self, agent: Any) -> None:
        """中断 agent 当前运行(用户 stop / 介入)。"""
        ...

    async def resume(self, agent: Any, message: str) -> None:
        """向已暂停(用户中断)的 agent 注入消息并继续。"""
        ...

    async def shutdown_sidecars(self) -> None:
        """Close any spawned sidecar processes. No-op when none are owned."""
        ...
