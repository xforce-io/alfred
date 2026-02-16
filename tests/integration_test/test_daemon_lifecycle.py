"""
Integration tests for daemon lifecycle and runner registration.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.everbot.cli import daemon as daemon_module
from src.everbot.infra.config import save_config
from src.everbot.infra.user_data import UserDataManager


class _FakeRunner:
    """Controllable fake heartbeat runner used by daemon tests."""

    instances = []

    def __init__(
        self,
        *,
        agent_name,
        workspace_path,
        session_manager,
        agent_factory,
        interval_minutes,
        active_hours,
        max_retries,
        ack_max_chars,
        realtime_status_hint,
        broadcast_scope,
        routine_reflection,
        auto_register_routines=False,
        on_result=None,
        summary_max_chars=None,
        heartbeat_max_history=10,
        reflect_force_interval_hours=24,
    ):
        self.agent_name = agent_name
        self.workspace_path = Path(workspace_path)
        self.session_manager = session_manager
        self.agent_factory = agent_factory
        self.interval_minutes = interval_minutes
        self.active_hours = active_hours
        self.max_retries = max_retries
        self.ack_max_chars = ack_max_chars
        self.realtime_status_hint = realtime_status_hint
        self.broadcast_scope = broadcast_scope
        self.routine_reflection = routine_reflection
        self.auto_register_routines = auto_register_routines
        self.on_result = on_result
        self.summary_max_chars = summary_max_chars
        self.heartbeat_max_history = heartbeat_max_history
        self.reflect_force_interval_hours = reflect_force_interval_hours
        self._running = False
        self.__class__.instances.append(self)

    async def start(self):
        self._running = True
        if self.on_result is not None:
            await self.on_result(self.agent_name, "runner-started")
        while self._running:
            await asyncio.sleep(0.01)

    def stop(self):
        self._running = False


class _FakeTickRunner:
    """Fake runner exposing run_once_with_options for unified scheduler mode."""

    instances = []

    def __init__(
        self,
        *,
        agent_name,
        workspace_path,
        session_manager,
        agent_factory,
        interval_minutes,
        active_hours,
        max_retries,
        ack_max_chars,
        realtime_status_hint,
        broadcast_scope,
        routine_reflection,
        auto_register_routines=False,
        on_result=None,
        summary_max_chars=None,
        heartbeat_max_history=10,
        reflect_force_interval_hours=24,
    ):
        self.agent_name = agent_name
        self.workspace_path = Path(workspace_path)
        self.session_manager = session_manager
        self.agent_factory = agent_factory
        self.interval_minutes = interval_minutes
        self.active_hours = active_hours
        self.max_retries = max_retries
        self.ack_max_chars = ack_max_chars
        self.realtime_status_hint = realtime_status_hint
        self.broadcast_scope = broadcast_scope
        self.routine_reflection = routine_reflection
        self.auto_register_routines = auto_register_routines
        self.on_result = on_result
        self.summary_max_chars = summary_max_chars
        self.heartbeat_max_history = heartbeat_max_history
        self.reflect_force_interval_hours = reflect_force_interval_hours
        self.run_once_calls = 0
        self.stopped = False
        self.__class__.instances.append(self)

    async def run_once_with_options(self, *, force: bool = False):  # noqa: ARG002
        self.run_once_calls += 1
        if self.on_result is not None:
            await self.on_result(self.agent_name, f"tick-{self.run_once_calls}")
        return "HEARTBEAT_OK"

    async def start(self):
        raise AssertionError("Unified scheduler mode should not call runner.start()")

    def stop(self):
        self.stopped = True


class _FakeSplitRunner:
    """Fake runner with isolated-task scheduler hooks."""

    instances = []

    def __init__(
        self,
        *,
        agent_name,
        workspace_path,
        session_manager,
        agent_factory,
        interval_minutes,
        active_hours,
        max_retries,
        ack_max_chars,
        realtime_status_hint,
        broadcast_scope,
        routine_reflection,
        auto_register_routines=False,
        on_result=None,
        summary_max_chars=None,
        heartbeat_max_history=10,
        reflect_force_interval_hours=24,
    ):
        self.agent_name = agent_name
        self.workspace_path = Path(workspace_path)
        self.session_manager = session_manager
        self.agent_factory = agent_factory
        self.interval_minutes = interval_minutes
        self.active_hours = active_hours
        self.max_retries = max_retries
        self.ack_max_chars = ack_max_chars
        self.realtime_status_hint = realtime_status_hint
        self.broadcast_scope = broadcast_scope
        self.routine_reflection = routine_reflection
        self.auto_register_routines = auto_register_routines
        self.on_result = on_result
        self.summary_max_chars = summary_max_chars
        self.heartbeat_max_history = heartbeat_max_history
        self.reflect_force_interval_hours = reflect_force_interval_hours
        self.run_once_calls = 0
        self.run_once_include_isolated = []
        self.claimed = False
        self.executed = False
        self.stopped = False
        self.__class__.instances.append(self)

    async def run_once_with_options(self, *, force: bool = False, include_isolated: bool = True):  # noqa: ARG002
        self.run_once_calls += 1
        self.run_once_include_isolated.append(include_isolated)
        if self.on_result is not None:
            await self.on_result(self.agent_name, f"tick-{self.run_once_calls}")
        return "HEARTBEAT_OK"

    def list_due_isolated_tasks(self, now=None):  # noqa: ARG002
        if self.claimed or self.executed:
            return []
        return [
            {
                "id": "iso_1",
                "title": "Isolated Task",
                "description": "work",
                "execution_mode": "isolated",
                "timeout_seconds": 30,
            }
        ]

    async def claim_isolated_task(self, task_id: str, now=None):  # noqa: ARG002
        if task_id != "iso_1" or self.claimed:
            return False
        self.claimed = True
        return True

    async def execute_isolated_claimed_task(self, task_snapshot, *, run_id=None, now=None):  # noqa: ARG002
        if task_snapshot.get("id") == "iso_1":
            self.executed = True

    async def start(self):
        raise AssertionError("Unified scheduler mode should not call runner.start()")

    def stop(self):
        self.stopped = True


class _FakeInlineRunner:
    """Fake runner that exposes due inline tasks for scheduler routing."""

    instances = []

    def __init__(
        self,
        *,
        agent_name,
        workspace_path,
        session_manager,
        agent_factory,
        interval_minutes,
        active_hours,
        max_retries,
        ack_max_chars,
        realtime_status_hint,
        broadcast_scope,
        routine_reflection,
        auto_register_routines=False,
        on_result=None,
        summary_max_chars=None,
        heartbeat_max_history=10,
        reflect_force_interval_hours=24,
    ):
        self.agent_name = agent_name
        self.workspace_path = Path(workspace_path)
        self.session_manager = session_manager
        self.agent_factory = agent_factory
        self.interval_minutes = interval_minutes
        self.active_hours = active_hours
        self.max_retries = max_retries
        self.ack_max_chars = ack_max_chars
        self.realtime_status_hint = realtime_status_hint
        self.broadcast_scope = broadcast_scope
        self.routine_reflection = routine_reflection
        self.auto_register_routines = auto_register_routines
        self.on_result = on_result
        self.summary_max_chars = summary_max_chars
        self.heartbeat_max_history = heartbeat_max_history
        self.reflect_force_interval_hours = reflect_force_interval_hours
        self.run_once_calls = 0
        self.run_once_include_inline = []
        self.run_once_include_isolated = []
        self.inline_emitted = False
        self.stopped = False
        self.__class__.instances.append(self)

    async def run_once_with_options(
        self,
        *,
        force: bool = False,  # noqa: ARG002
        include_inline: bool = True,
        include_isolated: bool = True,
    ):
        self.run_once_calls += 1
        self.run_once_include_inline.append(include_inline)
        self.run_once_include_isolated.append(include_isolated)
        if self.on_result is not None:
            await self.on_result(self.agent_name, f"tick-{self.run_once_calls}")
        return "HEARTBEAT_OK"

    def list_due_inline_tasks(self, now=None):  # noqa: ARG002
        if self.inline_emitted:
            return []
        self.inline_emitted = True
        return [
            {
                "id": "inline_1",
                "title": "Inline Task",
                "description": "work",
                "execution_mode": "inline",
                "timeout_seconds": 30,
            }
        ]

    def list_due_isolated_tasks(self, now=None):  # noqa: ARG002
        return []

    async def claim_isolated_task(self, task_id: str, now=None):  # noqa: ARG002
        return False

    async def execute_isolated_claimed_task(self, task_snapshot, *, run_id=None, now=None):  # noqa: ARG002
        raise AssertionError("Inline-only runner should not execute isolated task")

    async def start(self):
        raise AssertionError("Unified scheduler mode should not call runner.start()")

    def stop(self):
        self.stopped = True


@pytest.mark.asyncio
async def test_daemon_start_stop_updates_status_snapshot(monkeypatch, tmp_path: Path):
    alfred_home = tmp_path / ".alfred"
    config_path = tmp_path / "config.yaml"
    save_config(
        {
            "everbot": {
                "enabled": True,
                "agents": {
                    "demo_agent": {
                        "heartbeat": {
                            "enabled": True,
                            "interval": 1,
                            "active_hours": [0, 24],
                        }
                    }
                },
            }
        },
        str(config_path),
    )

    monkeypatch.setattr(
        daemon_module,
        "UserDataManager",
        lambda: UserDataManager(alfred_home=alfred_home),
    )
    monkeypatch.setattr(daemon_module, "HeartbeatRunner", _FakeRunner)
    monkeypatch.setattr(
        daemon_module,
        "get_agent_factory",
        lambda **kwargs: SimpleNamespace(create_agent=AsyncMock()),
    )
    _FakeRunner.instances.clear()

    daemon = daemon_module.EverBotDaemon(config_path=str(config_path))
    run_task = asyncio.create_task(daemon.start())
    await asyncio.sleep(0.08)
    await daemon.stop()
    await asyncio.wait_for(run_task, timeout=2.0)

    snapshot_path = daemon.user_data.status_file
    assert snapshot_path.exists()
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert snapshot["status"] == "stopped"
    assert "demo_agent" in snapshot["agents"]
    assert "demo_agent" in snapshot["heartbeats"]
    assert "metrics" in snapshot
    assert daemon.user_data.pid_file.exists() is False


@pytest.mark.asyncio
async def test_daemon_uses_unified_scheduler_when_runner_supports_tick_mode(monkeypatch, tmp_path: Path):
    alfred_home = tmp_path / ".alfred"
    config_path = tmp_path / "config.yaml"
    save_config(
        {
            "everbot": {
                "enabled": True,
                "agents": {
                    "demo_agent": {
                        "heartbeat": {
                            "enabled": True,
                            "interval": 1,
                            "active_hours": [0, 24],
                        }
                    }
                },
            }
        },
        str(config_path),
    )

    monkeypatch.setattr(
        daemon_module,
        "UserDataManager",
        lambda: UserDataManager(alfred_home=alfred_home),
    )
    monkeypatch.setattr(daemon_module, "HeartbeatRunner", _FakeTickRunner)
    monkeypatch.setattr(
        daemon_module,
        "get_agent_factory",
        lambda **kwargs: SimpleNamespace(create_agent=AsyncMock()),
    )
    _FakeTickRunner.instances.clear()

    daemon = daemon_module.EverBotDaemon(config_path=str(config_path))
    run_task = asyncio.create_task(daemon.start())
    await asyncio.sleep(0.15)
    await daemon.stop()
    await asyncio.wait_for(run_task, timeout=2.0)

    assert len(_FakeTickRunner.instances) == 1
    runner = _FakeTickRunner.instances[0]
    assert runner.run_once_calls >= 1
    assert runner.stopped is True

    snapshot = json.loads(daemon.user_data.status_file.read_text(encoding="utf-8"))
    assert "demo_agent" in snapshot["heartbeats"]


@pytest.mark.asyncio
async def test_daemon_unified_scheduler_routes_isolated_tasks(monkeypatch, tmp_path: Path):
    alfred_home = tmp_path / ".alfred"
    config_path = tmp_path / "config.yaml"
    save_config(
        {
            "everbot": {
                "enabled": True,
                "agents": {
                    "demo_agent": {
                        "heartbeat": {
                            "enabled": True,
                            "interval": 1,
                            "active_hours": [0, 24],
                        }
                    }
                },
            }
        },
        str(config_path),
    )

    monkeypatch.setattr(
        daemon_module,
        "UserDataManager",
        lambda: UserDataManager(alfred_home=alfred_home),
    )
    monkeypatch.setattr(daemon_module, "HeartbeatRunner", _FakeSplitRunner)
    monkeypatch.setattr(
        daemon_module,
        "get_agent_factory",
        lambda **kwargs: SimpleNamespace(create_agent=AsyncMock()),
    )
    _FakeSplitRunner.instances.clear()

    daemon = daemon_module.EverBotDaemon(config_path=str(config_path))
    run_task = asyncio.create_task(daemon.start())
    await asyncio.sleep(0.15)
    await daemon.stop()
    await asyncio.wait_for(run_task, timeout=2.0)

    assert len(_FakeSplitRunner.instances) == 1
    runner = _FakeSplitRunner.instances[0]
    assert runner.run_once_calls >= 1
    assert runner.stopped is True
    assert runner.claimed is True
    assert runner.executed is True

    snapshot = json.loads(daemon.user_data.status_file.read_text(encoding="utf-8"))
    assert "demo_agent" in snapshot["heartbeats"]


@pytest.mark.asyncio
async def test_daemon_unified_scheduler_routes_inline_tasks(monkeypatch, tmp_path: Path):
    alfred_home = tmp_path / ".alfred"
    config_path = tmp_path / "config.yaml"
    save_config(
        {
            "everbot": {
                "enabled": True,
                "agents": {
                    "demo_agent": {
                        "heartbeat": {
                            "enabled": True,
                            "interval": 1,
                            "active_hours": [0, 24],
                        }
                    }
                },
            }
        },
        str(config_path),
    )

    monkeypatch.setattr(
        daemon_module,
        "UserDataManager",
        lambda: UserDataManager(alfred_home=alfred_home),
    )
    monkeypatch.setattr(daemon_module, "HeartbeatRunner", _FakeInlineRunner)
    monkeypatch.setattr(
        daemon_module,
        "get_agent_factory",
        lambda **kwargs: SimpleNamespace(create_agent=AsyncMock()),
    )
    _FakeInlineRunner.instances.clear()

    daemon = daemon_module.EverBotDaemon(config_path=str(config_path))
    run_task = asyncio.create_task(daemon.start())
    await asyncio.sleep(0.15)
    await daemon.stop()
    await asyncio.wait_for(run_task, timeout=2.0)

    assert len(_FakeInlineRunner.instances) == 1
    runner = _FakeInlineRunner.instances[0]
    assert runner.run_once_calls >= 1
    assert runner.stopped is True
    assert runner.inline_emitted is True

    snapshot = json.loads(daemon.user_data.status_file.read_text(encoding="utf-8"))
    assert "demo_agent" in snapshot["heartbeats"]