"""Integration tests for TaskSession workflow engine.

Mocks TurnOrchestrator (via PhaseRunner) and subprocess to test:
- Linear phase flow: research → plan → implement → verify
- Verify failure → retry implement with failure context
- PhaseGroup exhaustion → rollback to plan with retry context
- Rollback exhaustion → workflow fails
- Total budget exhaustion → workflow fails
- Report generated with correct phase traces
- Checkpoint pauses workflow
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.everbot.core.workflow.exceptions import (
    BudgetExhaustedError,
    CheckpointPauseError,
)
from src.everbot.core.workflow.models import (
    CmdResult,
    PhaseConfig,
    PhaseGroupConfig,
    PhaseResult,
    TaskSessionConfig,
    TaskSessionEvent,
    TaskSessionState,
    VerificationCmdConfig,
)
from src.everbot.core.workflow.task_session import TaskSession


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeContext:
    def __init__(self):
        self._vars: Dict[str, Any] = {}

    def set_variable(self, key, value):
        self._vars[key] = value

    def get_var_value(self, key):
        return self._vars.get(key)


class _FakeExecutor:
    def __init__(self):
        self.context = _FakeContext()


class _FakeAgent:
    """Minimal agent stub — PhaseRunner is fully mocked so agent is not used."""
    def __init__(self):
        self.executor = _FakeExecutor()
        self.name = "test_agent"


def _make_session(
    config: TaskSessionConfig,
    state: Optional[TaskSessionState] = None,
) -> TaskSession:
    state = state or TaskSessionState(session_id="wf_test_123")
    return TaskSession(
        config=config,
        state=state,
        agent=_FakeAgent(),
        agent_name="test_agent",
        workspace_path="/tmp/workspace",
        cancel_event=asyncio.Event(),
        skill_dir="/tmp/skill",
        project_dir="/tmp/project",
    )


async def _collect_events(session: TaskSession) -> List[TaskSessionEvent]:
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
# Test: Linear phase flow
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_linear_phase_flow():
    """research → plan flow without PhaseGroup."""
    config = TaskSessionConfig(
        name="simple",
        phases=[
            PhaseConfig(name="research", instruction_ref="sop.md"),
            PhaseConfig(name="plan", instruction_ref="sop.md"),
        ],
    )
    session = _make_session(config)

    # Mock phase_runner to return canned results
    async def mock_run_phase(phase_config, *, state, artifacts, **kwargs):
        return PhaseResult(
            artifact=f"artifact from {phase_config.name}",
            tool_calls_used=5,
            duration_seconds=10.0,
        )

    session._phase_runner.run_phase = mock_run_phase

    events = await _collect_events(session)
    types = _event_types(events)

    assert "workflow_start" in types
    assert "workflow_complete" in types
    assert types.count("phase_start") == 2
    assert types.count("phase_complete") == 2
    assert session.state.status == "done"
    assert session.state.artifacts["research"] == "artifact from research"
    assert session.state.artifacts["plan"] == "artifact from plan"


# ---------------------------------------------------------------------------
# Test: PhaseGroup with verify pass on first try
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_phase_group_verify_pass_first_try():
    """PhaseGroup passes verification on first iteration."""
    config = TaskSessionConfig(
        name="test",
        phases=[
            PhaseGroupConfig(
                name="impl_verify",
                action_phase="implement",
                verify_phase="verify",
                max_iterations=3,
                on_exhausted="abort",
                phases=[
                    PhaseConfig(name="implement", instruction_ref="sop.md"),
                    PhaseConfig(
                        name="verify",
                        verification_cmd=VerificationCmdConfig(cmd="pytest"),
                    ),
                ],
            ),
        ],
    )
    session = _make_session(config)

    async def mock_run_phase(phase_config, *, state, artifacts, **kwargs):
        return PhaseResult(artifact=f"done by {phase_config.name}", tool_calls_used=10)

    session._phase_runner.run_phase = mock_run_phase

    with patch(
        "src.everbot.core.workflow.task_session.run_verification_cmd",
        return_value=CmdResult(exit_code=0, output="all passed"),
    ):
        events = await _collect_events(session)

    types = _event_types(events)
    assert "verify_pass" in types
    assert "verify_fail" not in types
    assert session.state.status == "done"


# ---------------------------------------------------------------------------
# Test: PhaseGroup verify fails then passes on retry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_phase_group_retry_then_pass():
    """Verify fails on iteration 1, passes on iteration 2."""
    config = TaskSessionConfig(
        name="test",
        phases=[
            PhaseGroupConfig(
                name="impl_verify",
                action_phase="implement",
                verify_phase="verify",
                max_iterations=5,
                on_exhausted="abort",
                phases=[
                    PhaseConfig(name="implement", instruction_ref="sop.md"),
                    PhaseConfig(
                        name="verify",
                        verification_cmd=VerificationCmdConfig(cmd="pytest"),
                    ),
                ],
            ),
        ],
    )
    session = _make_session(config)

    call_count = {"n": 0}

    async def mock_run_phase(phase_config, *, state, artifacts, **kwargs):
        call_count["n"] += 1
        return PhaseResult(artifact=f"attempt {call_count['n']}", tool_calls_used=5)

    session._phase_runner.run_phase = mock_run_phase

    verify_results = iter([
        CmdResult(exit_code=1, output="test_x FAILED"),
        CmdResult(exit_code=0, output="all passed"),
    ])

    async def mock_verify(*args, **kwargs):
        return next(verify_results)

    with patch(
        "src.everbot.core.workflow.task_session.run_verification_cmd",
        side_effect=mock_verify,
    ):
        events = await _collect_events(session)

    types = _event_types(events)
    assert types.count("verify_fail") == 1
    assert types.count("verify_pass") == 1
    assert session.state.status == "done"


# ---------------------------------------------------------------------------
# Test: PhaseGroup exhaustion → rollback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_phase_group_exhausted_rollback():
    """PhaseGroup exhausts max_iterations → rollback to plan → succeed."""
    config = TaskSessionConfig(
        name="test",
        phases=[
            PhaseConfig(name="plan", instruction_ref="sop.md"),
            PhaseGroupConfig(
                name="impl_verify",
                action_phase="implement",
                verify_phase="verify",
                max_iterations=2,
                on_exhausted="rollback",
                rollback_target="plan",
                phases=[
                    PhaseConfig(name="implement", instruction_ref="sop.md"),
                    PhaseConfig(
                        name="verify",
                        verification_cmd=VerificationCmdConfig(cmd="pytest"),
                    ),
                ],
            ),
        ],
        max_rollback_retries=2,
    )
    session = _make_session(config)

    phase_calls: List[str] = []

    async def mock_run_phase(phase_config, *, state, artifacts, **kwargs):
        phase_calls.append(phase_config.name)
        return PhaseResult(artifact=f"{phase_config.name} result", tool_calls_used=3)

    session._phase_runner.run_phase = mock_run_phase

    # First pass: 2 iterations fail → rollback
    # Second pass (after rollback): 1st iteration passes
    verify_call_count = {"n": 0}

    async def mock_verify(*args, **kwargs):
        verify_call_count["n"] += 1
        if verify_call_count["n"] <= 2:
            return CmdResult(exit_code=1, output="FAILED")
        return CmdResult(exit_code=0, output="PASSED")

    with patch(
        "src.everbot.core.workflow.task_session.run_verification_cmd",
        side_effect=mock_verify,
    ):
        events = await _collect_events(session)

    types = _event_types(events)
    assert "rollback" in types
    assert "verify_pass" in types
    assert session.state.status == "done"
    assert session.state.rollback_retry_count == 1

    # plan should be called twice (initial + after rollback)
    assert phase_calls.count("plan") == 2

    # Retry context should be injected
    assert "__retry_context" in session.state.artifacts


# ---------------------------------------------------------------------------
# Test: Rollback exhaustion → workflow fails
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rollback_exhaustion_fails():
    """Rollback count exceeds max_rollback_retries → workflow fails."""
    config = TaskSessionConfig(
        name="test",
        phases=[
            PhaseConfig(name="plan", instruction_ref="sop.md"),
            PhaseGroupConfig(
                name="impl_verify",
                action_phase="implement",
                verify_phase="verify",
                max_iterations=1,  # exhaust immediately
                on_exhausted="rollback",
                rollback_target="plan",
                phases=[
                    PhaseConfig(name="implement", instruction_ref="sop.md"),
                    PhaseConfig(
                        name="verify",
                        verification_cmd=VerificationCmdConfig(cmd="pytest"),
                    ),
                ],
            ),
        ],
        max_rollback_retries=1,
    )
    session = _make_session(config)

    async def mock_run_phase(phase_config, *, state, artifacts, **kwargs):
        return PhaseResult(artifact="result", tool_calls_used=2)

    session._phase_runner.run_phase = mock_run_phase

    with patch(
        "src.everbot.core.workflow.task_session.run_verification_cmd",
        return_value=CmdResult(exit_code=1, output="always fails"),
    ):
        events = await _collect_events(session)

    types = _event_types(events)
    assert "workflow_failed" in types
    assert session.state.status == "failed"
    # Should have rolled back once, then failed on second exhaustion
    assert session.state.rollback_retry_count == 2


# ---------------------------------------------------------------------------
# Test: PhaseGroup on_exhausted=abort
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_phase_group_abort_on_exhausted():
    config = TaskSessionConfig(
        name="test",
        phases=[
            PhaseGroupConfig(
                name="g1",
                action_phase="a",
                verify_phase="v",
                max_iterations=1,
                on_exhausted="abort",
                phases=[
                    PhaseConfig(name="a", instruction_ref="sop.md"),
                    PhaseConfig(name="v", verification_cmd=VerificationCmdConfig(cmd="test")),
                ],
            ),
        ],
    )
    session = _make_session(config)

    async def mock_run_phase(phase_config, *, state, artifacts, **kwargs):
        return PhaseResult(artifact="x", tool_calls_used=1)

    session._phase_runner.run_phase = mock_run_phase

    with patch(
        "src.everbot.core.workflow.task_session.run_verification_cmd",
        return_value=CmdResult(exit_code=1, output="fail"),
    ):
        events = await _collect_events(session)

    assert session.state.status == "failed"


# ---------------------------------------------------------------------------
# Test: Total tool budget exhaustion
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_total_tool_budget_exhaustion():
    config = TaskSessionConfig(
        name="test",
        total_max_tool_calls=10,
        phases=[
            PhaseConfig(name="p1", instruction_ref="sop.md"),
            PhaseConfig(name="p2", instruction_ref="sop.md"),
        ],
    )
    session = _make_session(config)

    async def mock_run_phase(phase_config, *, state, artifacts, **kwargs):
        state.total_tool_calls_used += 15  # exceed budget
        return PhaseResult(artifact="done", tool_calls_used=15)

    session._phase_runner.run_phase = mock_run_phase

    events = await _collect_events(session)
    assert session.state.status == "failed"
    assert "workflow_failed" in _event_types(events)


# ---------------------------------------------------------------------------
# Test: Total timeout exhaustion
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_total_timeout_exhaustion():
    config = TaskSessionConfig(
        name="test",
        total_timeout_seconds=1,  # 1 second
        phases=[
            PhaseConfig(name="p1", instruction_ref="sop.md"),
            PhaseConfig(name="p2", instruction_ref="sop.md"),
        ],
    )
    session = _make_session(config)

    async def mock_run_phase(phase_config, *, state, artifacts, **kwargs):
        # After first phase, set start_time far in the past to trigger timeout
        state.start_time = datetime(2020, 1, 1)
        return PhaseResult(artifact="done", tool_calls_used=1)

    session._phase_runner.run_phase = mock_run_phase

    events = await _collect_events(session)
    # Timeout check happens at loop top before phase 2
    assert session.state.status == "failed"


# ---------------------------------------------------------------------------
# Test: Checkpoint pauses workflow
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_checkpoint_pauses_workflow():
    config = TaskSessionConfig(
        name="test",
        phases=[
            PhaseConfig(name="plan", instruction_ref="sop.md", checkpoint=True),
            PhaseConfig(name="implement", instruction_ref="sop.md"),
        ],
    )
    session = _make_session(config)

    async def mock_run_phase(phase_config, *, state, artifacts, **kwargs):
        return PhaseResult(artifact="my plan", tool_calls_used=3)

    session._phase_runner.run_phase = mock_run_phase

    with patch.object(session, "_persist_state"):
        events = await _collect_events(session)

    types = _event_types(events)
    assert "checkpoint" in types
    assert session.state.status == "paused"
    # implement should NOT have run
    assert "implement" not in session.state.artifacts


# ---------------------------------------------------------------------------
# Test: Cancellation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_event_stops_workflow():
    config = TaskSessionConfig(
        name="test",
        phases=[
            PhaseConfig(name="p1", instruction_ref="sop.md"),
            PhaseConfig(name="p2", instruction_ref="sop.md"),
        ],
    )
    session = _make_session(config)
    session._cancel_event.set()  # Pre-cancel

    async def mock_run_phase(phase_config, *, state, artifacts, **kwargs):
        return PhaseResult(artifact="done", tool_calls_used=1)

    session._phase_runner.run_phase = mock_run_phase

    events = await _collect_events(session)
    assert session.state.status == "cancelled"
    assert "workflow_cancelled" in _event_types(events)


# ---------------------------------------------------------------------------
# Test: LLM-mode verify with structured_tag
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_llm_verify_structured_tag():
    config = TaskSessionConfig(
        name="test",
        phases=[
            PhaseGroupConfig(
                name="g1",
                action_phase="impl",
                verify_phase="check",
                max_iterations=2,
                on_exhausted="abort",
                phases=[
                    PhaseConfig(name="impl", instruction_ref="sop.md"),
                    PhaseConfig(
                        name="check",
                        instruction_ref="check.md",
                        verify_protocol="structured_tag",
                    ),
                ],
            ),
        ],
    )
    session = _make_session(config)

    call_count = {"n": 0}

    async def mock_run_phase(phase_config, *, state, artifacts, **kwargs):
        call_count["n"] += 1
        if phase_config.name == "check":
            if call_count["n"] <= 2:
                return PhaseResult(artifact="<verify_result>FAIL: test broken</verify_result>")
            return PhaseResult(artifact="<verify_result>PASS</verify_result>")
        return PhaseResult(artifact="code written", tool_calls_used=5)

    session._phase_runner.run_phase = mock_run_phase

    events = await _collect_events(session)
    types = _event_types(events)
    assert "verify_fail" in types
    assert "verify_pass" in types
    assert session.state.status == "done"


# ---------------------------------------------------------------------------
# Test: Setup phase runs once per group entry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_setup_phase_runs_once():
    config = TaskSessionConfig(
        name="test",
        phases=[
            PhaseGroupConfig(
                name="g1",
                action_phase="impl",
                verify_phase="verify",
                setup_phase="setup",
                max_iterations=2,
                on_exhausted="abort",
                phases=[
                    PhaseConfig(name="setup", instruction_ref="setup.md"),
                    PhaseConfig(name="impl", instruction_ref="sop.md"),
                    PhaseConfig(
                        name="verify",
                        verification_cmd=VerificationCmdConfig(cmd="pytest"),
                    ),
                ],
            ),
        ],
    )
    session = _make_session(config)

    phase_calls: List[str] = []

    async def mock_run_phase(phase_config, *, state, artifacts, **kwargs):
        phase_calls.append(phase_config.name)
        return PhaseResult(artifact=f"{phase_config.name} done", tool_calls_used=2)

    session._phase_runner.run_phase = mock_run_phase

    with patch(
        "src.everbot.core.workflow.task_session.run_verification_cmd",
        return_value=CmdResult(exit_code=0, output="ok"),
    ):
        events = await _collect_events(session)

    # Setup should run exactly once, before the first iteration
    assert phase_calls.count("setup") == 1
    assert phase_calls[0] == "setup"
    assert session.state.status == "done"


# ---------------------------------------------------------------------------
# Test: Report is generated on completion
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_report_generated():
    config = TaskSessionConfig(
        name="bugfix",
        phases=[
            PhaseConfig(name="research", instruction_ref="sop.md"),
        ],
    )
    session = _make_session(config)

    async def mock_run_phase(phase_config, *, state, artifacts, **kwargs):
        return PhaseResult(artifact="found the bug", tool_calls_used=5, duration_seconds=10.0)

    session._phase_runner.run_phase = mock_run_phase

    events = await _collect_events(session)
    complete_event = next(e for e in events if e.event_type == "workflow_complete")
    assert "report" in complete_event.data
    assert "report_json" in complete_event.data
    assert "bugfix" in complete_event.data["report"]
    assert complete_event.data["report_json"]["status"] == "done"


# ---------------------------------------------------------------------------
# Test: PhaseGroup on_exhausted=checkpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_phase_group_checkpoint_on_exhausted():
    config = TaskSessionConfig(
        name="test",
        phases=[
            PhaseGroupConfig(
                name="g1",
                action_phase="a",
                verify_phase="v",
                max_iterations=1,
                on_exhausted="checkpoint",
                phases=[
                    PhaseConfig(name="a", instruction_ref="sop.md"),
                    PhaseConfig(name="v", verification_cmd=VerificationCmdConfig(cmd="test")),
                ],
            ),
        ],
    )
    session = _make_session(config)

    async def mock_run_phase(phase_config, *, state, artifacts, **kwargs):
        return PhaseResult(artifact="x", tool_calls_used=1)

    session._phase_runner.run_phase = mock_run_phase

    with patch(
        "src.everbot.core.workflow.task_session.run_verification_cmd",
        return_value=CmdResult(exit_code=1, output="fail"),
    ):
        with patch.object(session, "_persist_state"):
            events = await _collect_events(session)

    assert session.state.status == "paused"
    assert "checkpoint" in _event_types(events)
