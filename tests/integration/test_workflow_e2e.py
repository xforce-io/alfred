"""End-to-end workflow tests.

These tests exercise the FULL stack: YAML config loading → TaskSession →
PhaseRunner → TurnOrchestrator → ScriptedAgent.

Test scenarios 1-4 use a ScriptedAgent with mocked verification_cmd.
Test scenario 5 is a **real** e2e: the agent writes actual files to disk,
and verification_cmd runs real ``python -m pytest`` — nothing mocked.

Test scenarios:
1. 3-phase linear: research → plan → implement (no verify loop)
2. 4-phase with verify loop: research → plan → implement⟷verify (pass first try)
3. 4-phase with verify retry: verify fails once, implement retries, verify passes
4. Full rollback flow: research → plan → impl⟷verify exhausted → rollback to plan → succeed
5. Real bugfix e2e: buggy calculator.py + real pytest verification, no mocks
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any, Dict, List

import pytest
import yaml

from src.everbot.core.workflow.config_loader import load_workflow_config
from src.everbot.core.workflow.exceptions import BudgetExhaustedError, CheckpointPauseError
from src.everbot.core.workflow.models import (
    CmdResult,
    TaskSessionEvent,
    TaskSessionState,
)
from src.everbot.core.workflow.session_ids import create_workflow_session_id
from src.everbot.core.workflow.task_session import TaskSession


# ---------------------------------------------------------------------------
# ScriptedAgent: per-phase scripted LLM responses via real TurnOrchestrator
# ---------------------------------------------------------------------------

class _FakeContext:
    """Minimal context that tracks variables (especially _history)."""
    def __init__(self):
        self._vars: Dict[str, Any] = {}

    def set_variable(self, key, value):
        self._vars[key] = value

    def get_var_value(self, key):
        return self._vars.get(key)


class _FakeExecutor:
    def __init__(self):
        self.context = _FakeContext()


class ScriptedAgent:
    """Agent that returns different responses based on system_prompt / message content.

    Each call to ``continue_chat`` yields Dolphin-style progress events.
    The response is determined by matching ``phase_scripts`` keys against
    the system_prompt or message text.
    """

    def __init__(self, phase_scripts: Dict[str, str]):
        """
        Args:
            phase_scripts: mapping of substring → LLM response text.
                When ``continue_chat`` is called, the system_prompt and
                message are searched for each key.  The first matching
                key's value is used as the response.
        """
        self._phase_scripts = phase_scripts
        self.executor = _FakeExecutor()
        self.name = "scripted_agent"
        self.call_log: List[Dict[str, str]] = []

    async def continue_chat(self, *, message="", system_prompt="", **kwargs):
        msg_str = str(message) if not isinstance(message, str) else message
        self.call_log.append({"message": msg_str[:200], "system_prompt": system_prompt[:200]})

        # Find matching script
        combined = f"{system_prompt} {msg_str}"
        response = "No matching script found."
        for key, text in self._phase_scripts.items():
            if key in combined:
                response = text
                break

        # Yield as Dolphin-style progress events
        yield {"_progress": [{"id": "p1", "stage": "llm", "delta": response, "status": "running"}]}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_workflow(tmp_dir: str, name: str, data: dict) -> str:
    """Write workflow YAML and return skill_dir path."""
    wf_dir = os.path.join(tmp_dir, "workflows")
    os.makedirs(wf_dir, exist_ok=True)
    with open(os.path.join(wf_dir, f"{name}.yaml"), "w") as f:
        yaml.dump(data, f)
    return tmp_dir


def _write_instruction(tmp_dir: str, ref_path: str, content: str):
    """Write an instruction file at skill_dir/ref_path."""
    full_path = os.path.join(tmp_dir, ref_path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w") as f:
        f.write(content)


async def _run_workflow(session: TaskSession) -> List[TaskSessionEvent]:
    events = []
    try:
        async for event in session.run():
            events.append(event)
    except (BudgetExhaustedError, CheckpointPauseError):
        pass
    return events


def _event_types(events: List[TaskSessionEvent]) -> List[str]:
    return [e.event_type for e in events]


# ---------------------------------------------------------------------------
# Test 1: 3-phase linear flow (research → plan → implement)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e2e_three_phase_linear():
    """Full stack: YAML → TaskSession → PhaseRunner → TurnOrchestrator → Agent.
    3 phases, no PhaseGroup, no verification.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        # Setup: workflow YAML + instruction files
        _write_workflow(tmpdir, "linear", {
            "name": "linear_task",
            "phases": [
                {"name": "research", "instruction_ref": "instructions/research.md",
                 "max_turns": 2, "max_tool_calls": 10},
                {"name": "plan", "instruction_ref": "instructions/plan.md",
                 "max_turns": 2, "max_tool_calls": 10,
                 "input_artifacts": ["research"]},
                {"name": "implement", "instruction_ref": "instructions/implement.md",
                 "max_turns": 2, "max_tool_calls": 10,
                 "input_artifacts": ["plan"]},
            ],
            "total_timeout_seconds": 60,
            "total_max_tool_calls": 100,
        })
        _write_instruction(tmpdir, "instructions/research.md", "Research the bug.")
        _write_instruction(tmpdir, "instructions/plan.md", "Plan the fix.")
        _write_instruction(tmpdir, "instructions/implement.md", "Implement the fix.")

        # Load config
        config = load_workflow_config(tmpdir, "linear")
        assert config.name == "linear_task"
        assert len(config.phases) == 3

        # Create agent with per-phase responses
        agent = ScriptedAgent({
            "Research the bug": (
                "I analyzed the codebase and found the root cause.\n"
                "<phase_artifact>\n## Research Results\n"
                "- Bug is in module X line 42\n"
                "- Caused by null pointer\n"
                "</phase_artifact>"
            ),
            "Plan the fix": (
                "Based on the research, here is my plan.\n"
                "<phase_artifact>\n## Fix Plan\n"
                "1. Add null check in module X\n"
                "2. Add unit test\n"
                "</phase_artifact>"
            ),
            "Implement the fix": (
                "I've implemented the fix as planned.\n"
                "<phase_artifact>\n## Implementation\n"
                "- Added null check in module_x.py:42\n"
                "- Added test_module_x.py\n"
                "</phase_artifact>"
            ),
        })

        # Create and run session
        state = TaskSessionState(
            session_id=create_workflow_session_id("test", "linear"),
        )
        session = TaskSession(
            config=config,
            state=state,
            agent=agent,
            agent_name="test",
            workspace_path=tmpdir,
            cancel_event=asyncio.Event(),
            skill_dir=tmpdir,
            project_dir=tmpdir,
        )

        events = await _run_workflow(session)

    # Verify workflow completed
    types = _event_types(events)
    assert types[0] == "workflow_start"
    assert "workflow_complete" in types
    assert state.status == "done"

    # Verify all 3 phases ran
    assert types.count("phase_start") == 3
    assert types.count("phase_complete") == 3

    # Verify artifacts were extracted and propagated
    assert "Research Results" in state.artifacts["research"]
    assert "Fix Plan" in state.artifacts["plan"]
    assert "Implementation" in state.artifacts["implement"]

    # Verify agent was called 3 times (one turn per phase, artifact found → exit)
    assert len(agent.call_log) == 3

    # Verify report was generated
    complete_event = next(e for e in events if e.event_type == "workflow_complete")
    assert "report" in complete_event.data
    assert "linear_task" in complete_event.data["report"]
    report_json = complete_event.data["report_json"]
    assert report_json["status"] == "done"
    assert len(report_json["phase_traces"]) == 3

    # Verify artifact injection: plan phase should have received research artifact
    plan_call = agent.call_log[1]
    assert "research" in plan_call["message"].lower() or "Research" in plan_call["message"]

    # Verify implement phase should have received plan artifact
    impl_call = agent.call_log[2]
    assert "plan" in impl_call["message"].lower() or "Plan" in impl_call["message"]


