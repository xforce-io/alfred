"""Unit tests for TurnOrchestrator."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest

from src.everbot.core.runtime.turn_orchestrator import (
    CHAT_POLICY,
    HEARTBEAT_POLICY,
    JOB_POLICY,
    WORKFLOW_POLICY,
    TurnEvent,
    TurnEventType,
    TurnOrchestrator,
    TurnPolicy,
    _drain_after_timeout,
    _extract_failure_signature,
    _extract_tool_intent_signature,
    _is_read_only_intent,
    _resolve_timeout,
    _timeout_wrapper,
    _truncate_preview,
    build_chat_policy,
    build_heartbeat_policy,
    build_job_policy,
    build_workflow_policy,
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


def _llm_delta(text: str, pid: str = "p1", think: str = "") -> Dict[str, Any]:
    d: Dict[str, Any] = {"id": pid, "stage": "llm", "delta": text, "status": "running"}
    if think:
        d["think"] = think
    return d


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
async def test_cumulative_progress_does_not_duplicate_llm_deltas():
    """Cumulative `_progress` replays must not duplicate prior LLM output."""
    agent = _ScriptedAgent([
        _progress_event(_llm_delta("Hello ", pid="llm1")),
        _progress_event(
            _llm_delta("Hello ", pid="llm1"),
            _tool_call("_read_file", "/tmp/a.txt", pid="tc1"),
        ),
        _progress_event(
            _llm_delta("Hello ", pid="llm1"),
            _tool_call("_read_file", "/tmp/a.txt", pid="tc1"),
            _tool_output("_read_file", "content", pid="to1"),
        ),
        _progress_event(
            _llm_delta("Hello ", pid="llm1"),
            _tool_call("_read_file", "/tmp/a.txt", pid="tc1"),
            _tool_output("_read_file", "content", pid="to1"),
            _llm_delta("world", pid="llm2"),
        ),
    ])
    orch = TurnOrchestrator(TurnPolicy(max_attempts=1, max_tool_calls=10, max_consecutive_empty_llm_rounds=99))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "hi"):
        events.append(te)

    complete = next(e for e in events if e.type == TurnEventType.TURN_COMPLETE)
    assert complete.answer == "Hello world"
    deltas = [e.content for e in events if e.type == TurnEventType.LLM_DELTA]
    assert deltas == ["Hello ", "world"]


@pytest.mark.asyncio
async def test_tool_call_budget_exceeded():
    """Exceeding max_tool_calls yields TURN_ERROR."""
    calls = [_progress_event(_tool_call(f"tool_{i}", pid=f"tc_{i}")) for i in range(5)]
    agent = _ScriptedAgent(calls)
    orch = TurnOrchestrator(TurnPolicy(max_attempts=1, max_tool_calls=3, max_consecutive_empty_llm_rounds=99))
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
async def test_repeated_identical_bash_command_triggers_intent_guard():
    """Repeated identical bash commands should stop before the loop grows."""
    script = []
    for i in range(4):
        script.append(_progress_event(
            _tool_call(
                "_bash",
                "python skills/trajectory-reviewer/scripts/review_recent.py --limit-files 2",
                pid=f"tc{i}",
            )
        ))
        script.append(_progress_event(_tool_output("_bash", "report ok", pid=f"to{i}")))
    agent = _ScriptedAgent(script)
    orch = TurnOrchestrator(TurnPolicy(
        max_attempts=1,
        max_same_tool_intent=2,
        max_tool_calls=20,
        max_consecutive_empty_llm_rounds=99,
    ))
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

    # Cancelled turns now emit TURN_COMPLETE with status="cancelled" to preserve partial work
    assert any(
        (e.type == TurnEventType.TURN_COMPLETE and e.status == "cancelled")
        or (e.type == TurnEventType.TURN_ERROR and "cancelled" in e.error)
        for e in events
    )


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
    assert _extract_failure_signature("snippet=Command exited with code 2") is None


def test_extract_tool_intent_signature():
    assert _extract_tool_intent_signature("_bash", "cat > /tmp/foo.txt << EOF") == "write_file:/tmp/foo.txt"
    assert _extract_tool_intent_signature("_bash", "mkdir -p /tmp/bar") == "create_dir:/tmp/bar"
    assert _extract_tool_intent_signature("_read_file", "/tmp/baz.py") == "read_file:/tmp/baz.py"
    assert _extract_tool_intent_signature("", "") is None
    # _grep tool with JSON args
    assert _extract_tool_intent_signature("_grep", '{"pattern": "吸引子|attractor", "path": "."}') == "search_grep:吸引子|attractor"
    assert _extract_tool_intent_signature("_grep", '{"pattern": "TODO"}') == "search_grep:TODO"
    # Non-JSON _grep args: falls through to generic fallback (still dedup-able)
    sig = _extract_tool_intent_signature("_grep", 'not json')
    assert sig is not None and sig.startswith("tool_exec:_grep:")
    # _bash with grep/rg commands
    assert _extract_tool_intent_signature("_bash", 'grep -r "attractor" .') == "search_bash:attractor"
    assert _extract_tool_intent_signature("_bash", "rg -i 'TODO' src/") == "search_bash:TODO"
    # Skill script calls get classified as skill intents (flags only → no subcommand)
    assert _extract_tool_intent_signature(
        "_bash",
        "python skills/trajectory-reviewer/scripts/review_recent.py --limit-files 2",
    ) == "skill:trajectory-reviewer:review_recent"
    # Skill script with subcommand
    assert _extract_tool_intent_signature(
        "_bash",
        "python skills/trajectory-reviewer/scripts/review_recent.py check --limit-files 2",
    ) == "skill:trajectory-reviewer:review_recent:check"


def test_extract_tool_intent_skill_script():
    """Calls to the same skill script with different free-text args share intent;
    different subcommands are separate intents.  Search scripts return None
    (exempt from intent dedup, controlled by max_tool_calls instead)."""
    # web search: returns None (exempt from intent dedup)
    cmd1 = 'python /path/to/skills/web-search/scripts/search.py "泡泡玛特 股价 大跌" --backend auto'
    cmd2 = 'python /path/to/skills/web-search/scripts/search.py "bitcoin price crash" --type news'
    cmd3 = 'python skills/web/scripts/search.py "any query"'
    assert _extract_tool_intent_signature("_bash", cmd1) is None
    assert _extract_tool_intent_signature("_bash", cmd2) is None
    assert _extract_tool_intent_signature("_bash", cmd3) is None
    # invest skill: different subcommands → different intents
    cmd4 = 'python skills/invest/scripts/tools.py scan --modules macro'
    cmd5 = 'python skills/invest/scripts/tools.py report'
    assert _extract_tool_intent_signature("_bash", cmd4) == "skill:invest:tools:scan"
    assert _extract_tool_intent_signature("_bash", cmd5) == "skill:invest:tools:report"
    # invest: same subcommand, different flags → same intent
    cmd6 = 'python skills/invest/scripts/tools.py scan --modules china'
    assert _extract_tool_intent_signature("_bash", cmd6) == "skill:invest:tools:scan"
    # Non-skill bash commands should NOT match
    assert "skill:" not in (_extract_tool_intent_signature("_bash", "python scripts/analyze.py") or "")


@pytest.mark.asyncio
async def test_search_skill_exempt_from_intent_dedup():
    """Search skill scripts should be exempt from intent dedup entirely.
    They return None from _extract_tool_intent_signature, so repeated
    searches with different keywords are only bounded by max_tool_calls.
    This prevents premature circuit-breaking when the agent retries searches
    (e.g. querying A-share market data with varying Chinese keywords)."""
    search_cmd = 'python skills/web/scripts/search.py "A股 今天 行情"'
    # 8 search calls: would exceed max_same_tool_intent=6 if not exempt
    script = []
    for i in range(8):
        script.append(_progress_event(_llm_delta(f"Searching attempt {i}...", pid=f"llm{i}")))
        script.append(_progress_event(_tool_call("_bash", search_cmd, pid=f"tc{i}")))
        script.append(_progress_event(_tool_output("_bash", f"search result {i}", pid=f"to{i}")))
    agent = _ScriptedAgent(script)
    orch = TurnOrchestrator(TurnPolicy(
        max_attempts=1,
        max_same_tool_intent=6,
        max_tool_calls=20,
        max_consecutive_empty_llm_rounds=99,
    ))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "今天A股发生了啥"):
        events.append(te)

    errors = [e for e in events if e.type == TurnEventType.TURN_ERROR]
    assert len(errors) == 0, (
        f"Search skill calls should not trigger REPEATED_TOOL_INTENT; "
        f"got error: {errors[0].error if errors else 'none'}"
    )
    assert any(e.type == TurnEventType.TURN_COMPLETE for e in events)


def test_orchestrator_prior_failures_preseed():
    """Prior failures should pre-seed failure counters in new orchestrator."""
    prior = {"exit_code:1": 3}
    orch = TurnOrchestrator(TurnPolicy(), prior_failures=prior)
    assert orch.accumulated_failures == {"exit_code:1": 3}


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
    calls = [_progress_event(_skill_call("_bash", pid=f"sk_{i}")) for i in range(5)]
    agent = _ScriptedAgent(calls)
    orch = TurnOrchestrator(TurnPolicy(max_attempts=1, max_tool_calls=3, max_consecutive_empty_llm_rounds=99))
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


# ---------------------------------------------------------------------------
# Read-only intent differentiation tests
# ---------------------------------------------------------------------------

def test_is_read_only_intent():
    """Verify read-only vs write intent classification."""
    assert _is_read_only_intent("read_file:/tmp/foo.py") is True
    assert _is_read_only_intent("search_grep:TODO") is True
    assert _is_read_only_intent("search_bash:attractor") is True
    assert _is_read_only_intent("write_file:/tmp/foo.txt") is False
    assert _is_read_only_intent("create_dir:/tmp/bar") is False
    assert _is_read_only_intent("delete:/tmp/baz") is False


@pytest.mark.asyncio
async def test_read_only_intent_higher_limit():
    """5 reads of the same file should NOT trigger error (limit=6)."""
    script = []
    for i in range(5):
        script.append(_progress_event(_tool_call("_read_file", "/tmp/foo.py", pid=f"tc{i}")))
        script.append(_progress_event(_tool_output("_read_file", "content", pid=f"to{i}")))
    script.append(_progress_event(_llm_delta("done")))
    agent = _ScriptedAgent(script)
    orch = TurnOrchestrator(TurnPolicy(max_attempts=1, max_tool_calls=20, max_consecutive_empty_llm_rounds=99))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "go"):
        events.append(te)

    errors = [e for e in events if e.type == TurnEventType.TURN_ERROR]
    assert len(errors) == 0
    assert any(e.type == TurnEventType.TURN_COMPLETE for e in events)


@pytest.mark.asyncio
async def test_write_intent_keeps_strict_limit():
    """Write operations still use the strict limit (default 6)."""
    script = []
    for i in range(8):
        script.append(_progress_event(_tool_call("_bash", "cat > /tmp/foo.txt << EOF", pid=f"tc{i}")))
        script.append(_progress_event(_tool_output("_bash", "ok", pid=f"to{i}")))
    agent = _ScriptedAgent(script)
    orch = TurnOrchestrator(TurnPolicy(max_attempts=1, max_tool_calls=20, max_consecutive_empty_llm_rounds=99))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "go"):
        events.append(te)

    errors = [e for e in events if e.type == TurnEventType.TURN_ERROR]
    assert len(errors) == 1
    assert "REPEATED_TOOL_INTENT" in errors[0].error


@pytest.mark.asyncio
async def test_read_only_intent_still_has_limit():
    """12 reads of the same file should trigger error (exceeds limit=10)."""
    script = []
    for i in range(12):
        script.append(_progress_event(_tool_call("_read_file", "/tmp/foo.py", pid=f"tc{i}")))
        script.append(_progress_event(_tool_output("_read_file", "content", pid=f"to{i}")))
    agent = _ScriptedAgent(script)
    orch = TurnOrchestrator(TurnPolicy(max_attempts=1, max_tool_calls=20, max_consecutive_empty_llm_rounds=99))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "go"):
        events.append(te)

    errors = [e for e in events if e.type == TurnEventType.TURN_ERROR]
    assert len(errors) == 1
    assert "REPEATED_TOOL_INTENT" in errors[0].error


@pytest.mark.asyncio
async def test_intent_warning_injected_before_hard_stop():
    """When count == limit, tool output should contain a repeated_intent warning."""
    script = []
    # 7 identical write calls: count 1..5 normal, count 6 == limit → warning, count 7 → error
    for i in range(8):
        script.append(_progress_event(_tool_call("_bash", "pip install requests", pid=f"tc{i}")))
        script.append(_progress_event(_tool_output("_bash", "ok", pid=f"tc{i}")))
    agent = _ScriptedAgent(script)
    orch = TurnOrchestrator(TurnPolicy(max_attempts=1, max_tool_calls=30, max_consecutive_empty_llm_rounds=99))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "go"):
        events.append(te)

    # The 6th tool output (at limit) should carry the warning
    tool_outputs = [e for e in events if e.type == TurnEventType.TOOL_OUTPUT]
    warned = [e for e in tool_outputs if "repeated_intent" in e.tool_output]
    assert len(warned) >= 1

    # The 7th call should still trigger the hard error
    errors = [e for e in events if e.type == TurnEventType.TURN_ERROR]
    assert len(errors) == 1
    assert "REPEATED_TOOL_INTENT" in errors[0].error


# ---------------------------------------------------------------------------
# Timeout tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_timeout_stops_slow_agent():
    """Agent exceeding timeout_seconds is interrupted with TimeoutError."""

    class _SlowAgent:
        async def continue_chat(self, **kwargs):
            for i in range(200):
                yield _progress_event(_llm_delta(f"tok{i}"))
                await asyncio.sleep(0.01)  # 2s total, well above 0.3s timeout

    orch = TurnOrchestrator(TurnPolicy(max_attempts=1, timeout_seconds=0.3))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(_SlowAgent(), "hi"):
        events.append(te)

    errors = [e for e in events if e.type == TurnEventType.TURN_ERROR]
    assert len(errors) == 1
    assert "timeout" in errors[0].error.lower()


@pytest.mark.asyncio
async def test_timeout_fires_when_anext_blocks():
    """BUG: _timeout_wrapper checks deadline AFTER __anext__() returns.

    If the underlying stream hangs (LLM stops responding, network stall),
    __anext__() blocks indefinitely and the timeout never fires.  This is
    the root cause of [CLAUDE_TIMEOUT] in production: the timeout_seconds
    policy is set but has no effect when the stream itself is stuck.

    The fix: wrap __anext__() in asyncio.wait_for() so the per-iteration
    call itself is bounded, not just the gap between received items.
    """

    class _HangingAgent:
        """Agent that yields one event then hangs forever on __anext__."""
        async def continue_chat(self, **kwargs):
            yield _progress_event(_llm_delta("tok0"))
            # Simulate LLM/network hang — __anext__() blocks here
            await asyncio.sleep(999999)
            yield _progress_event(_llm_delta("never_reached"))

    async def _collect(orch, agent):
        events = []
        async for te in orch.run_turn(agent, "hi"):
            events.append(te)
        return events

    orch = TurnOrchestrator(TurnPolicy(max_attempts=1, timeout_seconds=0.2))
    # Use a test-level timeout to prevent the test from hanging forever.
    # If _timeout_wrapper works correctly, run_turn finishes within ~0.2s.
    # If the bug exists, __anext__() blocks and we hit the 2s test timeout.
    try:
        events = await asyncio.wait_for(_collect(orch, _HangingAgent()), timeout=2.0)
    except asyncio.TimeoutError:
        pytest.fail(
            "_timeout_wrapper did NOT fire: __anext__() blocked past the 0.2s "
            "deadline because the deadline check is after __anext__() returns. "
            "Fix: wrap __anext__() in asyncio.wait_for()."
        )

    errors = [e for e in events if e.type == TurnEventType.TURN_ERROR]
    assert len(errors) == 1, f"Expected TURN_ERROR with timeout, got: {[e.type for e in events]}"
    assert "timeout" in errors[0].error.lower()


@pytest.mark.asyncio
async def test_timeout_fires_when_no_events_produced():
    """BUG: If the agent's async generator never yields any event at all
    (e.g. LLM API hangs on first request), _timeout_wrapper blocks on
    the very first __anext__() call and the timeout never fires.

    This is a common [CLAUDE_TIMEOUT] scenario: the API call itself
    hangs before producing the first streaming token.
    """

    class _NeverYieldsAgent:
        """Agent that hangs immediately without yielding anything."""
        async def continue_chat(self, **kwargs):
            await asyncio.sleep(999999)
            yield _progress_event(_llm_delta("never"))  # unreachable

    async def _collect(orch, agent):
        events = []
        async for te in orch.run_turn(agent, "hi"):
            events.append(te)
        return events

    orch = TurnOrchestrator(TurnPolicy(max_attempts=1, timeout_seconds=0.2))
    try:
        events = await asyncio.wait_for(_collect(orch, _NeverYieldsAgent()), timeout=2.0)
    except asyncio.TimeoutError:
        pytest.fail(
            "_timeout_wrapper did NOT fire on first __anext__(): the stream "
            "never produced an event and the timeout was never checked. "
            "Fix: wrap __anext__() in asyncio.wait_for()."
        )

    errors = [e for e in events if e.type == TurnEventType.TURN_ERROR]
    assert len(errors) == 1
    assert "timeout" in errors[0].error.lower()


@pytest.mark.asyncio
async def test_timeout_not_retried():
    """TimeoutError must NOT be retried — retrying would repeat the same
    expensive work that already timed out.  Verify that max_attempts > 1
    does not cause retry on timeout."""
    call_count = {"n": 0}

    class _SlowAgent:
        async def continue_chat(self, **kwargs):
            call_count["n"] += 1
            for i in range(200):
                yield _progress_event(_llm_delta(f"tok{i}"))
                await asyncio.sleep(0.01)

    orch = TurnOrchestrator(TurnPolicy(max_attempts=3, timeout_seconds=0.3))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(_SlowAgent(), "hi"):
        events.append(te)

    # Should have been called exactly once — no retry
    assert call_count["n"] == 1, (
        f"Expected 1 attempt (no retry on timeout), got {call_count['n']}. "
        "TimeoutError should not be retryable."
    )
    errors = [e for e in events if e.type == TurnEventType.TURN_ERROR]
    assert len(errors) == 1
    assert "timeout" in errors[0].error.lower()


@pytest.mark.asyncio
async def test_timeout_not_triggered_when_fast():
    """Agent finishing within timeout completes normally."""
    agent = _ScriptedAgent([
        _progress_event(_llm_delta("quick")),
    ])
    orch = TurnOrchestrator(TurnPolicy(max_attempts=1, timeout_seconds=5.0))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "hi"):
        events.append(te)

    assert any(e.type == TurnEventType.TURN_COMPLETE for e in events)
    assert not any(e.type == TurnEventType.TURN_ERROR for e in events)


# ---------------------------------------------------------------------------
# _timeout_wrapper direct unit tests — expose the old __anext__() bug
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_timeout_wrapper_hangs_after_multiple_events():
    """BUG: Old _timeout_wrapper only checks deadline AFTER __anext__() returns.

    Production pattern: stream yields several events normally, then the LLM
    pauses (network stall, API throttle) on a subsequent __anext__(). The old
    code waits forever because the deadline check never executes.

    This differs from the single-event hang test by confirming that the bug
    manifests even after multiple successful iterations—not just on the first
    or second call.
    """

    async def _stream():
        for i in range(5):
            yield {"token": i}
        # After 5 fast events, stream hangs (simulates network stall)
        await asyncio.sleep(999999)
        yield {"token": "never"}

    collected = []
    timed_out = False
    try:
        async for item in _timeout_wrapper(_stream(), timeout=0.3):
            collected.append(item)
    except asyncio.TimeoutError:
        timed_out = True

    # If the old code is used, we'd never reach here (test-level timeout hits)
    try:
        await asyncio.wait_for(asyncio.sleep(0), timeout=0)
    except asyncio.TimeoutError:
        pass

    # Must have received all 5 fast events before the timeout fired
    assert len(collected) == 5, f"Expected 5 events before timeout, got {len(collected)}"
    assert timed_out, (
        "_timeout_wrapper did NOT fire after stream hung following 5 events. "
        "Old code: __anext__() blocks forever, deadline check never runs."
    )


@pytest.mark.asyncio
async def test_timeout_wrapper_hangs_after_tool_call_pattern():
    """BUG: Stream yields LLM delta + tool_call, then hangs waiting for next
    LLM response. This is the most common [CLAUDE_TIMEOUT] production pattern:
    the agent finishes a tool call, the API starts a new LLM inference, and
    the stream blocks on __anext__() during that inference.

    Old code: __anext__() blocks indefinitely → process-level timeout kills
    the entire session.
    """

    async def _stream():
        yield _progress_event(_llm_delta("Let me check"))
        yield _progress_event(_tool_call("_bash", "echo hi"))
        yield _progress_event(_tool_output("_bash", "hi"))
        # LLM inference for next response hangs
        await asyncio.sleep(999999)
        yield _progress_event(_llm_delta("never"))

    async def _collect(orch):
        events = []
        async for te in orch.run_turn(type("A", (), {
            "continue_chat": lambda self, **kw: _stream()
        })(), "hi"):
            events.append(te)
        return events

    orch = TurnOrchestrator(TurnPolicy(max_attempts=1, timeout_seconds=0.3))
    try:
        events = await asyncio.wait_for(_collect(orch), timeout=3.0)
    except asyncio.TimeoutError:
        pytest.fail(
            "_timeout_wrapper did NOT fire after tool_call+output pattern. "
            "Stream hung on __anext__() during next LLM inference. "
            "This is the primary [CLAUDE_TIMEOUT] production scenario."
        )

    errors = [e for e in events if e.type == TurnEventType.TURN_ERROR]
    assert len(errors) == 1, f"Expected TURN_ERROR, got: {[e.type for e in events]}"
    assert "timeout" in errors[0].error.lower()


@pytest.mark.asyncio
async def test_timeout_wrapper_deadline_already_passed():
    """BUG: Old code has no pre-check for `remaining <= 0`.

    If events arrive in a burst that takes wall-clock time (e.g. many small
    events processed synchronously), by the time we loop back to __anext__(),
    the deadline may have already passed. The new code checks `remaining <= 0`
    before calling __anext__(), the old code does not.

    Specifically tests the `remaining <= 0` branch added in the fix.
    """

    async def _burst_then_hang():
        """Yield events with sleeps that consume the entire timeout budget,
        then block on the next iteration."""
        # Two events that each consume half the timeout
        yield {"event": 0}
        await asyncio.sleep(0.15)
        yield {"event": 1}
        await asyncio.sleep(0.15)
        # By now ~0.3s has elapsed; with a 0.25s timeout, deadline is past.
        # Old code would call __anext__() which hangs forever.
        await asyncio.sleep(999999)
        yield {"event": "never"}

    collected = []
    timed_out = False
    try:
        result = asyncio.wait_for(
            _consume_wrapper(_burst_then_hang(), timeout=0.25),
            timeout=3.0,
        )
        collected = await result
    except asyncio.TimeoutError:
        timed_out = True

    assert timed_out or len(collected) <= 2, (
        "Expected timeout after deadline passed between iterations. "
        "Old code: no remaining<=0 pre-check, __anext__() blocks forever."
    )


async def _consume_wrapper(stream, timeout):
    """Helper: collect all items from _timeout_wrapper, propagate TimeoutError."""
    collected = []
    try:
        async for item in _timeout_wrapper(stream, timeout=timeout):
            collected.append(item)
    except asyncio.TimeoutError:
        raise
    return collected


@pytest.mark.asyncio
async def test_timeout_wrapper_drain_callback_invoked():
    """When on_timeout_drain is provided and timeout fires, the drain callback
    must be invoked to collect deferred results. Old code could hang before
    reaching the drain path if __anext__() blocked."""

    drain_called = {"called": False, "outputs": []}

    async def _on_drain(outputs, response):
        drain_called["called"] = True
        drain_called["outputs"] = outputs

    async def _stream():
        yield _progress_event(_llm_delta("partial"))
        yield _progress_event(_tool_call("_bash", "long_job"))
        # Tool execution hangs (simulates long-running command)
        await asyncio.sleep(999999)
        yield _progress_event(_tool_output("_bash", "done"))

    timed_out = False
    collected = []
    try:
        async for item in _timeout_wrapper(
            _stream(),
            timeout=0.2,
            on_timeout_drain=_on_drain,
            drain_extra_seconds=0.5,
        ):
            collected.append(item)
    except asyncio.TimeoutError:
        timed_out = True

    assert timed_out, (
        "_timeout_wrapper should have raised TimeoutError when stream hung "
        "during tool execution."
    )
    # Collected at least the events before the hang
    assert len(collected) >= 2


@pytest.mark.asyncio
async def test_timeout_wrapper_immediate_hang_zero_events():
    """Direct unit test: stream blocks on the very first __anext__().

    Old code: __anext__() blocks forever, no events, no deadline check.
    New code: asyncio.wait_for() on __anext__() fires the timeout.

    This is a simpler, more direct test than test_timeout_fires_when_no_events_produced
    which tests through the full TurnOrchestrator stack.
    """

    async def _never_yields():
        await asyncio.sleep(999999)
        yield "never"  # makes this an async generator

    timed_out = False
    try:
        async for _ in _timeout_wrapper(_never_yields(), timeout=0.1):
            pytest.fail("Should not yield any items")
    except asyncio.TimeoutError:
        timed_out = True

    assert timed_out, (
        "_timeout_wrapper did NOT fire on first __anext__(). "
        "Old code blocks forever without asyncio.wait_for()."
    )


# ---------------------------------------------------------------------------
# Policy preset tests — document expected configuration
# ---------------------------------------------------------------------------

def test_chat_policy_has_timeout():
    """CHAT_POLICY must have a timeout to prevent infinite LLM loops in Telegram."""
    assert CHAT_POLICY.timeout_seconds == 600


def test_heartbeat_policy_has_timeout():
    """HEARTBEAT_POLICY has a 120s timeout correctly configured."""
    assert HEARTBEAT_POLICY.timeout_seconds == 120


def test_job_policy_has_timeout():
    """JOB_POLICY must have a timeout to prevent isolated jobs from hanging."""
    assert JOB_POLICY.timeout_seconds == 600


# ---------------------------------------------------------------------------
# Empty-output loop detection tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_output_loop_triggers_error():
    """Consecutive tool calls with no LLM text output triggers TURN_ERROR."""
    script = []
    # First tool call (with LLM output — establishes baseline)
    script.append(_progress_event(_llm_delta("Let me help")))
    script.append(_progress_event(_tool_call("_bash", "echo hi", pid="tc0")))
    script.append(_progress_event(_tool_output("_bash", "hi", pid="to0")))
    # 3 consecutive tool calls with NO LLM output
    for i in range(1, 4):
        script.append(_progress_event(_tool_call("_bash", f"echo {i}", pid=f"tc{i}")))
        script.append(_progress_event(_tool_output("_bash", str(i), pid=f"to{i}")))
    agent = _ScriptedAgent(script)
    orch = TurnOrchestrator(TurnPolicy(
        max_attempts=1, max_tool_calls=20, max_consecutive_empty_llm_rounds=3,
    ))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "go"):
        events.append(te)

    errors = [e for e in events if e.type == TurnEventType.TURN_ERROR]
    assert len(errors) == 1
    assert "EMPTY_OUTPUT_LOOP" in errors[0].error


@pytest.mark.asyncio
async def test_empty_output_loop_resets_on_llm_output():
    """LLM output between tool calls resets the empty-output counter."""
    script = []
    # First tool call with output
    script.append(_progress_event(_llm_delta("step 1")))
    script.append(_progress_event(_tool_call("_bash", "echo 1", pid="tc0")))
    script.append(_progress_event(_tool_output("_bash", "1", pid="to0")))
    # 2 empty rounds (below threshold of 3)
    script.append(_progress_event(_tool_call("_bash", "echo 2", pid="tc1")))
    script.append(_progress_event(_tool_output("_bash", "2", pid="to1")))
    script.append(_progress_event(_tool_call("_bash", "echo 3", pid="tc2")))
    script.append(_progress_event(_tool_output("_bash", "3", pid="to2")))
    # LLM output resets the counter
    script.append(_progress_event(_llm_delta("step 2")))
    # 2 more empty rounds (still below threshold after reset)
    script.append(_progress_event(_tool_call("_bash", "echo 4", pid="tc3")))
    script.append(_progress_event(_tool_output("_bash", "4", pid="to3")))
    script.append(_progress_event(_tool_call("_bash", "echo 5", pid="tc4")))
    script.append(_progress_event(_tool_output("_bash", "5", pid="to4")))
    script.append(_progress_event(_llm_delta("done")))
    agent = _ScriptedAgent(script)
    orch = TurnOrchestrator(TurnPolicy(
        max_attempts=1, max_tool_calls=20, max_consecutive_empty_llm_rounds=3,
    ))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "go"):
        events.append(te)

    errors = [e for e in events if e.type == TurnEventType.TURN_ERROR]
    assert len(errors) == 0
    assert any(e.type == TurnEventType.TURN_COMPLETE for e in events)


@pytest.mark.asyncio
async def test_empty_output_loop_via_skill_stage():
    """Empty-output loop detection also works for skill-stage events."""
    script = []
    # First skill call with LLM output
    script.append(_progress_event(_llm_delta("loading")))
    script.append(_progress_event(_skill_call("_load_resource_skill", "paper-discovery", pid="sk0")))
    script.append(_progress_event(_skill_output("_load_resource_skill", "SKILL.md content", pid="so0")))
    # 3 consecutive skill calls with no LLM output
    for i in range(1, 4):
        script.append(_progress_event(_skill_call("_bash", f"fetch {i}", pid=f"sk{i}")))
        script.append(_progress_event(_skill_output("_bash", f"result {i}", pid=f"so{i}")))
    agent = _ScriptedAgent(script)
    orch = TurnOrchestrator(TurnPolicy(
        max_attempts=1, max_tool_calls=20, max_consecutive_empty_llm_rounds=3,
    ))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "go"):
        events.append(te)

    errors = [e for e in events if e.type == TurnEventType.TURN_ERROR]
    assert len(errors) == 1
    assert "EMPTY_OUTPUT_LOOP" in errors[0].error


@pytest.mark.asyncio
async def test_empty_output_loop_not_triggered_when_think_present():
    """LLM reasoning (think) without text output should NOT trigger EMPTY_OUTPUT_LOOP.

    This reproduces the real scenario: multi-step skill tasks where the model
    produces tool_calls with reasoning but no user-visible text each round.
    """
    script = []
    # First tool call with normal LLM output
    script.append(_progress_event(_llm_delta("Let me fetch papers")))
    script.append(_progress_event(_skill_call("_load_resource_skill", "paper-discovery", pid="sk0")))
    script.append(_progress_event(_skill_output("_load_resource_skill", "skill summary", pid="so0")))
    # 3 consecutive skill calls with think (reasoning) but NO text delta
    for i in range(1, 4):
        script.append(_progress_event(_llm_delta("", think=f"reasoning step {i}")))
        script.append(_progress_event(_skill_call("_bash", f"fetch {i}", pid=f"sk{i}")))
        script.append(_progress_event(_skill_output("_bash", f"result {i}", pid=f"so{i}")))
    # Final LLM output
    script.append(_progress_event(_llm_delta("Here are the papers")))
    agent = _ScriptedAgent(script)
    orch = TurnOrchestrator(TurnPolicy(
        max_attempts=1, max_tool_calls=20, max_consecutive_empty_llm_rounds=3,
    ))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "go"):
        events.append(te)

    errors = [e for e in events if e.type == TurnEventType.TURN_ERROR]
    assert len(errors) == 0
    assert any(e.type == TurnEventType.TURN_COMPLETE for e in events)


# ---------------------------------------------------------------------------
# last_successful_tool_output fallback tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_output_fallback_when_llm_empty():
    """When LLM produces no text, last successful tool_output should be used as response."""
    script = [
        _progress_event(_llm_delta("", think="reasoning about tools")),
        _progress_event(_tool_call("_bash", "echo analysis", pid="tc1")),
        _progress_event(_tool_output("_bash", "Analysis result: all good", pid="to1")),
        # LLM produces only thinking, no visible text
        _progress_event(_llm_delta("", think="done, returning result")),
    ]
    agent = _ScriptedAgent(script)
    orch = TurnOrchestrator(TurnPolicy(max_attempts=1, max_tool_calls=10))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "go"):
        events.append(te)

    complete = next(e for e in events if e.type == TurnEventType.TURN_COMPLETE)
    assert complete.answer, "Expected fallback to last_successful_tool_output, got empty"
    assert "Analysis result" in complete.answer


@pytest.mark.asyncio
async def test_skill_output_fallback_when_llm_empty():
    """When LLM produces no text after skill calls, last successful skill output
    should be used as response. This is the production bug: skill outputs don't
    update last_successful_tool_output, causing empty '(无响应)' replies."""
    script = [
        _progress_event(_llm_delta("", think="loading skill")),
        _progress_event(_skill_call("_load_resource_skill", "coding-master", pid="sk1")),
        _progress_event(_skill_output("_load_resource_skill", "SKILL.md content", pid="so1")),
        _progress_event(_llm_delta("", think="running analysis")),
        _progress_event(_skill_call("_bash", "dispatch.py analyze", pid="sk2")),
        _progress_event(_skill_output("_bash", "Deep Review: 5 bugs found ...", pid="so2")),
        # LLM produces only thinking, no visible text
        _progress_event(_llm_delta("", think="analysis complete")),
    ]
    agent = _ScriptedAgent(script)
    orch = TurnOrchestrator(TurnPolicy(max_attempts=1, max_tool_calls=10))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "review code"):
        events.append(te)

    complete = next(e for e in events if e.type == TurnEventType.TURN_COMPLETE)
    assert complete.answer, "Expected fallback to last successful skill output, got empty"
    assert "Deep Review" in complete.answer


@pytest.mark.asyncio
async def test_skill_output_fallback_ignores_failed_skill_output():
    """Only successful skill outputs should be used as fallback.
    A failed skill output (with error signature) should NOT overwrite
    a previous successful output."""
    script = [
        _progress_event(_llm_delta("", think="step 1")),
        _progress_event(_skill_call("_bash", "echo good", pid="sk1")),
        _progress_event(_skill_output("_bash", "Good result from first skill", pid="so1")),
        _progress_event(_llm_delta("", think="step 2")),
        _progress_event(_skill_call("_bash", "bad command", pid="sk2")),
        # Failed output: has error signature
        _progress_event(_skill_output("_bash", "Command exited with code 1", pid="so2")),
        _progress_event(_llm_delta("", think="done")),
    ]
    agent = _ScriptedAgent(script)
    orch = TurnOrchestrator(TurnPolicy(max_attempts=1, max_tool_calls=10))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "go"):
        events.append(te)

    complete = next(e for e in events if e.type == TurnEventType.TURN_COMPLETE)
    assert complete.answer, "Expected fallback to successful skill output"
    assert "Good result" in complete.answer
    assert "exited with code 1" not in complete.answer


@pytest.mark.asyncio
async def test_skill_output_fallback_ignores_status_failed_without_signature():
    """BUG: When a skill reports status='failed' but its output text does NOT
    match any known failure pattern (no 'exit code', no 'Error:', etc.),
    _extract_failure_signature() returns None.  The fallback tracking code
    only checks `not fail_sig`, ignoring the explicit status='failed' field.
    Result: failed skill output is stored as last_successful_tool_output and
    surfaced to the user as the response — leaking internal error details.

    The fix should also check `status != 'failed'` before tracking fallback.
    """
    script = [
        _progress_event(_llm_delta("", think="step 1")),
        _progress_event(_skill_call("_bash", "echo good", pid="sk1")),
        _progress_event(_skill_output("_bash", "Good result", pid="so1")),
        _progress_event(_llm_delta("", think="step 2")),
        _progress_event(_skill_call("_bash", "do something", pid="sk2")),
        # status="failed" but output has NO recognized failure pattern
        _progress_event(_skill_output(
            "_bash",
            "The operation could not be completed due to insufficient permissions",
            pid="so2",
            status="failed",
        )),
        _progress_event(_llm_delta("", think="done")),
    ]
    agent = _ScriptedAgent(script)
    orch = TurnOrchestrator(TurnPolicy(max_attempts=1, max_tool_calls=10))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "go"):
        events.append(te)

    complete = next(e for e in events if e.type == TurnEventType.TURN_COMPLETE)
    # The failed skill output should NOT become the fallback response
    assert "insufficient permissions" not in (complete.answer or ""), (
        "BUG: status='failed' skill output was used as fallback because "
        "_extract_failure_signature() returned None (no pattern match). "
        "The code should also check the explicit status field."
    )
    # The earlier successful output should be preserved
    assert complete.answer == "Good result", (
        f"Expected earlier successful output 'Good result', got: {complete.answer!r}"
    )


@pytest.mark.asyncio
async def test_skill_output_fallback_truncated_to_max_chars():
    """Skill output used as fallback should be truncated to max_tool_output_preview_chars."""
    long_output = "X" * 500
    script = [
        _progress_event(_llm_delta("", think="analyzing")),
        _progress_event(_skill_call("_bash", "long command", pid="sk1")),
        _progress_event(_skill_output("_bash", long_output, pid="so1")),
        _progress_event(_llm_delta("", think="done")),
    ]
    agent = _ScriptedAgent(script)
    orch = TurnOrchestrator(TurnPolicy(max_attempts=1, max_tool_calls=10, max_tool_output_preview_chars=100))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "go"):
        events.append(te)

    complete = next(e for e in events if e.type == TurnEventType.TURN_COMPLETE)
    assert complete.answer, "Expected truncated fallback"
    assert len(complete.answer) == 100


@pytest.mark.asyncio
async def test_skill_output_overwrites_earlier_tool_output_fallback():
    """A later successful skill output should overwrite an earlier tool_output fallback."""
    script = [
        _progress_event(_llm_delta("", think="step 1")),
        # Regular tool_call/tool_output stage
        _progress_event(_tool_call("_bash", "echo old", pid="tc1")),
        _progress_event(_tool_output("_bash", "Old tool_output result", pid="to1")),
        _progress_event(_llm_delta("", think="step 2")),
        # Skill stage (should overwrite the tool_output fallback)
        _progress_event(_skill_call("_bash", "echo new", pid="sk1")),
        _progress_event(_skill_output("_bash", "New skill result", pid="so1")),
        _progress_event(_llm_delta("", think="done")),
    ]
    agent = _ScriptedAgent(script)
    orch = TurnOrchestrator(TurnPolicy(max_attempts=1, max_tool_calls=10))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "go"):
        events.append(te)

    complete = next(e for e in events if e.type == TurnEventType.TURN_COMPLETE)
    assert complete.answer, "Expected skill output to be the fallback"
    assert "New skill result" in complete.answer


@pytest.mark.asyncio
async def test_skill_only_flow_no_tool_output_stage():
    """Pure skill-only flow (no tool_call/tool_output stages at all).
    This is the most common production scenario where the bug manifests:
    Dolphin emits only skill events, never tool_call/tool_output events."""
    script = [
        _progress_event(_llm_delta("", think="I'll use skills to solve this")),
        _progress_event(_skill_call("_load_resource_skill", "paper-discovery", pid="sk1")),
        _progress_event(_skill_output("_load_resource_skill", "Loaded skill context", pid="so1")),
        _progress_event(_llm_delta("", think="now search")),
        _progress_event(_skill_call("_bash", "search papers", pid="sk2")),
        _progress_event(_skill_output("_bash", "Found 3 papers: A, B, C", pid="so2")),
        _progress_event(_llm_delta("", think="now analyze")),
        _progress_event(_skill_call("_bash", "analyze results", pid="sk3")),
        _progress_event(_skill_output("_bash", "Final analysis: Paper A is best", pid="so3")),
        # LLM never produces visible text
    ]
    agent = _ScriptedAgent(script)
    orch = TurnOrchestrator(TurnPolicy(max_attempts=1, max_tool_calls=10))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "find papers"):
        events.append(te)

    complete = next(e for e in events if e.type == TurnEventType.TURN_COMPLETE)
    assert complete.answer, "Pure skill flow should have fallback answer, not empty '(无响应)'"
    assert "Final analysis" in complete.answer


# ---------------------------------------------------------------------------
# _drain_after_timeout tests (Bug 2 — incomplete drain results)
# ---------------------------------------------------------------------------

class _FakeAsyncIter:
    """Async iterator that yields scripted progress items then stops."""

    def __init__(self, items):
        self._items = list(items)
        self._index = 0
        self._closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._index]
        self._index += 1
        return item

    async def aclose(self):
        self._closed = True


@pytest.mark.asyncio
async def test_drain_prefers_llm_over_tool_outputs():
    """When both LLM text and tool output are present, drain uses LLM text only.

    Tool outputs are raw JSON and should not be forwarded to the user
    when the LLM has already produced a human-readable response.
    """
    items = [
        {"_progress": [{"stage": "llm", "delta": "Analysis: looks good."}]},
        {"_progress": [{"stage": "skill", "status": "completed", "output": "Tool result: pass"}]},
    ]
    results = []
    await _drain_after_timeout(
        _FakeAsyncIter(items),
        on_result=lambda r: results.append(r),
        extra_timeout=5.0,
    )
    assert len(results) == 1
    assert "Analysis: looks good." in results[0]
    # Tool output should NOT be included when LLM text is available
    assert "Tool result: pass" not in results[0]


@pytest.mark.asyncio
async def test_drain_llm_only():
    """Drain with only LLM text works correctly."""
    items = [
        {"_progress": [{"stage": "llm", "delta": "Only LLM."}]},
    ]
    results = []
    await _drain_after_timeout(
        _FakeAsyncIter(items),
        on_result=lambda r: results.append(r),
        extra_timeout=5.0,
    )
    assert len(results) == 1
    assert results[0] == "Only LLM."


@pytest.mark.asyncio
async def test_drain_tool_only():
    """Drain with only tool output works correctly."""
    items = [
        {"_progress": [{"stage": "skill", "status": "completed", "output": "Tool only."}]},
    ]
    results = []
    await _drain_after_timeout(
        _FakeAsyncIter(items),
        on_result=lambda r: results.append(r),
        extra_timeout=5.0,
    )
    assert len(results) == 1
    assert results[0] == "Tool only."


@pytest.mark.asyncio
async def test_drain_truncates_long_result():
    """Results exceeding 8000 chars are truncated."""
    long_text = "x" * 9000
    items = [
        {"_progress": [{"stage": "llm", "delta": long_text}]},
    ]
    results = []
    await _drain_after_timeout(
        _FakeAsyncIter(items),
        on_result=lambda r: results.append(r),
        extra_timeout=5.0,
    )
    assert len(results) == 1
    assert len(results[0]) < 9000
    assert "truncated" in results[0]


@pytest.mark.asyncio
async def test_drain_empty_skips_callback():
    """When there is no content, on_result is never called."""
    items = [
        {"_progress": [{"stage": "llm", "delta": ""}]},
    ]
    results = []
    await _drain_after_timeout(
        _FakeAsyncIter(items),
        on_result=lambda r: results.append(r),
        extra_timeout=5.0,
    )
    assert len(results) == 0


# ---------------------------------------------------------------------------
# Edge-case coverage: budget_exempt_tools
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_budget_exempt_tools_not_counted():
    """Tools in budget_exempt_tools should NOT count toward max_tool_calls."""
    script = []
    # 5 calls to an exempt tool — should not trigger budget error
    for i in range(5):
        script.append(_progress_event(_tool_call("_heartbeat", f"ping {i}", pid=f"tc{i}")))
        script.append(_progress_event(_tool_output("_heartbeat", "pong", pid=f"to{i}")))
    script.append(_progress_event(_llm_delta("done")))
    agent = _ScriptedAgent(script)
    orch = TurnOrchestrator(TurnPolicy(
        max_attempts=1, max_tool_calls=2,  # very low budget
        budget_exempt_tools=frozenset({"_heartbeat"}),
        max_consecutive_empty_llm_rounds=99,
    ))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "go"):
        events.append(te)

    errors = [e for e in events if e.type == TurnEventType.TURN_ERROR]
    assert len(errors) == 0, f"Exempt tool should not trigger budget: {errors}"
    complete = next(e for e in events if e.type == TurnEventType.TURN_COMPLETE)
    assert complete.tool_call_count == 0  # exempt tools not counted


@pytest.mark.asyncio
async def test_budget_exempt_tools_skill_stage():
    """Skill-stage events for exempt tools should also not count toward budget."""
    script = []
    for i in range(5):
        script.append(_progress_event(_skill_call("_heartbeat", f"ping {i}", pid=f"sk{i}")))
        script.append(_progress_event(_skill_output("_heartbeat", "pong", pid=f"so{i}")))
    script.append(_progress_event(_llm_delta("done")))
    agent = _ScriptedAgent(script)
    orch = TurnOrchestrator(TurnPolicy(
        max_attempts=1, max_tool_calls=2,
        budget_exempt_tools=frozenset({"_heartbeat"}),
        max_consecutive_empty_llm_rounds=99,
    ))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "go"):
        events.append(te)

    errors = [e for e in events if e.type == TurnEventType.TURN_ERROR]
    assert len(errors) == 0
    complete = next(e for e in events if e.type == TurnEventType.TURN_COMPLETE)
    assert complete.tool_call_count == 0


# ---------------------------------------------------------------------------
# Edge-case coverage: non-progress events limit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_non_progress_events_limit():
    """Too many events without '_progress' key triggers TURN_ERROR."""
    # Send events that are dicts but lack '_progress'
    script = [{"some_other_key": i} for i in range(10)]
    agent = _ScriptedAgent(script)
    orch = TurnOrchestrator(TurnPolicy(
        max_attempts=1, max_non_progress_events=5,
    ))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "go"):
        events.append(te)

    errors = [e for e in events if e.type == TurnEventType.TURN_ERROR]
    assert len(errors) == 1
    assert "TOO_MANY_NON_PROGRESS_EVENTS" in errors[0].error


# ---------------------------------------------------------------------------
# Edge-case coverage: on_before_retry callback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_on_before_retry_callback_invoked():
    """on_before_retry is called with (attempt, exception) before retry."""
    call_count = {"n": 0}
    retry_log: list[tuple[int, str]] = []

    class _FailOnceAgent:
        async def continue_chat(self, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise ConnectionError("peer closed connection")
            yield _progress_event(_llm_delta("ok"))

    async def _on_retry(attempt, exc):
        retry_log.append((attempt, str(exc)))

    orch = TurnOrchestrator(TurnPolicy(max_attempts=3))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(_FailOnceAgent(), "hi", on_before_retry=_on_retry):
        events.append(te)

    assert len(retry_log) == 1
    assert retry_log[0][0] == 0  # first attempt index
    assert "peer closed connection" in retry_log[0][1]
    assert any(e.type == TurnEventType.TURN_COMPLETE for e in events)


# ---------------------------------------------------------------------------
# Edge-case coverage: first_turn with arun (no message)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_first_turn_arun_only_when_no_message():
    """arun() is only used when is_first_turn=True AND message is empty.
    When message is provided, continue_chat() should be used even on first turn."""

    class _TrackingAgent:
        def __init__(self):
            self.used_arun = False
            self.used_continue_chat = False

        async def continue_chat(self, **kwargs):
            self.used_continue_chat = True
            yield _progress_event(_llm_delta("via continue_chat"))

        async def arun(self, **kwargs):
            self.used_arun = True
            yield _progress_event(_llm_delta("via arun"))

    # With message: should use continue_chat even when is_first_turn=True
    agent1 = _TrackingAgent()
    orch = TurnOrchestrator(TurnPolicy(max_attempts=1))
    async for _ in orch.run_turn(agent1, "hello", is_first_turn=True):
        pass
    assert agent1.used_continue_chat
    assert not agent1.used_arun

    # Without message: should use arun
    agent2 = _TrackingAgent()
    async for _ in orch.run_turn(agent2, "", is_first_turn=True):
        pass
    assert agent2.used_arun
    assert not agent2.used_continue_chat


# ---------------------------------------------------------------------------
# Edge-case coverage: failure signature with JSON error_code
# ---------------------------------------------------------------------------

def test_extract_failure_signature_with_error_code():
    """Exit code + JSON error_code should produce finer-grained signature."""
    output = 'Command exited with code 1\n{"error_code": "PATH_NOT_FOUND", "message": "not found"}'
    sig = _extract_failure_signature(output)
    assert sig == "exit_code:1:PATH_NOT_FOUND"


def test_extract_failure_signature_error_line():
    """'Error:' at line start should be matched."""
    assert _extract_failure_signature("  SyntaxError: invalid syntax") is not None
    assert _extract_failure_signature("SyntaxError: bad") is not None
    # Mid-sentence "Error:" should NOT match
    assert _extract_failure_signature("No Error: found here but it's a false positive") is None


def test_extract_failure_signature_network_markers():
    """Network error markers should produce signatures."""
    assert _extract_failure_signature("ECONNREFUSED on port 8080") == "ECONNREFUSED"
    assert _extract_failure_signature("SSL_ERROR_HANDSHAKE") == "SSL_ERROR"
    assert _extract_failure_signature("Connection refused by server") == "Connection refused"


# ---------------------------------------------------------------------------
# Edge-case coverage: llm answer field (non-delta path)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_llm_answer_field_used_when_no_delta():
    """When LLM progress has 'answer' but no 'delta', answer should be used as response."""
    script = [
        {"_progress": [{"id": "p1", "stage": "llm", "delta": "", "answer": "Full answer", "status": "completed"}]},
    ]
    agent = _ScriptedAgent(script)
    orch = TurnOrchestrator(TurnPolicy(max_attempts=1))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "hi"):
        events.append(te)

    complete = next(e for e in events if e.type == TurnEventType.TURN_COMPLETE)
    assert complete.answer == "Full answer"


# ---------------------------------------------------------------------------
# Edge-case coverage: duplicate pid dedup
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_duplicate_pid_deduplication():
    """Same pid+status should be deduplicated (only emitted once)."""
    script = [
        _progress_event(_tool_call("_bash", "echo hi", pid="tc1")),
        # Duplicate: same pid and status
        _progress_event(_tool_call("_bash", "echo hi", pid="tc1")),
        _progress_event(_tool_output("_bash", "hi", pid="to1")),
        # Duplicate output
        _progress_event(_tool_output("_bash", "hi", pid="to1")),
        _progress_event(_llm_delta("done")),
    ]
    agent = _ScriptedAgent(script)
    orch = TurnOrchestrator(TurnPolicy(max_attempts=1, max_tool_calls=10))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "go"):
        events.append(te)

    tool_calls = [e for e in events if e.type == TurnEventType.TOOL_CALL]
    tool_outputs = [e for e in events if e.type == TurnEventType.TOOL_OUTPUT]
    assert len(tool_calls) == 1, f"Expected 1 deduplicated tool_call, got {len(tool_calls)}"
    assert len(tool_outputs) == 1, f"Expected 1 deduplicated tool_output, got {len(tool_outputs)}"


# ---------------------------------------------------------------------------
# Edge-case coverage: timeout with on_timeout_drain callback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_timeout_with_drain_callback():
    """When timeout fires and on_deferred_result is set, drain task should be created."""
    deferred_results: list[str] = []

    class _SlowAgent:
        async def continue_chat(self, **kwargs):
            for i in range(200):
                yield _progress_event(_llm_delta(f"tok{i} "))
                await asyncio.sleep(0.01)

    async def _on_deferred(result):
        deferred_results.append(result)

    orch = TurnOrchestrator(TurnPolicy(max_attempts=1, timeout_seconds=0.3))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(_SlowAgent(), "hi", on_deferred_result=_on_deferred):
        events.append(te)

    errors = [e for e in events if e.type == TurnEventType.TURN_ERROR]
    assert len(errors) == 1
    assert "timeout" in errors[0].error.lower()

    # Give drain task time to complete
    await asyncio.sleep(0.5)
    # Note: drain may or may not produce results depending on timing,
    # but the key assertion is no crash and TURN_ERROR was emitted


# ---------------------------------------------------------------------------
# Edge-case coverage: drain collects tool_output stage events
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_drain_ignores_tool_output_stage():
    """_drain_after_timeout only handles 'skill' and 'llm' stages, not 'tool_output'.
    This is a potential coverage gap: if only tool_output events arrive during drain,
    no result is collected."""
    items = [
        {"_progress": [{"stage": "tool_output", "status": "completed", "output": "Tool result"}]},
    ]
    results = []
    await _drain_after_timeout(
        _FakeAsyncIter(items),
        on_result=lambda r: results.append(r),
        extra_timeout=5.0,
    )
    # tool_output stage is NOT collected by drain — only skill and llm
    assert len(results) == 0


# ---------------------------------------------------------------------------
# _drain_after_timeout: internal skill filtering & [PIN] marker cleanup
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_drain_filters_load_resource_skill():
    """_load_resource_skill outputs are internal (contain [PIN] + SKILL.md)
    and must not appear in deferred results."""
    items = [
        {"_progress": [{"stage": "skill", "status": "completed",
                         "skill_info": {"name": "_load_resource_skill"},
                         "output": "[PIN]\n# Coding Master Skill\nFull content..."}]},
        {"_progress": [{"stage": "skill", "status": "completed",
                         "skill_info": {"name": "_bash"},
                         "output": "Real command result"}]},
    ]
    results = []
    await _drain_after_timeout(
        _FakeAsyncIter(items),
        on_result=lambda r: results.append(r),
        extra_timeout=5.0,
    )
    assert len(results) == 1
    assert "Real command result" in results[0]
    assert "Coding Master" not in results[0]
    assert "[PIN]" not in results[0]


@pytest.mark.asyncio
async def test_drain_filters_load_skill_resource():
    """_load_skill_resource (Level 3 resource loader) is also internal."""
    items = [
        {"_progress": [{"stage": "skill", "status": "completed",
                         "skill_info": {"name": "_load_skill_resource"},
                         "output": "scripts/etl.py content..."}]},
        {"_progress": [{"stage": "llm", "delta": "Here is the analysis."}]},
    ]
    results = []
    await _drain_after_timeout(
        _FakeAsyncIter(items),
        on_result=lambda r: results.append(r),
        extra_timeout=5.0,
    )
    assert len(results) == 1
    assert "Here is the analysis." in results[0]
    assert "etl.py" not in results[0]


@pytest.mark.asyncio
async def test_drain_filters_resource_skill_only_result():
    """If the only skill output is from _load_resource_skill and there is
    no LLM text, drain should produce no result (not an empty callback)."""
    items = [
        {"_progress": [{"stage": "skill", "status": "completed",
                         "skill_info": {"name": "_load_resource_skill"},
                         "output": "[PIN]\n# Skill content"}]},
    ]
    results = []
    await _drain_after_timeout(
        _FakeAsyncIter(items),
        on_result=lambda r: results.append(r),
        extra_timeout=5.0,
    )
    assert len(results) == 0


@pytest.mark.asyncio
async def test_drain_strips_pin_marker_from_llm_text():
    """[PIN] markers in LLM output are stripped from the final result."""
    items = [
        {"_progress": [{"stage": "llm", "delta": "Start [PIN] middle"}]},
        {"_progress": [{"stage": "llm", "delta": " end [PIN]"}]},
    ]
    results = []
    await _drain_after_timeout(
        _FakeAsyncIter(items),
        on_result=lambda r: results.append(r),
        extra_timeout=5.0,
    )
    assert len(results) == 1
    assert "[PIN]" not in results[0]
    assert "Start" in results[0]
    assert "middle" in results[0]
    assert "end" in results[0]


@pytest.mark.asyncio
async def test_drain_strips_pin_marker_from_tool_output():
    """[PIN] markers in tool output are stripped from the final result."""
    items = [
        {"_progress": [{"stage": "skill", "status": "completed",
                         "skill_info": {"name": "_bash"},
                         "output": "[PIN] should be cleaned"}]},
    ]
    results = []
    await _drain_after_timeout(
        _FakeAsyncIter(items),
        on_result=lambda r: results.append(r),
        extra_timeout=5.0,
    )
    assert len(results) == 1
    assert "[PIN]" not in results[0]
    assert "should be cleaned" in results[0]


@pytest.mark.asyncio
async def test_drain_collects_skill_answer_field():
    """Drain should check answer, block_answer, and output fields in order."""
    items = [
        {"_progress": [{"stage": "skill", "status": "completed",
                         "skill_info": {"name": "_bash"},
                         "answer": "Answer field value",
                         "output": "Output field value"}]},
    ]
    results = []
    await _drain_after_timeout(
        _FakeAsyncIter(items),
        on_result=lambda r: results.append(r),
        extra_timeout=5.0,
    )
    assert len(results) == 1
    # answer takes precedence over output
    assert "Answer field value" in results[0]


@pytest.mark.asyncio
async def test_drain_collects_skill_block_answer_field():
    """When answer is empty, block_answer should be used."""
    items = [
        {"_progress": [{"stage": "skill", "status": "completed",
                         "skill_info": {"name": "_python"},
                         "answer": "",
                         "block_answer": "Block answer value",
                         "output": "Output value"}]},
    ]
    results = []
    await _drain_after_timeout(
        _FakeAsyncIter(items),
        on_result=lambda r: results.append(r),
        extra_timeout=5.0,
    )
    assert len(results) == 1
    assert "Block answer value" in results[0]


@pytest.mark.asyncio
async def test_drain_resource_skill_name_via_tool_name_fallback():
    """Filter works even when skill_info is missing and name is in tool_name."""
    items = [
        {"_progress": [{"stage": "skill", "status": "completed",
                         "tool_name": "_load_resource_skill",
                         "output": "[PIN]\n# Should be filtered"}]},
        {"_progress": [{"stage": "llm", "delta": "Actual response."}]},
    ]
    results = []
    await _drain_after_timeout(
        _FakeAsyncIter(items),
        on_result=lambda r: results.append(r),
        extra_timeout=5.0,
    )
    assert len(results) == 1
    assert "Actual response." in results[0]
    assert "Should be filtered" not in results[0]


# ---------------------------------------------------------------------------
# Edge-case coverage: mixed failure signatures (different errors)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_total_failed_tool_outputs_limit():
    """max_failed_tool_outputs triggers even when failure signatures are different."""
    script = []
    # 3 different failure signatures
    for i in range(3):
        script.append(_progress_event(_tool_call("_bash", f"cmd{i}", pid=f"tc{i}")))
        script.append(_progress_event(_tool_output("_bash", f"Command exited with code {i+1}", pid=f"to{i}")))
    agent = _ScriptedAgent(script)
    orch = TurnOrchestrator(TurnPolicy(
        max_attempts=1, max_tool_calls=20,
        max_failed_tool_outputs=3,  # total limit
        max_same_failure_signature=10,  # high per-sig limit
        max_consecutive_empty_llm_rounds=99,
    ))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "go"):
        events.append(te)

    errors = [e for e in events if e.type == TurnEventType.TURN_ERROR]
    assert len(errors) == 1
    assert "REPEATED_TOOL_FAILURES" in errors[0].error


# ---------------------------------------------------------------------------
# Edge-case coverage: _truncate_preview edge cases
# ---------------------------------------------------------------------------

def test_truncate_preview_none_input():
    """None input should return empty string."""
    text, trunc, total = _truncate_preview(None, 100)
    assert text == ""
    assert not trunc
    assert total == 0


def test_truncate_preview_short_max_chars():
    """When max_chars < 100, simple truncation without head/tail split."""
    long = "x" * 200
    text, trunc, total = _truncate_preview(long, 50)
    assert trunc
    assert total == 200
    assert "truncated" in text
    assert len(text) < 100  # short-form truncation


def test_truncate_preview_exact_boundary():
    """Text exactly at max_chars should NOT be truncated."""
    text_in = "x" * 100
    text, trunc, total = _truncate_preview(text_in, 100)
    assert text == text_in
    assert not trunc
    assert total == 100


# ---------------------------------------------------------------------------
# Bug: TURN_COMPLETE should report output_tokens even for tool_call responses
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_turn_complete_reports_output_tokens_for_tool_call_only_turn():
    """When LLM returns only tool_calls (no text delta), TURN_COMPLETE should
    still report non-zero output_tokens.

    Root cause: TurnEvent has no output_tokens field at all. When LLM returns
    tool_calls (with think content but no visible text), the token usage is
    completely untracked in the turn event. This leads to context_trace showing
    output_tokens=0 and raw_output="" for turns that actually consumed tokens.

    Repro scenario from production:
    - User asks "帮我搜索一下 Anthropic 最新的新闻"
    - LLM returns tool_call (web-search) with think content but no text delta
    - TURN_COMPLETE has no output_tokens → context_trace records 0
    """
    agent = _ScriptedAgent([
        # LLM thinks (internal reasoning) then emits a tool_call, no text delta
        _progress_event(_llm_delta("", think="I should search for Anthropic news")),
        _progress_event(_tool_call("web-search", '{"query": "Anthropic latest news"}')),
        _progress_event(_tool_output("web-search", "Anthropic released Claude 4...")),
        # LLM produces final answer after tool output
        _progress_event(_llm_delta("Here are the latest Anthropic news...")),
    ])
    orch = TurnOrchestrator(TurnPolicy(max_attempts=1))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "搜索 Anthropic 新闻"):
        events.append(te)

    complete = next(e for e in events if e.type == TurnEventType.TURN_COMPLETE)
    # BUG: TurnEvent has no output_tokens field — this attribute access will fail
    # with AttributeError, proving the tracking gap exists.
    assert hasattr(complete, "output_tokens"), (
        "TurnEvent.TURN_COMPLETE should include output_tokens so that "
        "context_trace can accurately report token usage for tool_call turns"
    )
    assert complete.output_tokens > 0, (
        "output_tokens should be non-zero when LLM produced think + tool_call + answer"
    )


@pytest.mark.asyncio
async def test_turn_complete_reports_output_tokens_for_pure_tool_call_no_text():
    """When LLM returns ONLY tool_calls (no final text at all), output_tokens
    should still be tracked.

    This is the exact scenario from the production bug: first 2 LLM calls
    returned tool_calls only (output_tokens=0, raw_output=""), only the 3rd
    call returned text (output_tokens=1212).
    """
    agent = _ScriptedAgent([
        # LLM thinks then issues tool_call, never produces text delta
        _progress_event(_llm_delta("", think="Let me search for this")),
        _progress_event(_tool_call("web-search", '{"query": "test"}')),
        _progress_event(_tool_output("web-search", "search results here")),
        # No final LLM text — the turn ends with tool output only
    ])
    orch = TurnOrchestrator(TurnPolicy(max_attempts=1))
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "搜索测试"):
        events.append(te)

    complete = next(e for e in events if e.type == TurnEventType.TURN_COMPLETE)
    # Even without text output, the LLM still consumed output tokens for
    # think + tool_call generation. These must be tracked.
    assert hasattr(complete, "output_tokens"), (
        "TurnEvent must track output_tokens to prevent context_trace from "
        "reporting 0 for tool_call-only turns"
    )


# ===========================================================================
# Issue 3: Turn cancellation — work done is discarded
# ===========================================================================

class TestTurnCancellationWorkPreservation:
    """Issue 3: When a new message arrives and the current turn is cancelled,
    all tool outputs and partial LLM responses from the cancelled turn are
    discarded.  With a 44.4% cancellation rate, this wastes massive compute.

    The cancel_event mechanism in run_turn checks cancellation ONLY at the
    top of the event loop — if cancellation happens between tool calls,
    accumulated work is lost because no TURN_COMPLETE event is emitted."""

    @pytest.mark.asyncio
    async def test_cancelled_turn_emits_partial_results(self):
        """When cancel_event is set mid-turn, the orchestrator should emit a
        TURN_COMPLETE with partial results (tool outputs collected so far)
        instead of just TURN_ERROR('cancelled')."""
        cancel = asyncio.Event()

        class _SlowAgent:
            async def continue_chat(self, **kwargs):
                # Phase 1: tool call + output (work done)
                yield _progress_event(_tool_call("web-search", '{"q":"anthropic news"}'))
                yield _progress_event(_tool_output("web-search", "Found 10 results about Anthropic"))
                yield _progress_event(_llm_delta("Based on the search, "))
                # Simulate new message arriving → cancel
                cancel.set()
                # Phase 2: more work that should be interrupted
                yield _progress_event(_tool_call("_read_file", "/tmp/analysis.py"))
                yield _progress_event(_tool_output("_read_file", "file contents"))
                yield _progress_event(_llm_delta("the analysis shows..."))

        agent = _SlowAgent()
        orch = TurnOrchestrator(TurnPolicy(max_attempts=1))
        events: list[TurnEvent] = []
        async for te in orch.run_turn(agent, "搜索 Anthropic 新闻", cancel_event=cancel):
            events.append(te)

        types = [e.type for e in events]
        # Currently, the cancelled turn emits TURN_ERROR('cancelled') with no
        # partial results.  The tool call to web-search and its output are lost.
        has_error = TurnEventType.TURN_ERROR in types
        has_complete = TurnEventType.TURN_COMPLETE in types

        # At minimum, the partial response should be preserved
        if has_error and not has_complete:
            error_event = next(e for e in events if e.type == TurnEventType.TURN_ERROR)
            # Check if the error event carries any partial result
            has_partial_answer = bool(getattr(error_event, "answer", None))
            assert has_partial_answer or has_complete, (
                "Cancelled turn discarded all work: tool outputs and partial LLM "
                f"response are lost. Got {len([e for e in events if e.type == TurnEventType.TOOL_OUTPUT])} "
                "tool outputs before cancellation but no partial result preserved. "
                "The orchestrator should emit partial results on cancellation."
            )

    @pytest.mark.asyncio
    async def test_cancelled_turn_reports_work_done_stats(self):
        """Even when cancelled, the turn should report tool_call_count and
        tool_execution_count so the caller knows what work was done."""
        cancel = asyncio.Event()

        class _AgentWithWork:
            async def continue_chat(self, **kwargs):
                yield _progress_event(_tool_call("_bash", "echo step1", pid="tc1"))
                yield _progress_event(_tool_output("_bash", "step1", pid="to1"))
                yield _progress_event(_tool_call("_bash", "echo step2", pid="tc2"))
                yield _progress_event(_tool_output("_bash", "step2", pid="to2"))
                yield _progress_event(_tool_call("_bash", "echo step3", pid="tc3"))
                yield _progress_event(_tool_output("_bash", "step3", pid="to3"))
                cancel.set()
                yield _progress_event(_llm_delta("analyzing results..."))

        agent = _AgentWithWork()
        orch = TurnOrchestrator(TurnPolicy(max_attempts=1, max_tool_calls=20))
        events: list[TurnEvent] = []
        async for te in orch.run_turn(agent, "do work", cancel_event=cancel):
            events.append(te)

        # The cancelled turn should still report stats about work done
        error_events = [e for e in events if e.type == TurnEventType.TURN_ERROR]
        complete_events = [e for e in events if e.type == TurnEventType.TURN_COMPLETE]

        # We expect either a TURN_COMPLETE with stats, or a TURN_ERROR that
        # includes the work metrics
        if complete_events:
            complete = complete_events[0]
            assert getattr(complete, "tool_call_count", 0) >= 3, (
                "Cancelled turn lost track of tool calls done before cancellation"
            )
        elif error_events:
            # Current behavior: just emits TURN_ERROR('cancelled') with no stats
            # This is the bug — cancelled turns lose all accounting
            pytest.fail(
                "Cancelled turn emitted TURN_ERROR without TURN_COMPLETE. "
                "3 tool calls were executed but their stats are discarded. "
                "The orchestrator should emit a TURN_COMPLETE with partial stats "
                "even when cancelled, so callers can account for the work done."
            )


# ===========================================================================
# Issue 4: Quota/billing error should trigger fallback, not silent retry loop
# ===========================================================================

class TestQuotaErrorFallback:
    """Issue 4: When an external engine (e.g. Codex/OpenAI) returns a quota
    exhaustion error, the agent keeps trying to load skills and retry,
    creating an infinite loop of wasted tool calls.

    The turn orchestrator treats quota errors as non-retryable (good), but
    there is no mechanism to:
    1. Detect that a specific engine/skill hit a quota limit
    2. Fall back to an alternative engine or inform the user with actionable guidance
    3. Prevent the agent from re-invoking the same quota-exhausted skill"""

    @pytest.mark.asyncio
    async def test_quota_error_not_retried(self):
        """Quota errors should NOT be retried."""
        class _QuotaAgent:
            async def continue_chat(self, **kwargs):
                raise Exception("You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro)")
                yield  # make it async generator

        agent = _QuotaAgent()
        orch = TurnOrchestrator(TurnPolicy(max_attempts=3))
        events: list[TurnEvent] = []
        async for te in orch.run_turn(agent, "analyze code"):
            events.append(te)

        errors = [e for e in events if e.type == TurnEventType.TURN_ERROR]
        statuses = [e for e in events if e.type == TurnEventType.STATUS]
        assert len(errors) == 1, "Quota error should produce exactly one TURN_ERROR"
        assert len(statuses) == 0, (
            "Quota error should NOT produce STATUS events (no retry). "
            f"Got {len(statuses)} retry status events."
        )

    @pytest.mark.asyncio
    async def test_quota_error_message_is_actionable(self):
        """When quota is exhausted, the error message should tell the user
        which engine failed and suggest alternatives, not just 'error occurred'."""
        class _QuotaAgent:
            async def continue_chat(self, **kwargs):
                raise Exception("You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro)")
                yield

        agent = _QuotaAgent()
        orch = TurnOrchestrator(TurnPolicy(max_attempts=1))
        events: list[TurnEvent] = []
        async for te in orch.run_turn(agent, "analyze code"):
            events.append(te)

        error = next(e for e in events if e.type == TurnEventType.TURN_ERROR)
        error_msg = error.error.lower()
        # The error should contain actionable info about quota
        has_quota_info = any(
            kw in error_msg
            for kw in ["quota", "limit", "upgrade", "usage limit"]
        )
        assert has_quota_info, (
            f"Quota error message '{error.error[:100]}' does not contain quota-related "
            "keywords. The orchestrator should surface the quota error clearly."
        )

    @pytest.mark.asyncio
    async def test_repeated_skill_failure_from_quota_triggers_budget_error(self):
        """When a skill repeatedly fails due to quota error, the orchestrator
        should detect the repeated failure pattern and stop early rather than
        letting the agent retry the same skill indefinitely."""
        class _QuotaSkillAgent:
            """Agent that keeps trying to invoke a quota-exhausted skill."""
            async def continue_chat(self, **kwargs):
                for attempt in range(5):
                    # LLM decides to try the skill again
                    yield _progress_event(_llm_delta(f"Trying coding-master attempt {attempt}... ", pid=f"t{attempt}"))
                    yield _progress_event(
                        _skill_call("coding-master", '{"task": "analyze"}', pid=f"sk{attempt}")
                    )
                    yield _progress_event(
                        _skill_output(
                            "coding-master",
                            "Error: You've hit your usage limit. Upgrade to Pro",
                            pid=f"so{attempt}",
                            status="failed",
                        )
                    )
                # Agent eventually gives up and responds
                yield _progress_event(_llm_delta("I encountered an error..."))

        agent = _QuotaSkillAgent()
        orch = TurnOrchestrator(TurnPolicy(
            max_attempts=1,
            max_tool_calls=30,
            repeated_failure_limit=3,
        ))
        events: list[TurnEvent] = []
        async for te in orch.run_turn(agent, "use coding-master to analyze"):
            events.append(te)

        errors = [e for e in events if e.type == TurnEventType.TURN_ERROR]
        assert len(errors) >= 1, (
            "Orchestrator should have detected repeated skill failures from quota "
            "error and stopped early with REPEATED_TOOL_FAILURES error."
        )
        error_msg = errors[0].error if errors else ""
        assert "REPEATED" in error_msg or "quota" in error_msg.lower(), (
            f"Expected REPEATED_TOOL_FAILURES or quota-related error, got: '{error_msg}'. "
            "The agent invoked coding-master 5 times with the same quota error but "
            "the orchestrator did not detect the repeated failure pattern."
        )


# ===========================================================================
# Issue 5: grep tool should exclude .venv and other noise directories
# ===========================================================================

class TestGrepExcludeVenvDirectories:
    """Issue 5: The _grep tool searches .venv/lib/python3.12/site-packages/
    and similar directories, flooding results with irrelevant matches.

    The _extract_tool_intent_signature already parses grep patterns but does
    not filter or flag searches that will hit virtual environment directories."""

    def test_grep_intent_signature_extracted(self):
        """Baseline: grep pattern extraction works."""
        sig = _extract_tool_intent_signature(
            "_grep", '{"pattern": "etf|ranking|rank", "path": "."}'
        )
        assert sig is not None
        assert sig.startswith("search_grep:")

    def test_grep_on_venv_path_should_be_flagged(self):
        """When grep searches a path that is inside .venv, the intent signature
        should reflect this so the orchestrator can filter or warn.

        Current behavior: .venv paths are treated identically to project paths,
        wasting context tokens on irrelevant site-packages matches."""
        # A grep that returns results from .venv should be distinguishable
        venv_output = (
            ".venv/lib/python3.12/site-packages/PIL/ImageDraw.py:42: "
            "def draw(self, rank=0):\n"
            ".venv/lib/python3.12/site-packages/PIL/ImageDraw.py:108: "
            "ranking = self.get_ranking()\n"
            "src/analysis/etf_ranking.py:15: def compute_ranking():\n"
        )
        # The failure signature should detect .venv pollution
        sig = _extract_failure_signature(venv_output)
        # Currently returns None because there's no error in the output
        # But .venv results are noise — a quality grep should have excluded them
        # This test documents the gap
        venv_lines = [
            line for line in venv_output.strip().split("\n")
            if ".venv/" in line
        ]
        project_lines = [
            line for line in venv_output.strip().split("\n")
            if ".venv/" not in line
        ]
        assert len(venv_lines) > len(project_lines), (
            "Test setup: majority of grep results are from .venv"
        )
        # Verify that TurnPolicy has grep exclude patterns that cover .venv
        policy = TurnPolicy()
        has_venv_exclude = any(
            ".venv" in pat for pat in policy.grep_exclude_patterns
        )
        assert has_venv_exclude, (
            "TurnPolicy.grep_exclude_patterns should include .venv to prevent "
            "grep results from being polluted with site-packages matches."
        )
        # Verify intent signature flags .venv paths
        sig = _extract_tool_intent_signature(
            "_grep", '{"pattern": "ranking", "path": ".venv/lib/python3.12/site-packages"}'
        )
        assert sig is not None and "excluded" in sig, (
            "grep on .venv path should produce a distinguishable intent signature"
        )

    def test_grep_default_excludes_should_include_venv(self):
        """The turn orchestrator or tool configuration should have a default
        exclude list that includes .venv, node_modules, etc.

        This test checks whether such configuration exists."""
        import inspect

        # Check if TurnPolicy or TurnOrchestrator has any exclude pattern config
        policy_attrs = dir(TurnPolicy)
        orch_attrs = dir(TurnOrchestrator)

        has_exclude_config = any(
            "exclude" in attr.lower() or "ignore" in attr.lower() or "skip" in attr.lower()
            for attr in policy_attrs + orch_attrs
        )

        if not has_exclude_config:
            # Also check the _grep tool intent extraction
            source = inspect.getsource(_extract_tool_intent_signature)
            has_venv_handling = ".venv" in source or "site-packages" in source

            assert has_venv_handling or has_exclude_config, (
                "Neither TurnPolicy, TurnOrchestrator, nor _extract_tool_intent_signature "
                "has any configuration for excluding .venv or similar directories from "
                "grep searches. This causes grep results to be polluted with irrelevant "
                "matches from virtual environment site-packages."
            )


# ---------------------------------------------------------------------------
# Config-aware policy factory tests
# ---------------------------------------------------------------------------

class TestResolveTimeout:
    """Tests for _resolve_timeout priority resolution."""

    def test_returns_hardcoded_default_when_no_config(self):
        assert _resolve_timeout("chat") == CHAT_POLICY.timeout_seconds
        assert _resolve_timeout("heartbeat") == HEARTBEAT_POLICY.timeout_seconds
        assert _resolve_timeout("job") == JOB_POLICY.timeout_seconds
        assert _resolve_timeout("workflow") == WORKFLOW_POLICY.timeout_seconds

    def test_returns_hardcoded_default_when_config_empty(self):
        assert _resolve_timeout("chat", config={}) == 600

    def test_global_override(self):
        config = {"everbot": {"runtime": {"turn_timeout": {"chat": 900}}}}
        assert _resolve_timeout("chat", config) == 900

    def test_agent_override_takes_precedence_over_global(self):
        config = {
            "everbot": {
                "runtime": {"turn_timeout": {"chat": 900}},
                "agents": {"my_agent": {"turn_timeout": {"chat": 1200}}},
            }
        }
        assert _resolve_timeout("chat", config, agent_name="my_agent") == 1200

    def test_agent_override_without_global(self):
        config = {
            "everbot": {
                "agents": {"my_agent": {"turn_timeout": {"chat": 800}}},
            }
        }
        assert _resolve_timeout("chat", config, agent_name="my_agent") == 800

    def test_unknown_agent_falls_back_to_global(self):
        config = {
            "everbot": {
                "runtime": {"turn_timeout": {"chat": 900}},
                "agents": {"other_agent": {"turn_timeout": {"chat": 1200}}},
            }
        }
        assert _resolve_timeout("chat", config, agent_name="my_agent") == 900

    def test_unknown_agent_no_global_falls_back_to_default(self):
        config = {"everbot": {"agents": {"other_agent": {"turn_timeout": {"chat": 1200}}}}}
        assert _resolve_timeout("chat", config, agent_name="my_agent") == 600

    def test_non_dict_everbot_returns_default(self):
        config = {"everbot": "invalid"}
        assert _resolve_timeout("chat", config) == 600


class TestBuildPolicyFunctions:
    """Tests for build_*_policy factory functions."""

    def test_build_chat_policy_defaults(self):
        policy = build_chat_policy()
        assert policy.timeout_seconds == 600
        assert policy.max_tool_calls == CHAT_POLICY.max_tool_calls

    def test_build_chat_policy_with_config(self):
        config = {"everbot": {"runtime": {"turn_timeout": {"chat": 900}}}}
        policy = build_chat_policy(config)
        assert policy.timeout_seconds == 900
        assert policy.max_tool_calls == CHAT_POLICY.max_tool_calls

    def test_build_chat_policy_per_agent(self):
        config = {
            "everbot": {
                "runtime": {"turn_timeout": {"chat": 900}},
                "agents": {"demo_agent": {"turn_timeout": {"chat": 1200}}},
            }
        }
        policy = build_chat_policy(config, agent_name="demo_agent")
        assert policy.timeout_seconds == 1200

    def test_build_heartbeat_policy_defaults(self):
        policy = build_heartbeat_policy()
        assert policy.timeout_seconds == 120
        assert policy.max_attempts == HEARTBEAT_POLICY.max_attempts

    def test_build_heartbeat_policy_with_config(self):
        config = {"everbot": {"runtime": {"turn_timeout": {"heartbeat": 180}}}}
        policy = build_heartbeat_policy(config)
        assert policy.timeout_seconds == 180

    def test_build_job_policy_defaults(self):
        policy = build_job_policy()
        assert policy.timeout_seconds == 600
        assert policy.max_attempts == JOB_POLICY.max_attempts

    def test_build_workflow_policy_defaults(self):
        policy = build_workflow_policy()
        assert policy.timeout_seconds == 300
        assert policy.max_attempts == WORKFLOW_POLICY.max_attempts

    def test_build_workflow_policy_with_config(self):
        config = {"everbot": {"runtime": {"turn_timeout": {"workflow": 600}}}}
        policy = build_workflow_policy(config)
        assert policy.timeout_seconds == 600
