"""TDD A2a: 把 agent.executor.context 的裸访问收进 AgentProvider。

主干 ~50 处直接 `agent.executor.context.set_variable/get_var_value/init_trajectory`
是 provider 抽象的泄漏点。先把这三类操作收进 provider 方法(DolphinProvider 委托
给 dolphin context,行为不变),后续逐个替换调用点。
"""
from src.everbot.core.agent.provider.dolphin.provider import DolphinProvider


class FakeCtx:
    def __init__(self):
        self.vars = {}
        self.traj_calls = []
        self.session_id_set = None

    def set_variable(self, k, v):
        self.vars[k] = v

    def get_var_value(self, k):
        return self.vars.get(k)

    def init_trajectory(self, path, overwrite=False):
        self.traj_calls.append((path, overwrite))

    def set_session_id(self, sid):
        self.session_id_set = sid


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


def test_set_session_id_sets_variable_and_calls_setter():
    a = FakeAgent()
    DolphinProvider().set_session_id(a, "sess-1")
    assert a.executor.context.vars["session_id"] == "sess-1"
    assert a.executor.context.session_id_set == "sess-1"


def test_set_session_id_tolerates_context_without_setter():
    class CtxNoSetter:
        def __init__(self):
            self.vars = {}

        def set_variable(self, k, v):
            self.vars[k] = v

    class Exec:
        def __init__(self):
            self.context = CtxNoSetter()

    class Agent:
        def __init__(self):
            self.executor = Exec()

    a = Agent()
    DolphinProvider().set_session_id(a, "s2")  # 不应抛错
    assert a.executor.context.vars["session_id"] == "s2"
