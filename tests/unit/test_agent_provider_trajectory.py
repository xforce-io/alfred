"""TDD A3: 把 turn_orchestrator._flush_trajectory 收进 provider。

错误/循环中断时保存 trajectory 的逻辑深访问 dolphin 内部(ctx.trajectory /
context_manager / toolkit / finalize_stage)。收成 provider.finalize_trajectory_on_error
(DolphinProvider 委托;MilkieProvider 将 no-op —— milkie 自带 event sourcing)。
"""
from src.everbot.core.agent.provider.dolphin.provider import DolphinProvider


class FakeTraj:
    def __init__(self, enabled=True):
        self._enabled = enabled
        self.finalize_calls = []

    def is_enabled(self):
        return self._enabled

    def finalize_stage(self, **kwargs):
        self.finalize_calls.append(kwargs)


class FakeToolkit:
    def getSkillsSchema(self):
        return [{"name": "t"}]


class FakeCtx:
    def __init__(self, traj):
        self.trajectory = traj
        self.context_manager = object()
        self.toolkit = FakeToolkit()
        self.user_id = "u1"
        self._vars = {"_status": {"explore_time": 3}}

    def get_var_value(self, k):
        return self._vars.get(k)

    def get_last_model_name(self):
        return "claude"


class FakeExec:
    def __init__(self, ctx):
        self.context = ctx


class FakeAgent:
    def __init__(self, ctx):
        self.executor = FakeExec(ctx)


def test_finalize_trajectory_calls_finalize_stage_when_enabled():
    traj = FakeTraj(enabled=True)
    a = FakeAgent(FakeCtx(traj))
    DolphinProvider().finalize_trajectory_on_error(a)
    assert len(traj.finalize_calls) == 1
    call = traj.finalize_calls[0]
    assert call["stage_name"] == "explore"
    assert call["stage_index"] == 3
    assert call["model"] == "claude"
    assert call["tools"] == [{"name": "t"}]


def test_finalize_trajectory_skips_when_disabled():
    traj = FakeTraj(enabled=False)
    a = FakeAgent(FakeCtx(traj))
    DolphinProvider().finalize_trajectory_on_error(a)
    assert traj.finalize_calls == []


def test_finalize_trajectory_tolerates_missing_executor():
    class Bare:
        executor = None

    DolphinProvider().finalize_trajectory_on_error(Bare())  # 不应抛错
