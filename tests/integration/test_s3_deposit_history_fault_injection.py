"""S3 fault-injection test: deposit/history failures must fail the task.

Tests that when deposit_job_event or inject_to_history returns False,
the task is marked FAILED (not DONE) and will retry.
"""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_deposit_job_event_false_fails_task():
    """S3: deposit_job_event returning False must raise RuntimeError."""
    from everbot.core.runtime.cron import TaskRunner
    from everbot.core.runtime.task import Task
    
    # Mock delivery
    mock_delivery = MagicMock()
    mock_delivery.deposit_job_event = AsyncMock(return_value=False)  # Force failure
    mock_delivery.inject_to_history = AsyncMock(return_value=True)
    mock_delivery._emit_realtime = AsyncMock()
    
    # Mock agent provider
    mock_provider = MagicMock()
    mock_provider.run_turn = AsyncMock(return_value="test result")
    
    # Mock agent
    mock_agent = MagicMock()
    mock_agent.name = "test_agent"
    
    # Create task runner
    runner = TaskRunner(
        agent_loader=MagicMock(),
        skill_loader=MagicMock(),
        delivery=mock_delivery,
        workspace=Path("/tmp/test_workspace"),
    )
    
    # Stub _create_job_agent to return our mock
    async def _mock_create_job_agent(session_id):
        return mock_agent
    runner._create_job_agent = _mock_create_job_agent
    
    # Stub _build_job_system_prompt
    runner._build_job_system_prompt = MagicMock(return_value="test prompt")
    
    # Create a simple non-isolated task
    task = Task(
        id="test_task",
        schedule="* * * * *",
        type="message",
        agent="test_agent",
        content="test content",
    )
    
    # Run task and expect RuntimeError from deposit failure
    with pytest.raises(RuntimeError, match="Failed to deposit job completion event to mailbox"):
        await runner._run_simple_message(task, run_id="test_run_123", run_agent=mock_provider.run_turn)
    
    # Verify deposit was called but returned False
    mock_delivery.deposit_job_event.assert_called()
    # inject_to_history should NOT be called (task failed before that)
    mock_delivery.inject_to_history.assert_not_called()


@pytest.mark.asyncio
async def test_inject_to_history_false_fails_task():
    """S3: inject_to_history returning False must raise RuntimeError."""
    from everbot.core.runtime.cron import TaskRunner
    from everbot.core.runtime.task import Task
    
    # Mock delivery
    mock_delivery = MagicMock()
    mock_delivery.deposit_job_event = AsyncMock(return_value=True)  # Succeed
    mock_delivery.inject_to_history = AsyncMock(return_value=False)  # Force failure
    mock_delivery._emit_realtime = AsyncMock()
    
    # Mock agent provider
    mock_provider = MagicMock()
    mock_provider.run_turn = AsyncMock(return_value="test result")
    
    # Mock agent
    mock_agent = MagicMock()
    mock_agent.name = "test_agent"
    
    # Create task runner
    runner = TaskRunner(
        agent_loader=MagicMock(),
        skill_loader=MagicMock(),
        delivery=mock_delivery,
        workspace=Path("/tmp/test_workspace"),
    )
    
    # Stub _create_job_agent
    async def _mock_create_job_agent(session_id):
        return mock_agent
    runner._create_job_agent = _mock_create_job_agent
    
    # Stub _build_job_system_prompt
    runner._build_job_system_prompt = MagicMock(return_value="test prompt")
    
    # Create task
    task = Task(
        id="test_task",
        schedule="* * * * *",
        type="message",
        agent="test_agent",
        content="test content",
    )
    
    # Run task and expect RuntimeError from history inject failure
    with pytest.raises(RuntimeError, match="Failed to inject job result to history"):
        await runner._run_simple_message(task, run_id="test_run_123", run_agent=mock_provider.run_turn)
    
    # Verify both deposit and inject were called
    mock_delivery.deposit_job_event.assert_called()
    mock_delivery.inject_to_history.assert_called()
    # _emit_realtime should NOT be called (task failed before that)
    mock_delivery._emit_realtime.assert_not_called()


@pytest.mark.asyncio
async def test_staged_history_step_false_fails_task():
    """S3: staged history delivery step returning False must fail via run_delivery_step."""
    from everbot.core.runtime.routine_checkpoint import RoutineCheckpointStore
    
    # Create checkpoint store
    store = RoutineCheckpointStore(
        workspace_path=Path("/tmp/test_workspace"),
        execution_id="test_exec_123",
        task_id="test_task",
    )
    
    # Mock operation that returns False
    async def failing_history_inject():
        return False
    
    # Run delivery step and expect RuntimeError
    with pytest.raises(RuntimeError, match="Delivery step history failed: operation returned False"):
        await store.run_delivery_step(
            "history",
            "test_delivery_key_abc",
            failing_history_inject,
        )
    
    # Verify manifest does NOT mark step as delivered
    manifest = store.read_manifest()
    delivery = manifest.get("delivery", {})
    steps = delivery.get("steps", {})
    # Step should be removed (not pending, not delivered)
    assert steps.get("history") is None


if __name__ == "__main__":
    # Run tests with: python -m pytest tests/integration/test_s3_deposit_history_fault_injection.py -v
    pytest.main([__file__, "-v"])
