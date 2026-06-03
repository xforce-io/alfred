"""TDD A1: 把 agent.continue_chat/arun 收进 AgentProvider.run_turn。

run_turn 产出 provider 原始 ``_progress`` 风格事件流(中立契约),turn_orchestrator
在其上套 policy。DolphinProvider.run_turn 委托给 dolphin agent:
- 有 message → continue_chat(message 永不被丢弃)
- is_first_turn 且无 message 且 agent 有 arun → arun(自治模式)
"""
from src.everbot.core.agent.provider.dolphin.provider import DolphinProvider


class FakeAgent:
    def __init__(self):
        self.continue_chat_calls = []
        self.arun_calls = []

    async def continue_chat(self, **kwargs):
        self.continue_chat_calls.append(kwargs)
        yield {"_progress": [{"stage": "llm", "delta": "a"}]}
        yield {"_progress": [{"stage": "llm", "delta": "b"}]}

    async def arun(self, **kwargs):
        self.arun_calls.append(kwargs)
        yield {"_progress": [{"stage": "llm", "delta": "auto"}]}


async def test_run_turn_uses_continue_chat_with_message():
    agent = FakeAgent()
    events = [
        e async for e in DolphinProvider().run_turn(
            agent, "hi", system_prompt="sp", is_first_turn=False, stream_mode="delta"
        )
    ]
    assert events == [
        {"_progress": [{"stage": "llm", "delta": "a"}]},
        {"_progress": [{"stage": "llm", "delta": "b"}]},
    ]
    assert len(agent.continue_chat_calls) == 1
    call = agent.continue_chat_calls[0]
    assert call["message"] == "hi"
    assert call["system_prompt"] == "sp"
    assert call["mode"] == "tool_call"
    assert call["stream_mode"] == "delta"
    assert agent.arun_calls == []


async def test_run_turn_uses_arun_on_first_turn_without_message():
    agent = FakeAgent()
    events = [e async for e in DolphinProvider().run_turn(agent, "", is_first_turn=True)]
    assert events == [{"_progress": [{"stage": "llm", "delta": "auto"}]}]
    assert len(agent.arun_calls) == 1
    assert agent.arun_calls[0]["run_mode"] is True
    assert agent.arun_calls[0]["mode"] == "tool_call"
    assert agent.continue_chat_calls == []


async def test_run_turn_prefers_continue_chat_when_message_present_even_first_turn():
    """有 message 时即便 is_first_turn 也走 continue_chat —— 消息不能被静默丢弃。"""
    agent = FakeAgent()
    _ = [e async for e in DolphinProvider().run_turn(agent, "hello", is_first_turn=True)]
    assert len(agent.continue_chat_calls) == 1
    assert agent.arun_calls == []


async def test_run_turn_falls_back_to_continue_chat_when_no_arun():
    """agent 没有 arun 时,first_turn 无 message 也只能 continue_chat。"""
    class NoArun:
        def __init__(self):
            self.calls = []

        async def continue_chat(self, **kwargs):
            self.calls.append(kwargs)
            yield {"_progress": [{"stage": "llm", "delta": "x"}]}

    agent = NoArun()
    events = [e async for e in DolphinProvider().run_turn(agent, "", is_first_turn=True)]
    assert events == [{"_progress": [{"stage": "llm", "delta": "x"}]}]
    assert len(agent.calls) == 1
