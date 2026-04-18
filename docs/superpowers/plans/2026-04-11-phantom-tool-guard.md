# Phantom Tool Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect and stop LLM calls to unregistered (phantom) tools, preventing wasted turns from hallucinated tool names.

**Architecture:** TurnOrchestrator accepts a callable that returns the current set of registered tool names. On each `tool_call` event, it checks membership. First phantom call gets a correction injected into `tool_output`; second phantom call to the same tool terminates the turn with `TURN_ERROR`.

**Tech Stack:** Python, pytest, existing TurnOrchestrator infrastructure.

**Spec:** `docs/superpowers/specs/2026-04-11-phantom-tool-guard-design.md`

---

### Task 1: Add `max_phantom_tool_calls` to TurnPolicy

**Files:**
- Modify: `src/everbot/core/runtime/turn_policy.py:63-98`

- [ ] **Step 1: Write the failing test**

In `tests/unit/test_turn_orchestrator.py`, add:

```python
@pytest.mark.asyncio
async def test_phantom_tool_first_call_injects_warning():
    """First call to an unregistered tool injects a correction into tool_output."""
    script = [
        _progress_event(_tool_call("_cm_next", '{"repo": "kweaver"}', pid="tc1")),
        _progress_event(_tool_output("_cm_next", "some garbage output", pid="to1")),
        _progress_event(_llm_delta("OK I will use bash instead")),
    ]
    agent = _ScriptedAgent(script)
    orch = TurnOrchestrator(
        TurnPolicy(max_attempts=1, max_tool_calls=20, max_phantom_tool_calls=1),
        get_registered_tools=lambda: {"_bash", "_python", "_grep"},
    )
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "go"):
        events.append(te)

    outputs = [e for e in events if e.type == TurnEventType.TOOL_OUTPUT]
    assert len(outputs) == 1
    assert "not a registered tool" in outputs[0].tool_output
    # Turn should complete, not error
    errors = [e for e in events if e.type == TurnEventType.TURN_ERROR]
    assert len(errors) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_turn_orchestrator.py::test_phantom_tool_first_call_injects_warning -xvs`
Expected: FAIL — `TurnOrchestrator` does not accept `get_registered_tools` parameter.

- [ ] **Step 3: Add `max_phantom_tool_calls` to TurnPolicy**

In `src/everbot/core/runtime/turn_policy.py`, add after line 94 (`budget_exempt_tools`):

```python
    # Maximum times an unregistered (phantom) tool may be called before
    # the turn is terminated.  The first call is allowed to execute (since
    # the orchestrator cannot cancel in-flight Dolphin tool execution) but
    # receives a correction prompt in its tool_output.
    max_phantom_tool_calls: int = 1
```

- [ ] **Step 4: Commit**

```bash
git add src/everbot/core/runtime/turn_policy.py tests/unit/test_turn_orchestrator.py
git commit -m "feat(turn-policy): add max_phantom_tool_calls parameter"
```

---

### Task 2: Wire `get_registered_tools` into TurnOrchestrator and TurnExecutor

**Files:**
- Modify: `src/everbot/core/runtime/turn_orchestrator.py:321-334`
- Modify: `src/everbot/core/runtime/turn_executor.py:91-93`

- [ ] **Step 1: Add `get_registered_tools` to TurnOrchestrator.__init__**

In `src/everbot/core/runtime/turn_orchestrator.py`, change `__init__`:

```python
    def __init__(
        self,
        policy: Optional[TurnPolicy] = None,
        prior_failures: Optional[Dict[str, int]] = None,
        get_registered_tools: Optional[Callable[[], set]] = None,
    ):
        self.policy = policy or TurnPolicy()
        self._get_registered_tools = get_registered_tools
        self._prior_failures: Dict[str, int] = dict(prior_failures or {})
        self.accumulated_failures: Dict[str, int] = dict(self._prior_failures)
```

Add `Callable` to the typing imports at the top of the file if not already present.

- [ ] **Step 2: Wire callback in TurnExecutor.stream_turn**

In `src/everbot/core/runtime/turn_executor.py`, change lines 91-93:

```python
            policy = _SESSION_TYPE_POLICIES.get(session_type)
            if policy is not None:
                # Build tool-name lookup for phantom-tool guard
                get_tools = None
                if hasattr(agent, "get_skillkit_raw") and agent.get_skillkit_raw() is not None:
                    skillkit_raw = agent.get_skillkit_raw()
                    get_tools = lambda: set(skillkit_raw.getSkillNames())  # noqa: E731
                orchestrator = TurnOrchestrator(policy, get_registered_tools=get_tools)
```

- [ ] **Step 3: Run test to verify it still fails (for the right reason now)**