# ---------------------------------------------------------------------------
# Test 2: 4-phase with verify pass first try
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e2e_four_phase_verify_pass():
    """research → plan → implement⟷verify (verify passes on first try)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_workflow(tmpdir, "bugfix", {
            "name": "bugfix",
            "phases": [
                {"name": "research", "instruction_ref": "inst/r.md",
                 "max_turns": 1, "max_tool_calls": 10},
                {"name": "plan", "instruction_ref": "inst/p.md",
                 "max_turns": 1, "max_tool_calls": 10,
                 "input_artifacts": ["research"]},
                {"group": "impl_verify",
                 "action_phase": "implement",
                 "verify_phase": "verify",
                 "max_iterations": 3,
                 "on_exhausted": "abort",
                 "phases": [
                     {"name": "implement", "instruction_ref": "inst/i.md",
                      "max_turns": 1, "max_tool_calls": 20,
                      "input_artifacts": ["plan"]},
                     {"name": "verify",
                      "verification_cmd": {"cmd": "echo 'PASS'", "timeout_seconds": 10}},
                 ]},
            ],
            "total_timeout_seconds": 60,
            "total_max_tool_calls": 100,
        })
        _write_instruction(tmpdir, "inst/r.md", "Research phase instructions")
        _write_instruction(tmpdir, "inst/p.md", "Plan phase instructions")
        _write_instruction(tmpdir, "inst/i.md", "Implement phase instructions")

        config = load_workflow_config(tmpdir, "bugfix")

        agent = ScriptedAgent({
            "Research phase": "<phase_artifact>Found bug in auth module</phase_artifact>",
            "Plan phase": "<phase_artifact>Fix: add validation in auth.py</phase_artifact>",
            "Implement phase": "<phase_artifact>Applied fix to auth.py</phase_artifact>",
        })

        state = TaskSessionState(
            session_id=create_workflow_session_id("test", "bugfix"),
        )
        session = TaskSession(
            config=config, state=state,
            agent=agent, agent_name="test",
            workspace_path=tmpdir,
            cancel_event=asyncio.Event(),
            skill_dir=tmpdir, project_dir=tmpdir,
        )

        events = await _run_workflow(session)

    types = _event_types(events)
    assert state.status == "done"
    assert "verify_pass" in types
    assert "verify_fail" not in types

    # 3 LLM-driven phases (research, plan, implement) + verify is cmd-only
    assert len(agent.call_log) == 3

    # Report should show 3 phase traces (research, plan, impl_verify group)
    report_json = next(e for e in events if e.event_type == "workflow_complete").data["report_json"]
    assert len(report_json["phase_traces"]) == 3
    group_trace = report_json["phase_traces"][2]
    assert group_trace["phase_type"] == "phase_group"
    assert group_trace["iterations"] == 1
    assert group_trace["verify_traces"][0]["passed"] is True


# ---------------------------------------------------------------------------
# Test 3: verify fails once → retry implement → verify passes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e2e_verify_retry_then_pass():
    """implement⟷verify loop: verify fails on iteration 1, passes on iteration 2.
    Verifies that failure context is injected into the retry.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_workflow(tmpdir, "retry", {
            "name": "retry_test",
            "phases": [
                {"name": "plan", "instruction_ref": "inst/p.md",
                 "max_turns": 1, "max_tool_calls": 10},
                {"group": "impl_verify",
                 "action_phase": "implement",
                 "verify_phase": "verify",
                 "max_iterations": 3,
                 "on_exhausted": "abort",
                 "phases": [
                     {"name": "implement", "instruction_ref": "inst/i.md",
                      "max_turns": 1, "max_tool_calls": 20,
                      "input_artifacts": ["plan"]},
                     {"name": "verify",
                      "verification_cmd": {"cmd": "placeholder", "timeout_seconds": 10}},
                 ]},
            ],
            "total_timeout_seconds": 60,
            "total_max_tool_calls": 200,
        })
        _write_instruction(tmpdir, "inst/p.md", "Plan phase instructions")
        _write_instruction(tmpdir, "inst/i.md", "Implement phase instructions")

        config = load_workflow_config(tmpdir, "retry")

        # Agent: plan + 2 implement calls (first attempt + retry)
        implement_call_count = {"n": 0}

        class RetryAgent(ScriptedAgent):
            async def continue_chat(self, *, message="", system_prompt="", **kwargs):
                msg_str = str(message) if not isinstance(message, str) else message
                self.call_log.append({"message": msg_str[:500], "system_prompt": system_prompt[:200]})

                if "Plan phase" in system_prompt:
                    text = "<phase_artifact>Plan: fix the parser</phase_artifact>"
                else:
                    implement_call_count["n"] += 1
                    if implement_call_count["n"] == 1:
                        text = "<phase_artifact>First attempt: partial fix</phase_artifact>"
                    else:
                        text = "<phase_artifact>Second attempt: complete fix</phase_artifact>"
                yield {"_progress": [{"id": "p1", "stage": "llm", "delta": text, "status": "running"}]}

        agent = RetryAgent({})

        # Verification: fail first, pass second
        verify_call_count = {"n": 0}

        async def mock_verify_cmd(*args, **kwargs):
            verify_call_count["n"] += 1
            if verify_call_count["n"] == 1:
                return CmdResult(exit_code=1, output="FAILED: test_parser assertion error")
            return CmdResult(exit_code=0, output="All tests passed")

        state = TaskSessionState(
            session_id=create_workflow_session_id("test", "retry"),
        )
        session = TaskSession(
            config=config, state=state,
            agent=agent, agent_name="test",
            workspace_path=tmpdir,
            cancel_event=asyncio.Event(),
            skill_dir=tmpdir, project_dir=tmpdir,
        )

        from unittest.mock import patch
        with patch(
            "src.everbot.core.workflow.task_session.run_verification_cmd",
            side_effect=mock_verify_cmd,
        ):
            events = await _run_workflow(session)

    types = _event_types(events)
    assert state.status == "done"
    assert types.count("verify_fail") == 1
    assert types.count("verify_pass") == 1

    # Agent should have been called 3 times: plan + implement x2
    assert len(agent.call_log) == 3

    # The retry implement call should have received failure context
    retry_call = agent.call_log[2]  # 3rd call = second implement
    assert "重试上下文" in retry_call["message"] or "FAILED" in retry_call["message"]

    # Final artifact should be from the successful attempt
    assert "complete fix" in state.artifacts["implement"]

    # Report should show 2 verify traces
    report = next(e for e in events if e.event_type == "workflow_complete").data["report_json"]
    group_trace = report["phase_traces"][1]  # plan=0, group=1
    assert len(group_trace["verify_traces"]) == 2
    assert group_trace["verify_traces"][0]["passed"] is False
    assert group_trace["verify_traces"][1]["passed"] is True


