"""Minimal coverage for #178: sidecar spawn cooldown + inactive event sampling."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.everbot.core.agent.provider.milkie.pool import SidecarPool
from src.everbot.core.runtime.heartbeat import HeartbeatRunner


class _FailSidecar:
    def __init__(self, *_a, **_k):
        self.closed = 0

    async def start(self):
        raise RuntimeError("spawn failed")

    async def close(self):
        self.closed += 1


@pytest.mark.asyncio
async def test_sidecar_pool_cooldown_after_repeated_failures(monkeypatch):
    monkeypatch.setattr(
        "src.everbot.core.agent.provider.milkie.pool.SIDECAR_SPAWN_COOLDOWN_SECONDS",
        60,
    )
    starts = {"n": 0}

    def factory(cmd, env):
        starts["n"] += 1
        return _FailSidecar()

    pool = SidecarPool(build=lambda name: (["c"], {}), sidecar_factory=factory)
    with pytest.raises(RuntimeError, match="spawn failed"):
        await pool.get_or_spawn("alice")
    assert starts["n"] == 1
    # First failure remains immediately retryable.
    with pytest.raises(RuntimeError, match="spawn failed"):
        await pool.get_or_spawn("alice")
    assert starts["n"] == 2
    # Repeated failure enters cooldown and fail-fasts without spawning.
    with pytest.raises(RuntimeError, match="cooldown"):
        await pool.get_or_spawn("alice")
    assert starts["n"] == 2


def test_inactive_skip_events_are_sampled(tmp_path: Path, monkeypatch):
    events_file = tmp_path / "heartbeat_events.jsonl"
    udm = MagicMock()
    udm.heartbeat_events_file = events_file
    monkeypatch.setattr(
        "src.everbot.core.runtime.heartbeat.get_user_data_manager",
        lambda: udm,
    )
    monkeypatch.setattr(
        "src.everbot.core.runtime.heartbeat.INACTIVE_EVENT_SAMPLE_SECONDS",
        60,
    )

    sm = MagicMock()
    sm.get_primary_session_id = lambda n: f"web_session_{n}"
    sm.get_heartbeat_session_id = lambda n: f"heartbeat_session_{n}"
    runner = HeartbeatRunner(
        agent_name="demo",
        workspace_path=tmp_path,
        session_manager=sm,
        agent_factory=MagicMock(),
        interval_minutes=1,
        active_hours=(0, 24),
    )
    runner._write_heartbeat_event("skipped", reason="inactive")
    runner._write_heartbeat_event("skipped", reason="inactive")
    runner._write_heartbeat_event("skipped", reason="inactive")
    lines = events_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["reason"] == "inactive"
