"""Unit tests for _tasks cleanup in web app module."""

from __future__ import annotations

from src.everbot.web.app import _tasks, _task_refs, _cleanup_completed_tasks, _MAX_COMPLETED_TASKS


class TestCleanupCompletedTasks:
    """Tests for the _cleanup_completed_tasks helper."""

    def setup_method(self):
        _tasks.clear()

    def teardown_method(self):
        _tasks.clear()

    def test_no_cleanup_when_below_threshold(self):
        """Should not remove anything when task count is within limit."""
        _tasks["t1"] = "done"
        _tasks["t2"] = "running"
        _cleanup_completed_tasks()
        assert len(_tasks) == 2

    def test_cleanup_removes_done_tasks(self):
        """Should remove 'done' tasks when dict exceeds limit."""
        for i in range(_MAX_COMPLETED_TASKS + 10):
            _tasks[f"done-{i}"] = "done"
        _tasks["running-1"] = "running"
        _tasks["scheduled-1"] = "scheduled"

        _cleanup_completed_tasks()

        assert "running-1" in _tasks
        assert "scheduled-1" in _tasks
        # All done tasks should be removed
        assert not any(k.startswith("done-") for k in _tasks)

    def test_cleanup_removes_error_tasks(self):
        """Should remove 'error:*' tasks when dict exceeds limit."""
        for i in range(_MAX_COMPLETED_TASKS + 5):
            _tasks[f"err-{i}"] = f"error: something went wrong {i}"
        _tasks["active-1"] = "running"

        _cleanup_completed_tasks()

        assert "active-1" in _tasks
        assert not any(k.startswith("err-") for k in _tasks)

    def test_preserves_running_and_scheduled(self):
        """Should never remove running or scheduled tasks."""
        for i in range(_MAX_COMPLETED_TASKS + 20):
            _tasks[f"running-{i}"] = "running"
        _tasks["done-1"] = "done"

        _cleanup_completed_tasks()

        # done-1 gets cleaned, but running tasks that fit within limit stay
        assert "done-1" not in _tasks
        # Phase 2 eviction kicks in: only _MAX_COMPLETED_TASKS remain
        assert len(_tasks) == _MAX_COMPLETED_TASKS

    def test_phase2_evicts_oldest_nonterminal_tasks(self):
        """When only non-terminal tasks remain and exceed limit, oldest are evicted."""
        for i in range(_MAX_COMPLETED_TASKS + 10):
            _tasks[f"stuck-{i}"] = "running"

        _cleanup_completed_tasks()

        # Dict should be capped at _MAX_COMPLETED_TASKS
        assert len(_tasks) == _MAX_COMPLETED_TASKS
        # Oldest entries (lowest indices) should be evicted first
        for i in range(10):
            assert f"stuck-{i}" not in _tasks
        # Newest entries should remain
        for i in range(10, _MAX_COMPLETED_TASKS + 10):
            assert f"stuck-{i}" in _tasks

    def test_phase2_evicts_oldest_scheduled_tasks(self):
        """Scheduled tasks stuck forever are also evicted in phase 2."""
        for i in range(_MAX_COMPLETED_TASKS + 5):
            _tasks[f"sched-{i}"] = "scheduled"

        _cleanup_completed_tasks()

        assert len(_tasks) == _MAX_COMPLETED_TASKS
        # First 5 should be gone
        for i in range(5):
            assert f"sched-{i}" not in _tasks

    def test_task_refs_set_exists(self):
        """_task_refs set should exist to prevent GC of fire-and-forget tasks."""
        assert isinstance(_task_refs, set)

    def test_mixed_terminal_and_nonterminal_cleanup(self):
        """Phase 1 removes terminal, phase 2 caps remaining non-terminal."""
        # Add enough to exceed limit
        for i in range(30):
            _tasks[f"done-{i}"] = "done"
        for i in range(_MAX_COMPLETED_TASKS + 5):
            _tasks[f"running-{i}"] = "running"

        _cleanup_completed_tasks()

        # All done tasks removed in phase 1
        assert not any(k.startswith("done-") for k in _tasks)
        # Phase 2 caps remaining running tasks
        assert len(_tasks) == _MAX_COMPLETED_TASKS
