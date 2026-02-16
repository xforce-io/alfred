"""
Tests for heartbeat token consumption optimizations:
1. Reflect skip when MEMORY.md/HEARTBEAT.md unchanged (file hash)
2. History trimming for heartbeat sessions
3. Agent creation skipped for idle / disabled-reflect modes
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.everbot.core.runtime.heartbeat import HeartbeatRunner
from src.everbot.core.tasks.task_manager import Task, TaskList, TaskState


def _make_runner(workspace_path: Path = Path("."), **overrides) -> HeartbeatRunner:
    session_manager = SimpleNamespace(
        get_primary_session_id=lambda agent_name: f"web_session_{agent_name}",
        get_heartbeat_session_id=lambda agent_name: f"heartbeat_session_{agent_name}",
    )
    defaults = {
        "agent_name": "test_agent",
        "workspace_path": workspace_path,
        "session_manager": session_manager,
        "agent_factory": AsyncMock(),
        "interval_minutes": 1,
        "active_hours": (0, 24),
        "max_retries": 1,
        "on_result": None,
    }
    defaults.update(overrides)
    return HeartbeatRunner(**defaults)


def _write_heartbeat_md(workspace: Path, tasks: list[dict] | None = None) -> None:
    """Write a valid HEARTBEAT.md with optional task list."""
    task_list = {"version": 2, "tasks": tasks or []}
    content = f"# HEARTBEAT\n\n## Tasks\n\n```json\n{json.dumps(task_list, indent=2)}\n```\n"
    (workspace / "HEARTBEAT.md").write_text(content, encoding="utf-8")


# ============================================================
# Optimization 1: Reflect skip on file hash unchanged
# ============================================================


class TestReflectFileHashSkip:
    """Tests for _should_skip_reflection / _update_reflect_state."""

    def test_first_reflect_never_skipped(self, tmp_path: Path):
        """First reflect should always run (no prior state)."""
        runner = _make_runner(workspace_path=tmp_path)
        _write_heartbeat_md(tmp_path)
        (tmp_path / "MEMORY.md").write_text("some memory", encoding="utf-8")
        assert runner._should_skip_reflection() is False

    def test_skip_when_files_unchanged(self, tmp_path: Path):
        """After reflect, skip if files haven't changed."""
        runner = _make_runner(workspace_path=tmp_path)
        _write_heartbeat_md(tmp_path)
        (tmp_path / "MEMORY.md").write_text("some memory", encoding="utf-8")

        runner._update_reflect_state()

        assert runner._should_skip_reflection() is True

    def test_no_skip_when_memory_changed(self, tmp_path: Path):
        """If MEMORY.md changes after reflect, don't skip."""
        runner = _make_runner(workspace_path=tmp_path)
        _write_heartbeat_md(tmp_path)
        (tmp_path / "MEMORY.md").write_text("v1", encoding="utf-8")

        runner._update_reflect_state()

        (tmp_path / "MEMORY.md").write_text("v2", encoding="utf-8")
        assert runner._should_skip_reflection() is False

    def test_no_skip_when_heartbeat_changed(self, tmp_path: Path):
        """If HEARTBEAT.md changes after reflect, don't skip."""
        runner = _make_runner(workspace_path=tmp_path)
        _write_heartbeat_md(tmp_path)

        runner._update_reflect_state()

        _write_heartbeat_md(tmp_path, tasks=[{
            "id": "new_task", "title": "New", "schedule": "1d",
            "state": "pending", "enabled": True,
        }])
        assert runner._should_skip_reflection() is False

    def test_force_after_interval_even_if_unchanged(self, tmp_path: Path):
        """After force interval elapses, reflect even if files unchanged."""
        runner = _make_runner(workspace_path=tmp_path, reflect_force_interval_hours=1)
        _write_heartbeat_md(tmp_path)
        (tmp_path / "MEMORY.md").write_text("stable", encoding="utf-8")

        runner._update_reflect_state()
        # Simulate time passing beyond force interval
        runner._last_reflect_at = datetime.now() - timedelta(hours=2)

        assert runner._should_skip_reflection() is False

    def test_skip_within_force_interval(self, tmp_path: Path):
        """Within force interval and files unchanged, skip."""
        runner = _make_runner(workspace_path=tmp_path, reflect_force_interval_hours=24)
        _write_heartbeat_md(tmp_path)

        runner._update_reflect_state()
        # Still well within 24h
        runner._last_reflect_at = datetime.now() - timedelta(hours=1)

        assert runner._should_skip_reflection() is True

    def test_missing_files_treated_as_empty_hash(self, tmp_path: Path):
        """Missing MEMORY.md is handled gracefully."""
        runner = _make_runner(workspace_path=tmp_path)
        _write_heartbeat_md(tmp_path)
        # No MEMORY.md exists

        runner._update_reflect_state()

        # Still no MEMORY.md — unchanged
        assert runner._should_skip_reflection() is True

        # Create MEMORY.md — now changed
        (tmp_path / "MEMORY.md").write_text("new content", encoding="utf-8")
        assert runner._should_skip_reflection() is False


