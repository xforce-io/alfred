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


def test_dolphin_needs_history_restore_true():
    """dolphin 进程内 agent:重启后必须把存的 history 灌回 context。"""
    assert DolphinProvider().needs_history_restore() is True


async def test_dolphin_interrupt_and_resume_delegate_to_agent():
    """收敛 chat_service 的裸 agent.interrupt()/resume_with_input():DolphinProvider
    委托给 dolphin agent(行为不变),与已收敛的 is_user_interrupt_paused 保持一致。"""
    calls = []

    class _Agent:
        async def interrupt(self):
            calls.append("interrupt")

        async def resume_with_input(self, message):
            calls.append(("resume", message))

    a = _Agent()
    await DolphinProvider().interrupt(a)
    await DolphinProvider().resume(a, "hello")
    assert calls == ["interrupt", ("resume", "hello")]


async def test_restore_to_agent_short_circuits_when_provider_self_persists(tmp_path, monkeypatch):
    """provider 自持久化(needs_history_restore=False,如 milkie)→ restore 跳过灌回,
    完全不碰 agent(milkie handle 无 .executor/.snapshot,碰了会 AttributeError)。"""
    from everbot.core.session.persistence import SessionPersistence
    import everbot.core.agent.provider as provider_pkg

    class _SelfPersistedProvider:
        def needs_history_restore(self):
            return False

    monkeypatch.setattr(provider_pkg, "provider_for", lambda agent: _SelfPersistedProvider())
    p = SessionPersistence(tmp_path)
    # agent / session_data 都是裸 object:不 short-circuit 必 AttributeError。不抛即证明跳过。
    await p.restore_to_agent(object(), object())
