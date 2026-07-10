from __future__ import annotations

from pathlib import Path

import pytest

from src.everbot.core.runtime.routine_checkpoint import RoutineCheckpointStore


@pytest.mark.asyncio
async def test_completed_stage_is_reused_after_store_restart(tmp_path: Path):
    calls = 0

    async def produce() -> str:
        nonlocal calls
        calls += 1
        return "fetched data"

    first = RoutineCheckpointStore(tmp_path, "task-1:2026-07-10T10:00:00Z", "task-1")
    assert await first.run_stage("fetch", "source=v1", produce, run_id="run-1") == "fetched data"

    restarted = RoutineCheckpointStore(tmp_path, "task-1:2026-07-10T10:00:00Z", "task-1")
    assert await restarted.run_stage("fetch", "source=v1", produce, run_id="run-2") == "fetched data"
    assert calls == 1


@pytest.mark.asyncio
async def test_changed_fetch_input_invalidates_analyze(tmp_path: Path):
    store = RoutineCheckpointStore(tmp_path, "execution", "task-1")
    await store.run_stage("fetch", "source=v1", lambda: _value("data-v1"), run_id="run-1")
    await store.run_stage("analyze", "analysis:v1:data-v1", lambda: _value("report-v1"), run_id="run-1")

    await store.run_stage("fetch", "source=v2", lambda: _value("data-v2"), run_id="run-2")
    manifest = store.read_manifest()
    assert "analyze" not in manifest["stages"]
    assert manifest.get("delivery") is None


@pytest.mark.asyncio
async def test_pending_delivery_step_is_not_repeated_after_ambiguous_failure(tmp_path: Path):
    store = RoutineCheckpointStore(tmp_path, "execution", "task-1")
    visible: list[str] = []

    async def ambiguous() -> None:
        visible.append("sent")
        raise ConnectionError("confirmation lost")

    with pytest.raises(ConnectionError):
        await store.run_delivery_step("realtime", "delivery-key", ambiguous)

    restarted = RoutineCheckpointStore(tmp_path, "execution", "task-1")
    ran = await restarted.run_delivery_step("realtime", "delivery-key", ambiguous)
    assert ran is False
    assert visible == ["sent"]


async def _value(value: str) -> str:
    return value