# ============================================================
# Optimization 2: History trimming
# ============================================================


class TestHistoryTrimming:
    """Tests for _trim_session_history."""

    def test_trim_to_max_history(self, tmp_path: Path):
        """History is trimmed to heartbeat_max_history."""
        runner = _make_runner(workspace_path=tmp_path, heartbeat_max_history=5)
        runner._heartbeat_mode = "structured_reflect"
        session_data = SimpleNamespace(
            history_messages=[{"role": "user", "content": f"msg{i}"} for i in range(20)]
        )
        runner._trim_session_history(session_data)
        assert len(session_data.history_messages) == 5
        # Should keep the LAST 5 messages
        assert session_data.history_messages[0]["content"] == "msg15"

    def test_no_trim_when_under_limit(self, tmp_path: Path):
        """Don't trim if history is already under the limit."""
        runner = _make_runner(workspace_path=tmp_path, heartbeat_max_history=10)
        runner._heartbeat_mode = "structured_reflect"
        messages = [{"role": "user", "content": f"msg{i}"} for i in range(5)]
        session_data = SimpleNamespace(history_messages=messages)
        runner._trim_session_history(session_data)
        assert len(session_data.history_messages) == 5

    def test_structured_due_gets_higher_limit(self, tmp_path: Path):
        """structured_due mode uses max(heartbeat_max_history, 30)."""
        runner = _make_runner(workspace_path=tmp_path, heartbeat_max_history=5)
        runner._heartbeat_mode = "structured_due"
        session_data = SimpleNamespace(
            history_messages=[{"role": "user", "content": f"msg{i}"} for i in range(50)]
        )
        runner._trim_session_history(session_data)
        assert len(session_data.history_messages) == 30

    def test_structured_due_respects_large_max_history(self, tmp_path: Path):
        """If heartbeat_max_history > 30, structured_due uses the larger value."""
        runner = _make_runner(workspace_path=tmp_path, heartbeat_max_history=50)
        runner._heartbeat_mode = "structured_due"
        session_data = SimpleNamespace(
            history_messages=[{"role": "user", "content": f"msg{i}"} for i in range(100)]
        )
        runner._trim_session_history(session_data)
        assert len(session_data.history_messages) == 50

    def test_none_session_data_handled(self, tmp_path: Path):
        """None session_data doesn't crash."""
        runner = _make_runner(workspace_path=tmp_path)
        runner._trim_session_history(None)  # Should not raise

    def test_empty_history_handled(self, tmp_path: Path):
        """Empty history list doesn't crash."""
        runner = _make_runner(workspace_path=tmp_path)
        session_data = SimpleNamespace(history_messages=[])
        runner._trim_session_history(session_data)
        assert session_data.history_messages == []


# ============================================================
# Optimization 3: Agent creation skipped for idle / reflect
# ============================================================