Run: `python -m pytest tests/unit/test_turn_orchestrator.py::test_phantom_tool_first_call_injects_warning -xvs`
Expected: FAIL — orchestrator accepts the param now but doesn't use it yet, so `"not a registered tool"` is not in output.

- [ ] **Step 4: Commit**

```bash
git add src/everbot/core/runtime/turn_orchestrator.py src/everbot/core/runtime/turn_executor.py
git commit -m "feat(orchestrator): accept get_registered_tools callback"
```

---

### Task 3: Implement phantom tool guard logic in `_run_attempt`

**Files:**
- Modify: `src/everbot/core/runtime/turn_orchestrator.py` — `_run_attempt` method, `stage == "tool_call"` and `stage == "tool_output"` sections

- [ ] **Step 1: Add phantom tool check in `tool_call` stage**

In `_run_attempt`, after the existing local variable declarations (around line 558), add:

```python
        phantom_tool_counts: Dict[str, int] = {}
        phantom_pids: Dict[str, str] = {}  # pid -> tool_name
```

In the `stage == "tool_call"` branch, right after `t_name = progress.get("tool_name", "")` (line 859) and before the empty-output loop detection, add:

```python
                    # Phantom tool guard: detect calls to unregistered tools
                    if self._get_registered_tools is not None:
                        registered = self._get_registered_tools()
                        if t_name and t_name not in registered:
                            phantom_tool_counts[t_name] = phantom_tool_counts.get(t_name, 0) + 1
                            if phantom_tool_counts[t_name] > policy.max_phantom_tool_calls:
                                _flush_trajectory()
                                yield TurnEvent(
                                    type=TurnEventType.TURN_ERROR,
                                    error=(
                                        f"PHANTOM_TOOL: tool `{t_name}` is not registered, "
                                        f"called {phantom_tool_counts[t_name]} times, "
                                        f"limit={policy.max_phantom_tool_calls}"
                                    ),
                                    answer=response,
                                    tool_call_count=tool_call_count,
                                    tool_execution_count=tool_execution_count,
                                    tool_names_executed=list(tool_names_executed),
                                    failed_tool_outputs=failed_tool_outputs,
                                )
                                return
                            if pid:
                                phantom_pids[pid] = t_name
```

- [ ] **Step 2: Add correction injection in `tool_output` stage**

In the `stage == "tool_output"` branch, after the existing warning injections (after the `tool_intent_last_output` block, before the `yield TurnEvent(type=TurnEventType.TOOL_OUTPUT, ...)` at line 1007), add:

```python
                    # Phantom tool correction: tell LLM this tool doesn't exist
                    _phantom_name = phantom_pids.pop(pid, None) if pid else None
                    if _phantom_name:
                        out_preview += (
                            f"\n[⚠ PHANTOM_TOOL: `{_phantom_name}` is not a registered tool"
                            f" and cannot be called. Use registered tools"
                            f" (_bash, _python, _grep, etc.) to complete the task.]"
                        )
```

- [ ] **Step 3: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_turn_orchestrator.py::test_phantom_tool_first_call_injects_warning -xvs`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/everbot/core/runtime/turn_orchestrator.py
git commit -m "feat(orchestrator): implement phantom tool guard"
```

---

### Task 4: Add test for second phantom call triggering TURN_ERROR

**Files:**
- Modify: `tests/unit/test_turn_orchestrator.py`

- [ ] **Step 1: Write the test**

```python
@pytest.mark.asyncio
async def test_phantom_tool_second_call_triggers_error():
    """Second call to same unregistered tool triggers TURN_ERROR."""
    script = [
        _progress_event(_tool_call("_cm_next", '{"repo": "a"}', pid="tc1")),
        _progress_event(_tool_output("_cm_next", "garbage", pid="to1")),
        _progress_event(_tool_call("_cm_next", '{"repo": "b"}', pid="tc2")),
        # This tool_output should never be reached
        _progress_event(_tool_output("_cm_next", "more garbage", pid="to2")),
    ]
    agent = _ScriptedAgent(script)
    orch = TurnOrchestrator(
        TurnPolicy(max_attempts=1, max_tool_calls=20, max_phantom_tool_calls=1),
        get_registered_tools=lambda: {"_bash", "_python"},
    )
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "go"):
        events.append(te)

    errors = [e for e in events if e.type == TurnEventType.TURN_ERROR]
    assert len(errors) == 1
    assert "PHANTOM_TOOL" in errors[0].error
    assert "_cm_next" in errors[0].error
```

- [ ] **Step 2: Run test**

