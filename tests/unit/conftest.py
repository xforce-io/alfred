"""Unit test configuration。

#38 dolphin 移除后:
- 不再 shim dolphin 常量(src 用 dolphin_compat 的纯常量)。
- 提供 `_DelegatingTestProvider`:镜像「旧 DolphinProvider 委托给 agent 自身方法」的行为,
  供大量用脚本化/fake agent(暴露 continue_chat/arun/executor.context/snapshot 等)测试
  编排器、会话、上下文逻辑的单测复用。autouse fixture 把 `provider_for` 按 agent 类型分派:
  非 MilkieAgentHandle(脚本 fake agent)→ 委托 fake;MilkieAgentHandle → 真 MilkieProvider。
  消费模块都是函数内 `from ..agent.provider import provider_for`(调用时实时取),故 patch
  一处包级 `provider_for` 即影响全部消费者。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

_project_root = str(Path(__file__).resolve().parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

logger = logging.getLogger(__name__)


class _DelegatingTestProvider:
    """镜像旧 DolphinProvider:操作委托给 agent 自身(executor.context / snapshot / continue_chat)。

    仅供单测复用既有 fake/脚本化 agent;不进入生产路径。
    """

    async def run_turn(self, agent, message, *, system_prompt="", is_first_turn=False, stream_mode="delta"):
        if is_first_turn and hasattr(agent, "arun") and not message:
            stream = agent.arun(run_mode=True, stream_mode=stream_mode, mode="tool_call")
        else:
            stream = agent.continue_chat(
                message=message, stream_mode=stream_mode, mode="tool_call", system_prompt=system_prompt,
            )
        async for event in stream:
            yield event

    def set_variable(self, agent, key, value):
        agent.executor.context.set_variable(key, value)

    def get_variable(self, agent, key):
        return agent.executor.context.get_var_value(key)

    def init_trajectory(self, agent, path, overwrite=False):
        agent.executor.context.init_trajectory(path, overwrite=overwrite)

    def set_session_id(self, agent, session_id):
        ctx = agent.executor.context
        ctx.set_variable("session_id", session_id)
        if hasattr(ctx, "set_session_id"):
            ctx.set_session_id(session_id)

    def finalize_trajectory_on_error(self, agent):
        try:
            ctx = getattr(getattr(agent, "executor", None), "context", None)
            traj = getattr(ctx, "trajectory", None) if ctx else None
            cm = getattr(ctx, "context_manager", None) if ctx else None
            if traj and cm and traj.is_enabled():
                toolkit = getattr(ctx, "toolkit", None) or getattr(ctx, "skillkit", None)
                tools_schema = toolkit.getSkillsSchema() if toolkit else []
                status = ctx.get_var_value("_status") or {}
                model = ctx.get_last_model_name() if hasattr(ctx, "get_last_model_name") else None
                traj.finalize_stage(
                    stage_name="explore", stage_index=status.get("explore_time", 0),
                    context_manager=cm, tools=tools_schema, user_id=ctx.user_id or "", model=model,
                )
        except Exception as exc:
            logger.debug("finalize_trajectory_on_error failed (non-fatal): %s", exc)

    def has_skill(self, agent, name):
        gs = getattr(agent, "global_skills", None)
        installed = getattr(gs, "installedSkillset", None) if gs else None
        return bool(installed and installed.hasSkill(name))

    def register_skillkit(self, agent, skillkit):
        gs = getattr(agent, "global_skills", None)
        installed = getattr(gs, "installedSkillset", None) if gs else None
        if installed is not None:
            installed.addSkillkit(skillkit)

    def export_session(self, agent):
        return agent.snapshot.export_portable_session()

    def needs_history_restore(self):
        return True

    def is_paused(self, agent):
        return bool(getattr(agent, "_paused", False))

    def is_error(self, agent):
        return bool(getattr(agent, "_error", False))

    def is_user_interrupt_paused(self, agent):
        return bool(getattr(agent, "_user_interrupt", False))

    async def call_llm(self, context, prompt, temperature=0.3, fast=False, raise_on_error=True):
        from src.everbot.core.agent.provider import oneshot_llm_provider
        return await oneshot_llm_provider().call_llm(
            context, prompt, temperature=temperature, fast=fast, raise_on_error=raise_on_error
        )

    def ensure_chat_compatibility(self):
        return False

    async def create_agent(self, agent_name, workspace_path, **kw):
        raise NotImplementedError("test delegating provider does not create agents")

    async def interrupt(self, agent):
        await agent.interrupt()

    async def resume(self, agent, message):
        await agent.resume_with_input(message)

    async def shutdown_sidecars(self):
        return None


@pytest.fixture(autouse=True)
def _delegate_fake_agents_to_test_provider(monkeypatch):
    """`provider_for` 按 agent 类型分派:脚本 fake agent → 委托 fake;真 handle → 真 milkie。"""
    import src.everbot.core.agent.provider as prov
    from src.everbot.core.agent.provider.milkie.provider import MilkieAgentHandle

    real_provider_for = prov.provider_for
    fake = _DelegatingTestProvider()

    def _smart(agent):
        if isinstance(agent, MilkieAgentHandle):
            return real_provider_for(agent)
        return fake

    monkeypatch.setattr(prov, "provider_for", _smart)
    # 顶层(模块级)import provider_for 的消费者:已绑定名不受包级 patch 影响,逐个 patch。
    for modpath in (
        "src.everbot.core.channel.core_service",
        "src.everbot.web.services.chat_service",
    ):
        try:
            import importlib
            mod = importlib.import_module(modpath)
            if hasattr(mod, "provider_for"):
                monkeypatch.setattr(mod, "provider_for", _smart)
        except Exception:
            pass
    yield