class TestAgentCreationSkip:
    """Tests for skipping agent creation in _execute_once."""

    @pytest.mark.asyncio
    async def test_idle_mode_skips_agent_creation(self, tmp_path: Path, monkeypatch):
        """When HEARTBEAT.md doesn't exist (idle), no agent is created."""
        runner = _make_runner(workspace_path=tmp_path)
        # No HEARTBEAT.md — idle mode

        agent_factory = AsyncMock()
        runner.agent_factory = agent_factory

        get_agent_mock = AsyncMock()
        monkeypatch.setattr(runner, "_get_or_create_agent", get_agent_mock)
        monkeypatch.setattr(runner, "_record_timeline_event", MagicMock())
        monkeypatch.setattr(runner, "_record_runtime_metric", MagicMock())

        # Stub session locking (go straight to body)
        runner.session_manager.acquire_session = AsyncMock(return_value=True)
        runner.session_manager.release_session = MagicMock()
        monkeypatch.setattr(
            "src.everbot.core.runtime.heartbeat.asyncio.sleep",
            AsyncMock(),
        )
        # Stub event emit
        monkeypatch.setattr(
            "src.everbot.core.runtime.heartbeat.HeartbeatRunner._deposit_deliver_event_to_primary_session",
            AsyncMock(return_value=True),
        )

        result = await runner._execute_once()
        assert result == "HEARTBEAT_IDLE"
        get_agent_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_disabled_reflect_skips_agent_creation(self, tmp_path: Path, monkeypatch):
        """When routine_reflection=False and mode=structured_reflect, no agent is created."""
        runner = _make_runner(workspace_path=tmp_path, routine_reflection=False)
        _write_heartbeat_md(tmp_path)  # Valid HEARTBEAT.md but no due tasks → reflect mode

        get_agent_mock = AsyncMock()
        monkeypatch.setattr(runner, "_get_or_create_agent", get_agent_mock)
        monkeypatch.setattr(runner, "_record_timeline_event", MagicMock())
        monkeypatch.setattr(runner, "_record_runtime_metric", MagicMock())

        runner.session_manager.acquire_session = AsyncMock(return_value=True)
        runner.session_manager.release_session = MagicMock()

        result = await runner._execute_once()
        assert result == "HEARTBEAT_OK"
        get_agent_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reflect_skip_by_hash_skips_agent_creation(self, tmp_path: Path, monkeypatch):
        """When files unchanged (hash skip), no agent is created."""
        runner = _make_runner(workspace_path=tmp_path, routine_reflection=True)
        _write_heartbeat_md(tmp_path)
        (tmp_path / "MEMORY.md").write_text("stable", encoding="utf-8")

        # Simulate prior reflect
        runner._update_reflect_state()

        get_agent_mock = AsyncMock()
        monkeypatch.setattr(runner, "_get_or_create_agent", get_agent_mock)
        monkeypatch.setattr(runner, "_record_timeline_event", MagicMock())
        monkeypatch.setattr(runner, "_record_runtime_metric", MagicMock())

        runner.session_manager.acquire_session = AsyncMock(return_value=True)
        runner.session_manager.release_session = MagicMock()

        result = await runner._execute_once()
        assert result == "HEARTBEAT_OK"
        get_agent_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reflect_runs_after_file_change(self, tmp_path: Path, monkeypatch):
        """After file change, reflect should run (agent created)."""
        runner = _make_runner(workspace_path=tmp_path, routine_reflection=True)
        _write_heartbeat_md(tmp_path)
        (tmp_path / "MEMORY.md").write_text("v1", encoding="utf-8")

        runner._update_reflect_state()

        # Change MEMORY.md
        (tmp_path / "MEMORY.md").write_text("v2 — new intentions", encoding="utf-8")

        fake_agent = SimpleNamespace(
            executor=SimpleNamespace(
                context=SimpleNamespace(
                    set_variable=MagicMock(),
                    get_var_value=MagicMock(return_value=""),
                    set_session_id=MagicMock(),
                    init_trajectory=MagicMock(),
                )
            ),
            name="test_agent",
        )
        get_agent_mock = AsyncMock(return_value=fake_agent)
        monkeypatch.setattr(runner, "_get_or_create_agent", get_agent_mock)
        monkeypatch.setattr(runner, "_run_agent", AsyncMock(return_value="HEARTBEAT_OK"))
        monkeypatch.setattr(runner, "_save_session_atomic", AsyncMock())
        monkeypatch.setattr(runner, "_record_timeline_event", MagicMock())
        monkeypatch.setattr(runner, "_record_runtime_metric", MagicMock())
        monkeypatch.setattr(runner, "_inject_result_to_primary_history", AsyncMock(return_value=True))
        monkeypatch.setattr(runner, "_deposit_deliver_event_to_primary_session", AsyncMock(return_value=True))
        monkeypatch.setattr(runner, "_write_task_snapshot", MagicMock())
        monkeypatch.setattr(
            "src.everbot.core.runtime.heartbeat.asyncio.sleep",
            AsyncMock(),
        )

        runner.session_manager.acquire_session = AsyncMock(return_value=True)
        runner.session_manager.release_session = MagicMock()

        result = await runner._execute_once()
        get_agent_mock.assert_awaited_once()


# ============================================================
# Integration: _compute_file_hashes
# ============================================================


class TestComputeFileHashes:

    def test_both_files_present(self, tmp_path: Path):
        runner = _make_runner(workspace_path=tmp_path)
        (tmp_path / "MEMORY.md").write_text("mem", encoding="utf-8")
        (tmp_path / "HEARTBEAT.md").write_text("hb", encoding="utf-8")
        hashes = runner._compute_file_hashes()
        assert "MEMORY.md" in hashes
        assert "HEARTBEAT.md" in hashes
        assert len(hashes["MEMORY.md"]) == 32  # MD5 hex length
        assert len(hashes["HEARTBEAT.md"]) == 32

    def test_missing_file_returns_empty_string(self, tmp_path: Path):
        runner = _make_runner(workspace_path=tmp_path)
        hashes = runner._compute_file_hashes()
        assert hashes["MEMORY.md"] == ""
        assert hashes["HEARTBEAT.md"] == ""

    def test_hash_changes_on_content_change(self, tmp_path: Path):
        runner = _make_runner(workspace_path=tmp_path)
        (tmp_path / "MEMORY.md").write_text("v1", encoding="utf-8")
        hash1 = runner._compute_file_hashes()["MEMORY.md"]

        (tmp_path / "MEMORY.md").write_text("v2", encoding="utf-8")
        hash2 = runner._compute_file_hashes()["MEMORY.md"]

        assert hash1 != hash2

    def test_same_content_same_hash(self, tmp_path: Path):
        runner = _make_runner(workspace_path=tmp_path)
        (tmp_path / "MEMORY.md").write_text("same", encoding="utf-8")
        hash1 = runner._compute_file_hashes()["MEMORY.md"]
        hash2 = runner._compute_file_hashes()["MEMORY.md"]
        assert hash1 == hash2
