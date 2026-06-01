"""Dolphin agent 状态判断 helper（封装 AgentState/PauseType）。"""
from typing import Any

from dolphin.core.agent.agent_state import AgentState, PauseType


def is_paused(agent: Any) -> bool:
    return getattr(agent, "state", None) == AgentState.PAUSED


def is_error(agent: Any) -> bool:
    return getattr(agent, "state", None) == AgentState.ERROR


def is_user_interrupt_paused(agent: Any) -> bool:
    return (
        getattr(agent, "state", None) == AgentState.PAUSED
        and getattr(agent, "_pause_type", None) == PauseType.USER_INTERRUPT
    )
