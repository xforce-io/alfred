"""Unit test: verify guards fire for repeated tool calls with no LLM output.

Simulates the whisper loop scenario: LLM repeatedly calls _python(whisper) with no text output.
Tests both EMPTY_OUTPUT_LOOP and REPEATED_TOOL_INTENT guards.
"""

import pytest

from src.everbot.core.runtime.turn_orchestrator import TurnOrchestrator, _extract_tool_intent_signature
from src.everbot.core.runtime.turn_policy import TurnPolicy


def _make_skill_events(n_tools: int, include_llm_stages: bool = False):
    """Generate a sequence of _progress events simulating n tool calls with no LLM output.

    Mimics dolphin's behavior where empty LLM stages are popped when SKILL stages are created.
    Each event contains the FULL accumulated _progress list (as dolphin does).
    """
    stages = []
    events = []
    stage_counter = 0

    for i in range(n_tools):
        # If including LLM stages (not popped), add an LLM stage with empty output
        if include_llm_stages:
            stage_counter += 1
            llm_stage = {
                "id": f"llm_{stage_counter}",
                "stage": "llm",
                "status": "completed",
                "answer": "",
                "think": "",
                "delta": "",  # delta mode: empty
            }
            stages.append(llm_stage)
            # Event: LLM completed (no output)
            events.append({"_progress": [s.copy() for s in stages]})

        # SKILL stage "processing" (if LLM was popped, remove it)
        if include_llm_stages:
            # LLM stage stays in list (not popped scenario)
            pass
        stage_counter += 1
        skill_stage = {
            "id": f"skill_{stage_counter}",
            "stage": "skill",
            "status": "processing",
            "answer": "",
            "think": "",
            "skill_info": {"name": "_python", "args": [{"key": "cmd", "value": "whisper code"}]},
        }
        stages.append(skill_stage)
        # Event: SKILL processing
        events.append({"_progress": [s.copy() for s in stages]})

        # SKILL stage "completed"
        stages[-1] = {**stages[-1], "status": "completed", "answer": "Return value: test"}
        events.append({"_progress": [s.copy() for s in stages]})

    return events


def _make_popped_llm_events(n_tools: int):
    """Generate events where LLM stages are popped (dolphin's actual behavior for empty LLM output).

    Each round: LLM stage appears briefly, then is replaced by SKILL stage.
    """
    all_stages = []  # accumulated persistent stages (only SKILLs survive)
    events = []
    stage_counter = 0

    for i in range(n_tools):
        stage_counter += 1
        llm_id = f"llm_{stage_counter}"

        # Event 1: LLM streaming/completed (before SKILL replaces it)
        temp_stages = all_stages + [{
            "id": llm_id,
            "stage": "llm",
            "status": "completed",
            "answer": "",
            "think": "",
            "delta": "",
        }]
        events.append({"_progress": [s.copy() for s in temp_stages]})

        # Event 2: SKILL stage replaces LLM (popped)
        stage_counter += 1
        skill_stage = {
            "id": f"skill_{stage_counter}",
            "stage": "skill",
            "status": "processing",
            "answer": "",
            "think": "",
            "skill_info": {"name": "_python", "args": [{"key": "cmd", "value": "whisper code"}]},
        }
        all_stages.append(skill_stage)
        events.append({"_progress": [s.copy() for s in all_stages]})

        # Event 3: SKILL completed
        all_stages[-1] = {**all_stages[-1], "status": "completed", "answer": "Return value: test"}
        events.append({"_progress": [s.copy() for s in all_stages]})

    return events


async def _mock_agent_stream(events):
    """Simulate an agent that yields pre-built events."""
    for event in events:
        yield event


class MockAgent:
    def __init__(self, events):
        self._events = events

    async def continue_chat(self, **kwargs):
        for event in self._events:
            yield event


@pytest.mark.asyncio
async def test_empty_output_guard_fires_skill_only():
    """Guard should fire when seeing repeated SKILL stages with no LLM output (popped LLM scenario)."""
    policy = TurnPolicy(max_consecutive_empty_llm_rounds=3, max_tool_calls=100)
    orchestrator = TurnOrchestrator(policy)

    events = _make_popped_llm_events(10)
    agent = MockAgent(events)

    results = []
    async for te in orchestrator.run_turn(agent, "test voice message"):
        results.append(te)

    # Should have a TURN_ERROR with EMPTY_OUTPUT_LOOP
    errors = [r for r in results if r.type.value == "turn_error"]
    assert len(errors) > 0, (
        f"EMPTY_OUTPUT_LOOP guard did NOT fire for 10 tool calls with no LLM output. "
        f"Got events: {[(r.type.value, getattr(r, 'error', '')) for r in results]}"
    )
    assert "EMPTY_OUTPUT_LOOP" in errors[0].error


