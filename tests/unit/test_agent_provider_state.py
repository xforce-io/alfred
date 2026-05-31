"""DolphinProvider 状态 helper 必须与 AgentState/PauseType 直接比较等价。"""
from dolphin.core.agent.agent_state import AgentState, PauseType
from src.everbot.core.agent.provider.dolphin.state import (
    is_paused, is_error, is_user_interrupt_paused,
)


class _FakeAgent:
    def __init__(self, state, pause_type=None):
        self.state = state
        self._pause_type = pause_type


def test_is_paused_true_only_when_paused():
    assert is_paused(_FakeAgent(AgentState.PAUSED)) is True
    assert is_paused(_FakeAgent(AgentState.ERROR)) is False


def test_is_error_true_only_when_error():
    assert is_error(_FakeAgent(AgentState.ERROR)) is True
    assert is_error(_FakeAgent(AgentState.PAUSED)) is False


def test_is_user_interrupt_paused_requires_both():
    assert is_user_interrupt_paused(
        _FakeAgent(AgentState.PAUSED, PauseType.USER_INTERRUPT)) is True
    assert is_user_interrupt_paused(
        _FakeAgent(AgentState.PAUSED, None)) is False
    assert is_user_interrupt_paused(
        _FakeAgent(AgentState.ERROR, PauseType.USER_INTERRUPT)) is False
