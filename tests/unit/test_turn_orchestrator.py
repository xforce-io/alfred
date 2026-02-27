"""Unit tests for TurnOrchestrator."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest

from src.everbot.core.runtime.turn_orchestrator import (
    TurnEvent,
    TurnEventType,
    TurnOrchestrator,
    TurnPolicy,
    _extract_failure_signature,
    _extract_tool_intent_signature,
    _truncate_preview,
)


# ---------------------------------------------------------------------------
# Helper: dummy agent that yields scripted progress events
# ---------------------------------------------------------------------------

class _ScriptedAgent:
    """Agent that yields a scripted sequence of Dolphin-style progress events."""

    def __init__(self, script: List[Dict[str, Any]]):
        self._script = script
        self.call_count = 0

    async def continue_chat(self, **kwargs):
        self.call_count += 1
        for item in self._script:
            yield item

    async def arun(self, **kwargs):
        self.call_count += 1
        for item in self._script:
            yield item


def _progress_event(*progresses: Dict[str, Any]) -> Dict[str, Any]:
    return {"_progress": list(progresses)}


def _llm_delta(text: str, pid: str = "p1") -> Dict[str, Any]:
    return {"id": pid, "stage": "llm", "delta": text, "status": "running"}


def _tool_call(name: str, args: str = "", pid: str = "tc1") -> Dict[str, Any]:
    return {"id": pid, "stage": "tool_call", "tool_name": name, "args": args, "status": "running"}


def _tool_output(name: str = "", output: str = "", pid: str = "to1") -> Dict[str, Any]:
    return {"id": pid, "stage": "tool_output", "tool_name": name, "output": output, "status": "completed"}


def _skill_call(name: str, args: str = "", pid: str = "sk1") -> Dict[str, Any]:
    """Simulate Dolphin skill invocation (status=processing → tool call equivalent)."""
    return {"id": pid, "stage": "skill", "skill_info": {"name": name, "args": args}, "status": "processing"}


def _skill_output(name: str = "", output: str = "", pid: str = "so1", status: str = "completed") -> Dict[str, Any]:
    """Simulate Dolphin skill result (status=completed/failed → tool output equivalent)."""
    return {"id": pid, "stage": "skill", "skill_info": {"name": name}, "output": output, "status": status}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_basic_llm_delta_flow():
    agent = _ScriptedAgent([
        _progress_event(_llm_delta("Hello ")),
        _progress_event(_llm_delta("world")),
    ])
    orch = TurnOrchestrator(TurnPolicy(max_attempts=1))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "hi", system_prompt="sys"):
        events.append(te)

    types = [e.type for e in events]
    assert TurnEventType.LLM_DELTA in types
    assert TurnEventType.TURN_COMPLETE in types
    complete = next(e for e in events if e.type == TurnEventType.TURN_COMPLETE)
    assert complete.answer == "Hello world"


@pytest.mark.asyncio
async def test_tool_call_budget_exceeded():
    """Exceeding max_tool_calls yields TURN_ERROR."""
    calls = [_progress_event(_tool_call(f"tool_{i}", pid=f"tc_{i}")) for i in range(5)]
    agent = _ScriptedAgent(calls)
    orch = TurnOrchestrator(TurnPolicy(max_attempts=1, max_tool_calls=3))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "go"):
        events.append(te)

    errors = [e for e in events if e.type == TurnEventType.TURN_ERROR]
    assert len(errors) == 1
    assert "TOOL_CALL_BUDGET_EXCEEDED" in errors[0].error


@pytest.mark.asyncio
async def test_repeated_tool_failures():
    """Repeated same failure signature triggers TURN_ERROR."""
    script = []
    for i in range(4):
        script.append(_progress_event(_tool_call("bash", pid=f"tc{i}")))
        script.append(_progress_event(_tool_output("bash", "Command exited with code 1", pid=f"to{i}")))
    agent = _ScriptedAgent(script)
    orch = TurnOrchestrator(TurnPolicy(max_attempts=1, max_same_failure_signature=2, max_tool_calls=20))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "go"):
        events.append(te)

    errors = [e for e in events if e.type == TurnEventType.TURN_ERROR]
    assert len(errors) == 1
    assert "REPEATED_TOOL_FAILURES" in errors[0].error


@pytest.mark.asyncio
async def test_repeated_tool_intent():
    """Repeated same tool intent triggers TURN_ERROR."""
    script = []
    for i in range(5):
        script.append(_progress_event(_tool_call("_bash", "cat > /tmp/foo.txt << EOF", pid=f"tc{i}")))
        script.append(_progress_event(_tool_output("_bash", "ok", pid=f"to{i}")))
    agent = _ScriptedAgent(script)
    orch = TurnOrchestrator(TurnPolicy(max_attempts=1, max_same_tool_intent=2, max_tool_calls=20))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "go"):
        events.append(te)

    errors = [e for e in events if e.type == TurnEventType.TURN_ERROR]
    assert len(errors) == 1
    assert "REPEATED_TOOL_INTENT" in errors[0].error


@pytest.mark.asyncio
async def test_retry_on_transient_error():
    """Transient error triggers retry with STATUS event."""
    call_count = {"n": 0}

    class _FailOnceAgent:
        async def continue_chat(self, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise ConnectionError("peer closed connection without sending")
            yield _progress_event(_llm_delta("ok"))

    orch = TurnOrchestrator(TurnPolicy(max_attempts=3))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(_FailOnceAgent(), "hi"):
        events.append(te)

    statuses = [e for e in events if e.type == TurnEventType.STATUS]
    assert len(statuses) == 1  # One retry notification
    assert call_count["n"] == 2
    assert any(e.type == TurnEventType.TURN_COMPLETE for e in events)


@pytest.mark.asyncio
async def test_non_retryable_error():
    """Non-retryable error yields TURN_ERROR immediately."""

    class _BadAgent:
        async def continue_chat(self, **kwargs):
            raise ValueError("bad input")
            yield  # make it an async generator

    orch = TurnOrchestrator(TurnPolicy(max_attempts=3))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(_BadAgent(), "hi"):
        events.append(te)

    assert len(events) == 1
    assert events[0].type == TurnEventType.TURN_ERROR
    assert "bad input" in events[0].error


@pytest.mark.asyncio
async def test_cancel_event():
    """External cancel_event stops the turn."""

    class _SlowAgent:
        async def continue_chat(self, **kwargs):
            for i in range(100):
                yield _progress_event(_llm_delta(f"tok{i}"))
                await asyncio.sleep(0)

    cancel = asyncio.Event()
    orch = TurnOrchestrator(TurnPolicy(max_attempts=1))
    events: list[TurnEvent] = []
    count = 0
    async for te in orch.run_turn(_SlowAgent(), "hi", cancel_event=cancel):
        events.append(te)
        count += 1
        if count == 3:
            cancel.set()

    assert any(e.type == TurnEventType.TURN_ERROR and "cancelled" in e.error for e in events)


@pytest.mark.asyncio
async def test_first_turn_uses_arun():
    agent = _ScriptedAgent([_progress_event(_llm_delta("hi"))])
    orch = TurnOrchestrator(TurnPolicy(max_attempts=1))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "hello", is_first_turn=True):
        events.append(te)

    assert agent.call_count == 1
    assert any(e.type == TurnEventType.TURN_COMPLETE for e in events)


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------

def test_extract_failure_signature():
    assert _extract_failure_signature("Command exited with code 1") == "exit_code:1"
    assert _extract_failure_signature("Command exited with code 0") is None
    assert _extract_failure_signature("ERR_CONNECTION something") == "ERR_CONNECTION"
    assert _extract_failure_signature("") is None
    assert _extract_failure_signature("SyntaxError: invalid syntax") is not None


def test_extract_tool_intent_signature():
    assert _extract_tool_intent_signature("_bash", "cat > /tmp/foo.txt << EOF") == "write_file:/tmp/foo.txt"
    assert _extract_tool_intent_signature("_bash", "mkdir -p /tmp/bar") == "create_dir:/tmp/bar"
    assert _extract_tool_intent_signature("_read_file", "/tmp/baz.py") == "read_file:/tmp/baz.py"
    assert _extract_tool_intent_signature("", "") is None
    # _grep tool with JSON args
    assert _extract_tool_intent_signature("_grep", '{"pattern": "吸引子|attractor", "path": "."}') == "search_grep:吸引子|attractor"
    assert _extract_tool_intent_signature("_grep", '{"pattern": "TODO"}') == "search_grep:TODO"
    assert _extract_tool_intent_signature("_grep", 'not json') is None
    # _bash with grep/rg commands
    assert _extract_tool_intent_signature("_bash", 'grep -r "attractor" .') == "search_bash:attractor"
    assert _extract_tool_intent_signature("_bash", "rg -i 'TODO' src/") == "search_bash:TODO"


def test_truncate_preview():
    short = "hello"
    text, trunc, total = _truncate_preview(short, 100)
    assert text == short
    assert not trunc

    long = "x" * 200
    text, trunc, total = _truncate_preview(long, 50)
    assert trunc
    assert total == 200
    assert len(text) < 200


# ---------------------------------------------------------------------------
# Skill-stage tests (Dolphin emits "skill" instead of "tool_call"/"tool_output")
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_skill_budget_exceeded():
    """Exceeding max_tool_calls via skill events yields TURN_ERROR."""
    calls = [_progress_event(_skill_call(f"_bash", pid=f"sk_{i}")) for i in range(5)]
    agent = _ScriptedAgent(calls)
    orch = TurnOrchestrator(TurnPolicy(max_attempts=1, max_tool_calls=3))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "go"):
        events.append(te)

    errors = [e for e in events if e.type == TurnEventType.TURN_ERROR]
    assert len(errors) == 1
    assert "TOOL_CALL_BUDGET_EXCEEDED" in errors[0].error


@pytest.mark.asyncio
async def test_skill_repeated_failures():
    """Repeated same failure signature via skill events triggers TURN_ERROR."""
    script = []
    for i in range(4):
        script.append(_progress_event(_skill_call("_bash", pid=f"sk{i}")))
        script.append(_progress_event(_skill_output("_bash", "Command exited with code 1", pid=f"so{i}")))
    agent = _ScriptedAgent(script)
    orch = TurnOrchestrator(TurnPolicy(max_attempts=1, max_same_failure_signature=2, max_tool_calls=20))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "go"):
        events.append(te)

    errors = [e for e in events if e.type == TurnEventType.TURN_ERROR]
    assert len(errors) == 1
    assert "REPEATED_TOOL_FAILURES" in errors[0].error


@pytest.mark.asyncio
async def test_skill_repeated_intent():
    """Repeated same tool intent via skill events triggers TURN_ERROR."""
    script = []
    for i in range(5):
        script.append(_progress_event(_skill_call("_bash", "cat > /tmp/foo.txt << EOF", pid=f"sk{i}")))
        script.append(_progress_event(_skill_output("_bash", "ok", pid=f"so{i}")))
    agent = _ScriptedAgent(script)
    orch = TurnOrchestrator(TurnPolicy(max_attempts=1, max_same_tool_intent=2, max_tool_calls=20))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "go"):
        events.append(te)

    errors = [e for e in events if e.type == TurnEventType.TURN_ERROR]
    assert len(errors) == 1
    assert "REPEATED_TOOL_INTENT" in errors[0].error


@pytest.mark.asyncio
async def test_skill_counts_in_turn_complete():
    """Skill events correctly increment tool_call_count and tool_execution_count."""
    script = [
        _progress_event(_skill_call("_bash", "echo hi", pid="sk1")),
        _progress_event(_skill_output("_bash", "hi", pid="so1")),
        _progress_event(_skill_call("_read_file", "/tmp/x", pid="sk2")),
        _progress_event(_skill_output("_read_file", "content", pid="so2")),
        _progress_event(_llm_delta("done")),
    ]
    agent = _ScriptedAgent(script)
    orch = TurnOrchestrator(TurnPolicy(max_attempts=1, max_tool_calls=10))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "go"):
        events.append(te)

    complete = next(e for e in events if e.type == TurnEventType.TURN_COMPLETE)
    assert complete.tool_call_count == 2
    assert complete.tool_execution_count == 2
    assert complete.tool_names_executed == ["_bash", "_read_file"]