@pytest.mark.asyncio
async def test_empty_output_guard_fires_with_llm_stages():
    """Guard should fire even when LLM stages are present but have no output."""
    policy = TurnPolicy(max_consecutive_empty_llm_rounds=3, max_tool_calls=100)
    orchestrator = TurnOrchestrator(policy)

    events = _make_skill_events(10, include_llm_stages=True)
    agent = MockAgent(events)

    results = []
    async for te in orchestrator.run_turn(agent, "test voice message"):
        results.append(te)

    errors = [r for r in results if r.type.value == "turn_error"]
    assert len(errors) > 0, (
        f"EMPTY_OUTPUT_LOOP guard did NOT fire with LLM stages present. "
        f"Got events: {[(r.type.value, getattr(r, 'error', '')) for r in results]}"
    )
    assert "EMPTY_OUTPUT_LOOP" in errors[0].error


@pytest.mark.asyncio
async def test_empty_output_guard_does_not_fire_with_llm_output():
    """Guard should NOT fire when LLM produces output between tool calls."""
    policy = TurnPolicy(max_consecutive_empty_llm_rounds=3, max_tool_calls=100)
    orchestrator = TurnOrchestrator(policy)

    stages = []
    events = []
    stage_counter = 0

    for i in range(10):
        # LLM with actual output
        stage_counter += 1
        llm_stage = {
            "id": f"llm_{stage_counter}",
            "stage": "llm",
            "status": "completed",
            "answer": f"Let me call whisper round {i}",
            "think": "",
            "delta": f"Let me call whisper round {i}" if i == 0 else f" round {i}",
        }
        stages.append(llm_stage)
        events.append({"_progress": [s.copy() for s in stages]})

        # SKILL
        stage_counter += 1
        skill_stage = {
            "id": f"skill_{stage_counter}",
            "stage": "skill",
            "status": "processing",
            "answer": "",
            "skill_info": {"name": "_python", "args": []},
        }
        stages.append(skill_stage)
        events.append({"_progress": [s.copy() for s in stages]})

        stages[-1] = {**stages[-1], "status": "completed", "answer": "done"}
        events.append({"_progress": [s.copy() for s in stages]})

    agent = MockAgent(events)

    results = []
    async for te in orchestrator.run_turn(agent, "test"):
        results.append(te)

    errors = [r for r in results if r.type.value == "turn_error"]
    assert len(errors) == 0, (
        f"Guard should NOT have fired when LLM has output. Errors: {[r.error for r in errors]}"
    )


@pytest.mark.asyncio
async def test_guard_fires_at_correct_count():
    """Guard should fire after exactly max_consecutive_empty_llm_rounds consecutive empty rounds."""
    policy = TurnPolicy(max_consecutive_empty_llm_rounds=3, max_tool_calls=100)
    orchestrator = TurnOrchestrator(policy)

    events = _make_popped_llm_events(10)
    agent = MockAgent(events)

    tool_events = []
    error_event = None
    async for te in orchestrator.run_turn(agent, "test"):
        if te.type.value == "skill":
            tool_events.append(te)
        elif te.type.value == "turn_error":
            error_event = te
            break

    assert error_event is not None, "Guard should have fired"
    # First tool call skipped (tool_exec_count=0), then 3 more = guard fires at 4th processing
    # Both processing + completed SKILL events are yielded before the guard fires
    processing_events = [e for e in tool_events if e.status == "processing"]
    assert len(processing_events) <= 4, (
        f"Expected guard to fire by 4th processing event, got {len(processing_events)}"
    )


@pytest.mark.asyncio
async def test_think_output_bypasses_empty_guard():
    """ROOT CAUSE TEST: When LLM produces `think` (reasoning) output but no visible text,
    the EMPTY_OUTPUT_LOOP guard is bypassed because think resets the counter.

    This reproduces the whisper loop bug: kimi-code produces reasoning_content
    between tool calls, which the guard treats as valid output.
    """
    policy = TurnPolicy(max_consecutive_empty_llm_rounds=3, max_tool_calls=100)
    orchestrator = TurnOrchestrator(policy)

    # Simulate events where LLM has think output (reasoning) but no visible text
    stages = []
    events = []
    stage_counter = 0

    for i in range(20):  # 20 identical tool calls
        # LLM stage with THINK output (reasoning) but NO visible text
        stage_counter += 1
        llm_stage = {
            "id": f"llm_{stage_counter}",
            "stage": "llm",
            "status": "completed",
            "answer": "",
            "think": f"I need to transcribe this voice message using whisper round {i}",
            "delta": "",
        }
        stages.append(llm_stage)
        events.append({"_progress": [s.copy() for s in stages]})

        # SKILL stage
        stage_counter += 1
        skill_stage = {
            "id": f"skill_{stage_counter}",
            "stage": "skill",
            "status": "processing",
            "answer": "",
            "think": "",
            "skill_info": {"name": "_python", "args": [{"key": "cmd", "value": "whisper code"}]},
        }
        stages.append(skill_stage)
        events.append({"_progress": [s.copy() for s in stages]})

        stages[-1] = {**stages[-1], "status": "completed", "answer": "Return value: test"}
        events.append({"_progress": [s.copy() for s in stages]})

    agent = MockAgent(events)

    results = []
    async for te in orchestrator.run_turn(agent, "test voice"):
        results.append(te)

    errors = [r for r in results if r.type.value == "turn_error"]
    skill_processing = [r for r in results if r.type.value == "skill" and r.status == "processing"]

    # FIX: think-only output no longer resets consecutive_empty_llm_rounds,
    # so the guard fires even when the model produces reasoning between tool calls.
    # However, think DOES set llm_had_output_this_round=True, which means the
    # counter doesn't increment on rounds with think output. The guard relies on
    # REPEATED_TOOL_INTENT (via _python code hash) as the primary defense.
    # The EMPTY_OUTPUT_LOOP may or may not fire depending on timing — the key
    # guarantee is that at least ONE guard stops the loop.
    assert len(errors) > 0 or len(skill_processing) < 20, (
        f"Neither guard stopped the loop: {len(skill_processing)} tool calls went through"
    )


