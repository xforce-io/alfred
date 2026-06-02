"""TDD C1: milkie 原生事件 → dolphin ``_progress`` item(统一中立契约)。

A1 把 ``_progress`` 定为 provider 中立契约,turn_orchestrator 在其上套 policy
产出 TurnEvent。故 MilkieProvider 也必须把 milkie 事件适配成 ``_progress`` item
(而非直接产 TurnEvent),才能复用 turn_orchestrator 的全部 policy。

映射(对齐 turn_orchestrator 657-862 的消费):
- message_delta {text}            → {stage:llm, delta, ...}
- tool.requested {toolName,input,toolCallId}
                                  → {stage:skill, status:running, skill_info, id}
- tool.responded {status,output/error,toolCallId}
                                  → {stage:skill, status:completed|failed, answer, id}
- agent.run.completed / error / started / 未知 → None(终态由流结束表示)
"""
import json

from everbot.core.agent.provider.milkie.adapter import milkie_event_to_progress


def test_message_delta_maps_to_llm_progress():
    p = milkie_event_to_progress("message_delta", {"text": "Hello"})
    assert p == {"stage": "llm", "delta": "Hello", "answer": "", "id": "llm"}


def test_message_delta_missing_text_is_empty():
    p = milkie_event_to_progress("message_delta", {})
    assert p["stage"] == "llm"
    assert p["delta"] == ""


def test_tool_requested_maps_to_skill_running():
    p = milkie_event_to_progress(
        "tool.requested", {"toolName": "search", "input": {"q": "x"}, "toolCallId": "tc1"}
    )
    assert p["stage"] == "skill"
    assert p["status"] == "running"
    assert p["id"] == "tc1"
    assert p["skill_info"]["name"] == "search"
    assert json.loads(p["skill_info"]["args"]) == {"q": "x"}


def test_tool_responded_ok_maps_to_skill_completed():
    p = milkie_event_to_progress(
        "tool.responded",
        {"toolName": "search", "toolCallId": "tc1", "status": "ok", "output": "result"},
    )
    assert p["stage"] == "skill"
    assert p["status"] == "completed"
    assert p["id"] == "tc1"
    assert p["answer"] == "result"
    assert p["skill_info"]["name"] == "search"


def test_tool_responded_error_maps_to_skill_failed_carrying_error():
    p = milkie_event_to_progress(
        "tool.responded",
        {"toolName": "search", "toolCallId": "tc1", "status": "error", "error": "boom"},
    )
    assert p["status"] == "failed"
    assert p["answer"] == "boom"


def test_tool_responded_non_string_output_is_json_encoded():
    p = milkie_event_to_progress(
        "tool.responded",
        {"toolName": "t", "toolCallId": "tc", "status": "ok", "output": {"k": 1}},
    )
    assert json.loads(p["answer"]) == {"k": 1}


def test_pid_pairs_running_and_completed_via_toolcallid():
    """同一 toolCallId 让 running/completed 配对(turn_orchestrator 靠 pid 去重/配对)。"""
    a = milkie_event_to_progress("tool.requested", {"toolName": "t", "input": {}, "toolCallId": "X"})
    b = milkie_event_to_progress("tool.responded", {"toolName": "t", "toolCallId": "X", "status": "ok", "output": "o"})
    assert a["id"] == b["id"] == "X"


def test_terminal_and_unrelated_events_return_none():
    assert milkie_event_to_progress("agent.run.completed", {"status": "completed", "output": "x"}) is None
    assert milkie_event_to_progress("error", {"message": "boom"}) is None
    assert milkie_event_to_progress("agent.run.started", {"contextId": "c"}) is None
    assert milkie_event_to_progress("fsm.transition", {"from": "a", "to": "b"}) is None
    assert milkie_event_to_progress("totally.unknown", {}) is None
