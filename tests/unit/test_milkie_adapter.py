"""TDD: milkie 原生事件 → alfred TurnEvent 适配。

垂直切片只处理纯文本对话需要的事件:逐 token ``message_delta`` 和终态
``agent.run.completed``(completed / interrupted / error 三态)。其余 milkie
事件(tool.requested、agent.run.started、未知名…)在本切片里优雅忽略(返回
None),由后续阶段再扩展映射。
"""
from everbot.core.agent.provider.milkie.adapter import milkie_event_to_turn_event
from everbot.core.runtime.turn_policy import TurnEventType


def test_message_delta_maps_to_llm_delta():
    ev = milkie_event_to_turn_event("message_delta", {"text": "Hello"})
    assert ev is not None
    assert ev.type == TurnEventType.LLM_DELTA
    assert ev.content == "Hello"


def test_completed_maps_to_turn_complete_with_answer():
    ev = milkie_event_to_turn_event(
        "agent.run.completed", {"status": "completed", "output": "final answer"}
    )
    assert ev is not None
    assert ev.type == TurnEventType.TURN_COMPLETE
    assert ev.answer == "final answer"
    assert ev.status == "completed"


def test_interrupted_maps_to_turn_complete_with_status():
    ev = milkie_event_to_turn_event(
        "agent.run.completed", {"status": "interrupted", "output": "partial"}
    )
    assert ev is not None
    assert ev.type == TurnEventType.TURN_COMPLETE
    assert ev.status == "interrupted"
    assert ev.answer == "partial"


def test_error_terminal_maps_to_turn_error():
    ev = milkie_event_to_turn_event(
        "agent.run.completed", {"status": "error", "output": "", "error": "kaboom"}
    )
    assert ev is not None
    assert ev.type == TurnEventType.TURN_ERROR
    assert ev.error == "kaboom"
    assert ev.status == "error"


def test_error_frame_is_ignored_terminal_carries_the_error():
    """milkie 先发 ``error`` 帧再发 error 终态;前者忽略,避免重复。"""
    assert milkie_event_to_turn_event("error", {"message": "kaboom"}) is None


def test_unrelated_events_are_ignored():
    assert milkie_event_to_turn_event("agent.run.started", {"contextId": "c"}) is None
    assert milkie_event_to_turn_event("tool.requested", {"toolName": "x"}) is None
    assert milkie_event_to_turn_event("fsm.transition", {"from": "a", "to": "b"}) is None
    assert milkie_event_to_turn_event("totally.unknown", {}) is None


def test_message_delta_missing_text_is_empty_not_crash():
    ev = milkie_event_to_turn_event("message_delta", {})
    assert ev is not None
    assert ev.type == TurnEventType.LLM_DELTA
    assert ev.content == ""