# ---------------------------------------------------------------------------
# Test 4: Full rollback flow
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e2e_full_rollback_flow():
    """research → plan → impl⟷verify exhausted → rollback to plan →
    new plan → impl⟷verify succeeds.

    This tests the outer loop: when the inner implement⟷verify loop
    cannot converge, the workflow rolls back to re-plan.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_workflow(tmpdir, "rollback", {
            "name": "rollback_test",
            "phases": [
                {"name": "research", "instruction_ref": "inst/r.md",
                 "max_turns": 1, "max_tool_calls": 10},
                {"name": "plan", "instruction_ref": "inst/p.md",
                 "max_turns": 1, "max_tool_calls": 10,
                 "input_artifacts": ["research"]},
                {"group": "impl_verify",
                 "action_phase": "implement",
                 "verify_phase": "verify",
                 "max_iterations": 2,
                 "on_exhausted": "rollback",
                 "rollback_target": "plan",
                 "phases": [
                     {"name": "implement", "instruction_ref": "inst/i.md",
                      "max_turns": 1, "max_tool_calls": 20,
                      "input_artifacts": ["plan"]},
                     {"name": "verify",
                      "verification_cmd": {"cmd": "placeholder", "timeout_seconds": 10}},
                 ]},
            ],
            "total_timeout_seconds": 120,
            "total_max_tool_calls": 500,
            "max_rollback_retries": 2,
        })
        _write_instruction(tmpdir, "inst/r.md", "Research instructions")
        _write_instruction(tmpdir, "inst/p.md", "Plan instructions")
        _write_instruction(tmpdir, "inst/i.md", "Implement instructions")

        config = load_workflow_config(tmpdir, "rollback")

        # Track calls to understand the full execution sequence
        call_sequence: List[str] = []
        plan_count = {"n": 0}

        class RollbackAgent(ScriptedAgent):
            async def continue_chat(self, *, message="", system_prompt="", **kwargs):
                msg_str = str(message) if not isinstance(message, str) else message
                self.call_log.append({"message": msg_str[:500], "system_prompt": system_prompt[:200]})

                if "Research" in system_prompt:
                    call_sequence.append("research")
                    text = "<phase_artifact>Bug found in module Y</phase_artifact>"
                elif "Plan" in system_prompt:
                    plan_count["n"] += 1
                    call_sequence.append(f"plan_{plan_count['n']}")
                    if plan_count["n"] == 1:
                        text = "<phase_artifact>Plan A: approach via method X</phase_artifact>"
                    else:
                        text = "<phase_artifact>Plan B: approach via method Z (revised)</phase_artifact>"
                else:
                    call_sequence.append("implement")
                    text = "<phase_artifact>Implementation done</phase_artifact>"
                yield {"_progress": [{"id": "p1", "stage": "llm", "delta": text, "status": "running"}]}

        agent = RollbackAgent({})

        # Verification: first 2 iterations fail (exhaust group → rollback),
        # after rollback and re-plan, 1st iteration passes
        verify_count = {"n": 0}

        async def mock_verify(*args, **kwargs):
            verify_count["n"] += 1
            if verify_count["n"] <= 2:
                return CmdResult(exit_code=1, output=f"FAILED iteration {verify_count['n']}")
            return CmdResult(exit_code=0, output="All tests passed after re-plan")

        state = TaskSessionState(
            session_id=create_workflow_session_id("test", "rollback"),
        )
        session = TaskSession(
            config=config, state=state,
            agent=agent, agent_name="test",
            workspace_path=tmpdir,
            cancel_event=asyncio.Event(),
            skill_dir=tmpdir, project_dir=tmpdir,
        )

        from unittest.mock import patch
        with patch(
            "src.everbot.core.workflow.task_session.run_verification_cmd",
            side_effect=mock_verify,
        ):
            events = await _run_workflow(session)

    types = _event_types(events)

    # Verify final status
    assert state.status == "done"

    # Verify the execution sequence:
    # research → plan_1 → implement → verify(fail) → implement → verify(fail)
    # → rollback → plan_2 → implement → verify(pass)
    assert call_sequence == [
        "research", "plan_1",
        "implement", "implement",      # 2 iterations of first group run
        "plan_2",                       # re-plan after rollback
        "implement",                    # new implementation
    ]

    # Verify event types
    assert "rollback" in types
    assert types.count("verify_fail") == 2
    assert types.count("verify_pass") == 1
    assert state.rollback_retry_count == 1

    # Verify retry context was injected into re-plan
    assert "__retry_context" in state.artifacts

    # Verify plan was updated to Plan B after rollback
    assert "Plan B" in state.artifacts["plan"] or "revised" in state.artifacts["plan"]

    # Report should show the full trace
    report = next(e for e in events if e.event_type == "workflow_complete").data["report_json"]
    assert report["status"] == "done"
    # research + plan + impl_verify(exhausted) + plan(re-run) + impl_verify(succeeded)
    assert len(report["phase_traces"]) >= 3


# ---------------------------------------------------------------------------
# Test 5: REAL e2e — buggy project + real pytest verification, no mocks
# ---------------------------------------------------------------------------

# The buggy project: calculator.py has a wrong implementation of `add`.
# test_calculator.py tests it. The agent must write the correct fix.

_BUGGY_CALCULATOR = '''\
"""A simple calculator module — with a bug."""


def add(a, b):
    return a - b  # BUG: should be a + b


def multiply(a, b):
    return a * b
'''

_CALCULATOR_TESTS = '''\
"""Tests for calculator module."""

import sys
import os

# Add project root to path so calculator can be imported
sys.path.insert(0, os.path.dirname(__file__))

from calculator import add, multiply


def test_add_positive():
    assert add(2, 3) == 5


def test_add_negative():
    assert add(-1, -2) == -3


def test_add_zero():
    assert add(0, 0) == 0


def test_multiply():
    """This test already passes — only add is broken."""
    assert multiply(3, 4) == 12
'''

_FIXED_CALCULATOR = '''\
"""A simple calculator module."""


def add(a, b):
    return a + b


def multiply(a, b):
    return a * b
'''

# A wrong fix: swaps the operation to multiplication instead of addition.
_WRONG_FIX_CALCULATOR = '''\
"""A simple calculator module — wrong fix attempt."""


def add(a, b):
    return a * b  # WRONG: should be a + b, not a * b


def multiply(a, b):
    return a * b
'''


class _RealBugfixAgent:
    """Agent that actually writes files to disk during implement phase.

    - research: reads the buggy file (simulated), outputs analysis
    - plan: outputs a fix plan
    - implement (attempt 1): writes a WRONG fix → real pytest will fail
    - implement (attempt 2): writes the CORRECT fix → real pytest will pass
    """

    def __init__(self, project_dir: str):
        self._project_dir = project_dir
        self._implement_count = 0
        self.executor = _FakeExecutor()
        self.name = "bugfix_agent"
        self.call_log: List[Dict[str, str]] = []

    async def continue_chat(self, *, message="", system_prompt="", **kwargs):
        msg_str = str(message) if not isinstance(message, str) else message
        self.call_log.append({
            "message": msg_str[:300],
            "system_prompt": system_prompt[:300],
        })

        if "Analyze the bug" in system_prompt:
            # Research phase: analyze the buggy code
            text = (
                "I've analyzed calculator.py. The `add` function uses "
                "subtraction (`a - b`) instead of addition (`a + b`).\n"
                "<phase_artifact>\n"
                "## Bug Analysis\n"
                "- File: calculator.py\n"
                "- Function: add(a, b)\n"
                "- Bug: returns `a - b` instead of `a + b`\n"
                "- Root cause: operator typo\n"
                "</phase_artifact>"
            )
        elif "Write a fix plan" in system_prompt:
            # Plan phase
            text = (
                "<phase_artifact>\n"
                "## Fix Plan\n"
                "1. Change `return a - b` to `return a + b` in calculator.py:5\n"
                "2. Verify with existing test_calculator.py\n"
                "</phase_artifact>"
            )
        else:
            # Implement phase: actually write files
            self._implement_count += 1
            calc_path = os.path.join(self._project_dir, "calculator.py")

            if self._implement_count == 1:
                # First attempt: write a WRONG fix
                with open(calc_path, "w") as f:
                    f.write(_WRONG_FIX_CALCULATOR)
                text = (
                    "I've fixed calculator.py by changing the operator.\n"
                    "<phase_artifact>\n"
                    "## Implementation (attempt 1)\n"
                    "Changed `a - b` to `a * b` in add()\n"
                    "</phase_artifact>"
                )
            else:
                # Second attempt: write the CORRECT fix
                with open(calc_path, "w") as f:
                    f.write(_FIXED_CALCULATOR)
                text = (
                    "I've corrected calculator.py with the right operator.\n"
                    "<phase_artifact>\n"
                    "## Implementation (attempt 2)\n"
                    "Changed `a - b` to `a + b` in add()\n"
                    "</phase_artifact>"
                )

        yield {"_progress": [
            {"id": "p1", "stage": "llm", "delta": text, "status": "running"},
        ]}


@pytest.mark.asyncio
async def test_e2e_real_bugfix_no_mocks():
    """REAL end-to-end: buggy Python module + real pytest + real file writes.

    Scenario:
      1. research: agent analyzes the buggy calculator.py
      2. plan: agent produces a fix plan
      3. implement (attempt 1): agent writes WRONG fix → real pytest FAILS
      4. implement (attempt 2): agent writes CORRECT fix → real pytest PASSES

    Nothing is mocked — verification_cmd runs actual `python -m pytest`,
    the agent writes actual files, pytest asserts real Python semantics.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        # --- Set up the buggy project ---
        project_dir = os.path.join(tmpdir, "project")
        os.makedirs(project_dir)

        with open(os.path.join(project_dir, "calculator.py"), "w") as f:
            f.write(_BUGGY_CALCULATOR)
        with open(os.path.join(project_dir, "test_calculator.py"), "w") as f:
            f.write(_CALCULATOR_TESTS)

        # Sanity check: tests should FAIL on the buggy code
        import subprocess
        pre_result = subprocess.run(
            ["python", "-m", "pytest", "test_calculator.py", "-v"],
            capture_output=True, text=True, cwd=project_dir,
        )
        assert pre_result.returncode != 0, "Buggy code should fail tests"
        assert "FAILED" in pre_result.stdout

        # --- Set up skill dir with workflow YAML + instructions ---
        skill_dir = os.path.join(tmpdir, "skill")
        _write_workflow(skill_dir, "real_bugfix", {
            "name": "real_bugfix",
            "phases": [
                {"name": "research", "instruction_ref": "inst/research.md",
                 "max_turns": 1, "max_tool_calls": 10},
                {"name": "plan", "instruction_ref": "inst/plan.md",
                 "max_turns": 1, "max_tool_calls": 10,
                 "input_artifacts": ["research"]},
                {"group": "impl_verify",
                 "action_phase": "implement",
                 "verify_phase": "verify",
                 "max_iterations": 3,
                 "on_exhausted": "abort",
                 "phases": [
                     {"name": "implement", "instruction_ref": "inst/implement.md",
                      "max_turns": 1, "max_tool_calls": 20,
                      "input_artifacts": ["plan"]},
                     {"name": "verify",
                      "verification_cmd": {
                          "cmd": "python -m pytest test_calculator.py -v",
                          "timeout_seconds": 30,
                      }},
                 ]},
            ],
            "total_timeout_seconds": 120,
            "total_max_tool_calls": 200,
        })
        _write_instruction(skill_dir, "inst/research.md", "Analyze the bug in calculator.py.")
        _write_instruction(skill_dir, "inst/plan.md", "Write a fix plan for the bug.")
        _write_instruction(skill_dir, "inst/implement.md",
                           "Implement the fix according to the plan.")

        # --- Load config and create session ---
        config = load_workflow_config(skill_dir, "real_bugfix")

        agent = _RealBugfixAgent(project_dir)

        state = TaskSessionState(
            session_id=create_workflow_session_id("test", "real_bugfix"),
        )
        session = TaskSession(
            config=config, state=state,
            agent=agent, agent_name="test",
            workspace_path=project_dir,
            cancel_event=asyncio.Event(),
            skill_dir=skill_dir,
            project_dir=project_dir,
        )

        # --- Run the workflow — NO MOCKS ---
        events = await _run_workflow(session)

        # === Assertions (inside tmpdir context so files still exist) ===
        types = _event_types(events)

        # Workflow should complete successfully
        assert state.status == "done", f"Expected done, got {state.status}"
        assert "workflow_complete" in types

        # Should have had exactly 1 verify failure (wrong fix) then 1 verify pass (correct fix)
        assert types.count("verify_fail") == 1, (
            f"Expected 1 verify_fail, got {types.count('verify_fail')}"
        )
        assert types.count("verify_pass") == 1, (
            f"Expected 1 verify_pass, got {types.count('verify_pass')}"
        )

        # Agent should have been called 4 times: research + plan + implement×2
        assert len(agent.call_log) == 4

        # The implement phase ran twice (wrong fix → correct fix)
        assert agent._implement_count == 2

        # Artifacts should be populated from each phase
        assert "Bug Analysis" in state.artifacts["research"]
        assert "Fix Plan" in state.artifacts["plan"]
        assert "attempt 2" in state.artifacts["implement"]

        # Verify artifact should contain real pytest output
        verify_artifact = state.artifacts.get("verify", "")
        assert "passed" in verify_artifact.lower()

        # The buggy file should now be FIXED on disk
        with open(os.path.join(project_dir, "calculator.py")) as f:
            final_code = f.read()
        assert "a + b" in final_code
        assert "a - b" not in final_code

        # Final sanity: run pytest one more time independently
        post_result = subprocess.run(
            ["python", "-m", "pytest", "test_calculator.py", "-v"],
            capture_output=True, text=True, cwd=project_dir,
        )
        assert post_result.returncode == 0, (
            f"Final pytest should pass:\n{post_result.stdout}"
        )

        # Report should capture the full flow
        report = next(
            e for e in events if e.event_type == "workflow_complete"
        ).data["report_json"]
        assert report["status"] == "done"
        group_trace = report["phase_traces"][2]  # research=0, plan=1, group=2
        assert group_trace["phase_type"] == "phase_group"
        assert len(group_trace["verify_traces"]) == 2
        assert group_trace["verify_traces"][0]["passed"] is False
        assert group_trace["verify_traces"][0]["exit_code"] == 1
        assert group_trace["verify_traces"][1]["passed"] is True
        assert group_trace["verify_traces"][1]["exit_code"] == 0
