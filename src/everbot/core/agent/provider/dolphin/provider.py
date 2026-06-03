"""DolphinProvider — 当前唯一的 AgentProvider 实现。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from . import state as _state
from . import llm as _llm
from .compat import ensure_continue_chat_compatibility

logger = logging.getLogger(__name__)


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

    def set_session_id(self, agent: Any, session_id: str) -> None:
        ctx = agent.executor.context
        ctx.set_variable("session_id", session_id)
        if hasattr(ctx, "set_session_id"):
            ctx.set_session_id(session_id)

    def finalize_trajectory_on_error(self, agent: Any) -> None:
        """Persist the explore-stage trajectory on early abort(行为搬自
        turn_orchestrator._flush_trajectory,深访问 dolphin trajectory)。"""
        try:
            ctx = getattr(agent, "executor", None)
            ctx = getattr(ctx, "context", None) if ctx else None
            traj = getattr(ctx, "trajectory", None) if ctx else None
            cm = getattr(ctx, "context_manager", None) if ctx else None
            if traj and cm and traj.is_enabled():
                toolkit = getattr(ctx, "toolkit", None) or getattr(ctx, "skillkit", None)
                tools_schema = toolkit.getSkillsSchema() if toolkit else []
                status = ctx.get_var_value("_status") or {}
                stage_index = status.get("explore_time", 0)
                model = ctx.get_last_model_name() if hasattr(ctx, "get_last_model_name") else None
                traj.finalize_stage(
                    stage_name="explore",
                    stage_index=stage_index,
                    context_manager=cm,
                    tools=tools_schema,
                    user_id=ctx.user_id or "",
                    model=model,
                )
        except Exception as exc:
            logger.debug("finalize_trajectory_on_error failed (non-fatal): %s", exc)

    # -- skillkit registration (收敛 global_skills.installedSkillset 裸访问) --

    def has_skill(self, agent: Any, name: str) -> bool:
        gs = getattr(agent, "global_skills", None)
        installed = getattr(gs, "installedSkillset", None) if gs else None
        return bool(installed and installed.hasSkill(name))

    def register_skillkit(self, agent: Any, skillkit: Any) -> None:
        gs = getattr(agent, "global_skills", None)
        installed = getattr(gs, "installedSkillset", None) if gs else None
        if installed is not None:
            installed.addSkillkit(skillkit)

    def export_session(self, agent: Any) -> dict:
        return agent.snapshot.export_portable_session()

    def needs_history_restore(self) -> bool:
        return True  # dolphin 进程内,重启须把历史灌回 context
