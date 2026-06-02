"""TDD A2a: 把 agent.executor.context 的裸访问收进 AgentProvider。

主干 ~50 处直接 `agent.executor.context.set_variable/get_var_value/init_trajectory`
是 provider 抽象的泄漏点。先把这三类操作收进 provider 方法(DolphinProvider 委托
给 dolphin context,行为不变),后续逐个替换调用点。
"""
from everbot.core.agent.provider.dolphin.provider import DolphinProvider


class FakeCtx:
    def __init__(self):
        self.vars = {}
        self.traj_calls = []

    def set_variable(self, k, v):
        self.vars[k] = v

    def get_var_value(self, k):
        return self.vars.get(k)

    def init_trajectory(self, path, overwrite=False):
        self.traj_calls.append((path, overwrite))


class FakeExecutor:
    def __init__(self):
        self.context = FakeCtx()


class FakeAgent:
    def __init__(self):
        self.executor = FakeExecutor()


def test_set_variable_delegates_to_context():
    a = FakeAgent()
    DolphinProvider().set_variable(a, "model_name", "claude")
    assert a.executor.context.vars["model_name"] == "claude"


def test_get_variable_delegates_to_context():
    a = FakeAgent()
    a.executor.context.vars["k"] = "v"
    assert DolphinProvider().get_variable(a, "k") == "v"


def test_get_variable_missing_returns_none():
    a = FakeAgent()
    assert DolphinProvider().get_variable(a, "nope") is None


def test_init_trajectory_delegates_with_overwrite():
    a = FakeAgent()
    DolphinProvider().init_trajectory(a, "/tmp/t.json", overwrite=True)
    assert a.executor.context.traj_calls == [("/tmp/t.json", True)]


def test_init_trajectory_default_no_overwrite():
    a = FakeAgent()
    DolphinProvider().init_trajectory(a, "/tmp/t.json")
    assert a.executor.context.traj_calls == [("/tmp/t.json", False)]
