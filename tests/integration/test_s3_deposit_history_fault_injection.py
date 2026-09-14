"""S3 fault-injection test: deposit/history failures must fail the task.

Tests that when deposit_job_event or inject_to_history returns False,
the task is marked FAILED (not DONE) and will retry.
"""
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.everbot.core.runtime.cron import CronExecutor
from src.everbot.core.runtime.cron_delivery import CronDelivery
from src.everbot.core.tasks.routine_manager import RoutineManager


def _make_executor(tmp_path: Path, **overrides) -> CronExecutor:
    """Helper to create CronExecutor with mocked dependencies."""
    sm = AsyncMock()
    sm.get_primary_session_id.return_value = "web_session_test"
    sm.get_heartbeat_session_id.return_value = "heartbeat_session_test"
    delivery = CronDelivery(
        session_manager=sm,
        primary_session_id="web_session_test",
        heartbeat_session_id="heartbeat_session_test",
        agent_name="test_agent",
        realtime_push=False,
    )
    defaults = dict(
        agent_name="test_agent",
        workspace_path=tmp_path,
        session_manager=sm,
        agent_factory=AsyncMock(),
        routine_manager=RoutineManager(tmp_path),
        delivery=delivery,
    )
    defaults.update(overrides)
    return CronExecutor(**defaults)


def _seed_task(tmp_path: Path, **task_overrides):
    """Seed HEARTBEAT.md with one task and return the manager."""
    mgr = RoutineManager(tmp_path)
    defaults = dict(
        title="Test task",
        schedule="1h",
        next_run_at="2026-03-01T11:00:00+00:00",
        now=datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc),
    )
    defaults.update(task_overrides)
    mgr.add_routine(**defaults)
    return mgr


@pytest.mark.asyncio
async def test_deposit_job_event_false_fails_task(tmp_path):
    """S3: deposit_job_event returning False must fail task."""
    mgr = _seed_task(
        tmp_path,
        title="S3 deposit test",
        execution_mode="isolated",
        job="skill-evaluate",
    )
    executor = _make_executor(tmp_path, routine_manager=mgr)
    
    # Patch _invoke_job to return a result
    with patch.object(executor, '_invoke_job', new_callable=AsyncMock) as mock_invoke:
        mock_invoke.return_value = "test result"
        
        # Patch deposit_job_event to return False (simulate failure)
        with patch.object(
            executor.delivery, 'deposit_job_event', new_callable=AsyncMock
        ) as mock_deposit:
            mock_deposit.return_value = False
            
            task_list = mgr.load_task_list()
            result = await executor.tick(
                task_list,
                run_agent=AsyncMock(),
                inject_context=AsyncMock(),
                run_id="test_run",
            )
            
            # Task should have failed
            assert result.executed == 1
            assert result.results[0].status == "failed"
            # Error message should match S3 requirement
            assert "Failed to deposit job completion event to mailbox" in result.results[0].error
            
            # Verify deposit was called
            mock_deposit.assert_called_once()


@pytest.mark.asyncio
async def test_inject_to_history_false_fails_task(tmp_path):
    """S3: inject_to_history returning False must fail task."""
    mgr = _seed_task(
        tmp_path,
        title="S3 history test",
        execution_mode="isolated",
        job="skill-evaluate",
    )
    executor = _make_executor(tmp_path, routine_manager=mgr)
    
    # Patch _invoke_job to return a result
    with patch.object(executor, '_invoke_job', new_callable=AsyncMock) as mock_invoke:
        mock_invoke.return_value = "test result"
        
        # Patch deposit_job_event to succeed
        with patch.object(
            executor.delivery, 'deposit_job_event', new_callable=AsyncMock
        ) as mock_deposit:
            mock_deposit.return_value = True
            
            # Patch inject_to_history to return False (simulate failure)
            with patch.object(
                executor.delivery, 'inject_to_history', new_callable=AsyncMock
            ) as mock_inject:
                mock_inject.return_value = False
                
                task_list = mgr.load_task_list()
                result = await executor.tick(
                    task_list,
                    run_agent=AsyncMock(),
                    inject_context=AsyncMock(),
                    run_id="test_run",
                )
                
                # Task should have failed
                assert result.executed == 1
                assert result.results[0].status == "failed"
                # Error message should match S3 requirement
                assert "Failed to inject job result to history" in result.results[0].error
                
                # Verify both were called
                mock_deposit.assert_called_once()
                mock_inject.assert_called_once()


@pytest.mark.asyncio
async def test_staged_history_step_false_fails_task(tmp_path):
    """S3: staged history delivery step returning False must fail via run_delivery_step."""
    from src.everbot.core.runtime.routine_checkpoint import RoutineCheckpointStore
    
    # Create checkpoint store
    store = RoutineCheckpointStore(
        workspace_path=tmp_path,
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
    # Run tests with: PYTHONPATH=src python -m pytest tests/integration/test_s3_deposit_history_fault_injection.py -v
    pytest.main([__file__, "-v"])
