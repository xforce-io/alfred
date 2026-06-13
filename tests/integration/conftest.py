"""Shared fixtures for integration tests.

#43 调查发现:workflow / heartbeat / session 等集成测试用**假 agent**
(ScriptedAgent / SimpleNamespace / MagicMock,无真实 sidecar)。自 #38 去 dolphin、
provider 收敛到 milkie 后,``provider_for(agent)`` 不再按类型派发,一律返回真实
``MilkieProvider`` 单例 —— 其 ``set_variable`` / ``get_variable`` / ``export_session``
是跨进程 HTTP 调用(``POST {agent.base_url}/context/...``)。假 agent 没有 ``base_url``
(且 ``context_id`` 常是 MagicMock,JSON 不可序列化),于是这些方法在集成测试里必崩。

这些测试本就不该打真实 serve(集成层无 sidecar),变量读写应走内存。autouse 夹具
把单例换成下面的内存 provider(只覆盖那三个 HTTP 变量方法,其余继承真实实现保持
不变),测试结束后还原。对当前通过的集成测试无影响 —— 它们均未引用 provider,
也从不命中这三个 HTTP 方法(命中即会因无 sidecar 而失败)。
"""
from __future__ import annotations

import pytest

from src.everbot.core.agent.provider.milkie.provider import MilkieProvider


class _InMemoryVarProvider(MilkieProvider):
    """真实 MilkieProvider,但变量读写改走内存、不打 HTTP(集成测试用)。

    只覆盖会跨进程 HTTP 的三个方法;run_turn/interrupt 等其余方法继承真实实现
    (集成测试用假 agent 的 ``continue_chat``,不经 provider.run_turn)。变量按
    agent 对象身份分桶,保证 workflow context_manager 的 KEY_HISTORY 往返一致。
    """

    def __init__(self) -> None:
        super().__init__(base_url=None)
        self._mem_vars: dict[int, dict] = {}

    def set_variable(self, agent, key, value) -> None:
        self._mem_vars.setdefault(id(agent), {})[key] = value

    def get_variable(self, agent, key):
        return self._mem_vars.get(id(agent), {}).get(key)

    def export_session(self, agent) -> dict:
        return {
            "history_messages": [],
            "variables": dict(self._mem_vars.get(id(agent), {})),
        }

    async def run_turn(self, agent, message, *, system_prompt="", **kwargs):
        """假 agent 暴露 dolphin 式 ``continue_chat``(产 ``{"_progress": [...]}``);
        桥接到它而非打真实 sidecar(等价于迁移前 dolphin provider 的行为)。无
        ``continue_chat`` 的(真 handle)回落真实实现。"""
        if hasattr(agent, "continue_chat"):
            async for ev in agent.continue_chat(message=message, system_prompt=system_prompt):
                yield ev
            return
        async for ev in super().run_turn(agent, message, system_prompt=system_prompt, **kwargs):
            yield ev


@pytest.fixture(autouse=True)
def _in_memory_provider():
    """把 milkie provider 单例换成内存版,避免假 agent 触发跨进程 HTTP 变量调用。"""
    from src.everbot.core.agent import provider as provider_mod

    saved = provider_mod._provider_by_name.get("milkie")
    provider_mod._provider_by_name["milkie"] = _InMemoryVarProvider()
    try:
        yield
    finally:
        if saved is None:
            provider_mod._provider_by_name.pop("milkie", None)
        else:
            provider_mod._provider_by_name["milkie"] = saved
