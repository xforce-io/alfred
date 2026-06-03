"""会话导出收敛:把散落的 agent.snapshot.export_portable_session() 收进
AgentProvider.export_session(agent)。DolphinProvider 委托给 dolphin snapshot
(行为不变),后续逐个替换 5 处调用点。export_session 同步 —— 其中一个调用点
(_extract_context_trace)在同步函数里,且 MilkieProvider 用 sync httpx(同 set_variable)。
"""
from everbot.core.agent.provider.dolphin.provider import DolphinProvider


class FakeSnapshot:
    def __init__(self, portable):
        self._portable = portable
        self.calls = 0

    def export_portable_session(self):
        self.calls += 1
        return self._portable


class FakeAgent:
    def __init__(self, portable):
        self.snapshot = FakeSnapshot(portable)


def test_dolphin_export_session_delegates_to_snapshot():
    portable = {
        "history_messages": [{"role": "user", "content": "hi"}],
        "variables": {"model_name": "claude"},
    }
    a = FakeAgent(portable)
    out = DolphinProvider().export_session(a)
    assert out == portable
    assert a.snapshot.calls == 1