Run: `python -m pytest tests/unit/test_turn_orchestrator.py::test_phantom_tool_second_call_triggers_error -xvs`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_turn_orchestrator.py
git commit -m "test(orchestrator): phantom tool second call triggers TURN_ERROR"
```

---

### Task 5: Add test for dynamic tool registration

**Files:**
- Modify: `tests/unit/test_turn_orchestrator.py`

- [ ] **Step 1: Write the test**

```python
@pytest.mark.asyncio
async def test_phantom_tool_passes_after_dynamic_registration():
    """Tool initially unregistered becomes valid after dynamic registration."""
    registered = {"_bash", "_python"}

    script = [
        # First call: _cm_next not registered → phantom warning
        _progress_event(_tool_call("_cm_next", '{"repo": "a"}', pid="tc1")),
        _progress_event(_tool_output("_cm_next", "garbage", pid="to1")),
        # Simulate dynamic registration (e.g. via _load_resource_skill)
        # by mutating the set before the second call
        _progress_event(_tool_call("_bash", 'echo "loading skill"', pid="tc2")),
        _progress_event(_tool_output("_bash", "loading skill", pid="to2")),
        # Second call: _cm_next now registered → should NOT trigger error
        _progress_event(_tool_call("_cm_next", '{"repo": "a"}', pid="tc3")),
        _progress_event(_tool_output("_cm_next", "real output", pid="to3")),
        _progress_event(_llm_delta("Done")),
    ]
    agent = _ScriptedAgent(script)

    call_count = [0]
    def get_tools():
        # After 2 tool executions, _cm_next becomes registered
        if call_count[0] >= 2:
            return {"_bash", "_python", "_cm_next"}
        call_count[0] += 1
        return set(registered)

    orch = TurnOrchestrator(
        TurnPolicy(max_attempts=1, max_tool_calls=20, max_phantom_tool_calls=1),
        get_registered_tools=get_tools,
    )
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "go"):
        events.append(te)

    errors = [e for e in events if e.type == TurnEventType.TURN_ERROR]
    assert len(errors) == 0
    # First output should have warning, third should not
    outputs = [e for e in events if e.type == TurnEventType.TOOL_OUTPUT]
    assert "not a registered tool" in outputs[0].tool_output
    assert "not a registered tool" not in outputs[2].tool_output
```

- [ ] **Step 2: Run test**

Run: `python -m pytest tests/unit/test_turn_orchestrator.py::test_phantom_tool_passes_after_dynamic_registration -xvs`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_turn_orchestrator.py
git commit -m "test(orchestrator): phantom tool clears after dynamic registration"
```

---

### Task 6: Add test for no callback (backward compatibility)

**Files:**
- Modify: `tests/unit/test_turn_orchestrator.py`

- [ ] **Step 1: Write the test**

```python
@pytest.mark.asyncio
async def test_phantom_tool_guard_disabled_without_callback():
    """When get_registered_tools is None, phantom guard does not activate."""
    script = [
        _progress_event(_tool_call("_nonexistent", "args", pid="tc1")),
        _progress_event(_tool_output("_nonexistent", "whatever", pid="to1")),
        _progress_event(_tool_call("_nonexistent", "args", pid="tc2")),
        _progress_event(_tool_output("_nonexistent", "whatever", pid="to2")),
        _progress_event(_llm_delta("done")),
    ]
    agent = _ScriptedAgent(script)
    # No get_registered_tools passed
    orch = TurnOrchestrator(
        TurnPolicy(max_attempts=1, max_tool_calls=20),
    )
    events: list[TurnEvent] = []
    async for te in orch.run_turn(agent, "go"):
        events.append(te)

    errors = [e for e in events if e.type == TurnEventType.TURN_ERROR]
    assert len(errors) == 0
    outputs = [e for e in events if e.type == TurnEventType.TOOL_OUTPUT]
    assert all("not a registered tool" not in o.tool_output for o in outputs)
```

- [ ] **Step 2: Run test**

Run: `python -m pytest tests/unit/test_turn_orchestrator.py::test_phantom_tool_guard_disabled_without_callback -xvs`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_turn_orchestrator.py
git commit -m "test(orchestrator): phantom guard inactive without callback"
```

---

### Task 7: Run full test suite and verify no regressions

**Files:** None (verification only)

- [ ] **Step 1: Run unit tests**

Run: `python -m pytest tests/unit/ -x -q`
Expected: All pass (1312+)

- [ ] **Step 2: Run integration tests**

Run: `python -m pytest tests/integration/ -q --deselect tests/integration/test_heartbeat_token_optimizations.py::TestAgentCreationSkip::test_reflect_runs_after_file_change --deselect tests/integration/test_slm_lifecycle.py::TestSLMLifecycle::test_full_lifecycle --deselect tests/integration/test_slm_lifecycle.py::TestSLMSuccessfulUpgrade::test_successful_upgrade`
Expected: All pass (3 pre-existing failures deselected)

- [ ] **Step 3: Commit if any fixups needed, otherwise done**