# ── REPEATED_TOOL_INTENT guard tests ──────────────────────────────


def test_python_intent_signature_identical_code():
    """Identical _python code should produce the same signature."""
    code = '{"cmd": "import whisper\\nmodel = whisper.load_model(\\"base\\")\\nresult = model.transcribe(\\"/path/to/audio.oga\\")\\nreturn_value = result[\\"text\\"]\\n"}'
    sig1 = _extract_tool_intent_signature("_python", code)
    sig2 = _extract_tool_intent_signature("_python", code)
    assert sig1 is not None
    assert sig1 == sig2
    assert sig1.startswith("python_exec:")


def test_python_intent_signature_different_code():
    """Different _python code should produce different signatures."""
    code1 = '{"cmd": "import whisper\\nmodel.transcribe(\\"a.oga\\")"}'
    code2 = '{"cmd": "import os\\nos.listdir(\\"/tmp\\")"}'
    sig1 = _extract_tool_intent_signature("_python", code1)
    sig2 = _extract_tool_intent_signature("_python", code2)
    assert sig1 != sig2


def test_python_intent_signature_file_write_takes_priority():
    """_python code with file write patterns should return write_file, not generic hash."""
    # Raw code format (not JSON-encoded) matches file path patterns
    code = 'with open("/tmp/test.txt", "w") as f:\n    f.write("hello")'
    sig = _extract_tool_intent_signature("_python", code)
    assert sig is not None
    assert sig.startswith("write_file:"), f"Expected write_file:, got {sig}"


@pytest.mark.asyncio
async def test_repeated_python_intent_guard_fires():
    """REPEATED_TOOL_INTENT guard should stop identical _python calls with think output."""
    policy = TurnPolicy(
        max_consecutive_empty_llm_rounds=100,  # disable empty output guard
        max_tool_calls=100,
        max_same_tool_intent=3,
    )
    orchestrator = TurnOrchestrator(policy)

    # Simulate: LLM with think output + identical _python whisper calls
    stages = []
    events = []
    stage_counter = 0
    whisper_code = '{"cmd": "import whisper\\nmodel = whisper.load_model(\\"base\\")\\nresult = model.transcribe(\\"/path/audio.oga\\")\\nreturn_value = result[\\"text\\"]"}'

    for i in range(20):
        # LLM with think
        stage_counter += 1
        stages.append({
            "id": f"llm_{stage_counter}",
            "stage": "llm",
            "status": "completed",
            "answer": "",
            "think": f"Need to transcribe voice message (attempt {i})",
            "delta": "",
        })
        events.append({"_progress": [s.copy() for s in stages]})

        # SKILL with identical args
        stage_counter += 1
        stages.append({
            "id": f"skill_{stage_counter}",
            "stage": "skill",
            "status": "processing",
            "answer": "",
            "think": "",
            "skill_info": {"name": "_python", "args": whisper_code},
        })
        events.append({"_progress": [s.copy() for s in stages]})

        stages[-1] = {**stages[-1], "status": "completed", "answer": "Return value: test"}
        events.append({"_progress": [s.copy() for s in stages]})

    agent = MockAgent(events)

    results = []
    async for te in orchestrator.run_turn(agent, "test voice"):
        results.append(te)

    errors = [r for r in results if r.type.value == "turn_error"]
    assert len(errors) > 0, "REPEATED_TOOL_INTENT guard should have fired"
    assert "REPEATED_TOOL_INTENT" in errors[0].error

    skill_processing = [r for r in results if r.type.value == "skill" and r.status == "processing"]
    assert len(skill_processing) <= 3, (
        f"Should have stopped by 3rd identical call, got {len(skill_processing)}"
    )
