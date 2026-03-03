"""Unit tests for session mailbox atomic operations."""

import asyncio
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest

from src.everbot.core.session.session import SessionManager


@pytest.mark.asyncio
async def test_deposit_and_ack_mailbox_event(tmp_path: Path):
    manager = SessionManager(tmp_path)
    session_id = "web_session_demo_agent"
    event = {
        "event_id": "evt_123",
        "event_type": "heartbeat_result",
        "summary": "new update",
    }

    ok = await manager.deposit_mailbox_event(session_id, event)
    assert ok is True

    loaded = await manager.load_session(session_id)
    assert loaded is not None
    assert isinstance(loaded.mailbox, list)
    assert len(loaded.mailbox) == 1
    assert loaded.mailbox[0]["event_id"] == "evt_123"
    metrics = manager.get_metrics_snapshot()
    assert metrics.get("mailbox_deposit_count", 0) >= 1

    ack_ok = await manager.ack_mailbox_events(session_id, ["evt_123"])
    assert ack_ok is True

    loaded2 = await manager.load_session(session_id)
    assert loaded2 is not None
    assert loaded2.mailbox == []
    metrics = manager.get_metrics_snapshot()
    assert metrics.get("mailbox_drain_count", 0) >= 1


@pytest.mark.asyncio
async def test_deposit_mailbox_event_is_idempotent_by_event_id(tmp_path: Path):
    manager = SessionManager(tmp_path)
    session_id = "web_session_demo_agent"
    event = {
        "event_id": "evt_same",
        "event_type": "heartbeat_result",
        "summary": "same update",
    }

    ok1 = await manager.deposit_mailbox_event(session_id, event)
    ok2 = await manager.deposit_mailbox_event(session_id, event)
    assert ok1 is True
    assert ok2 is True

    loaded = await manager.load_session(session_id)
    assert loaded is not None
    assert len(loaded.mailbox) == 1
    assert loaded.mailbox[0]["event_id"] == "evt_same"
    metrics = manager.get_metrics_snapshot()
    assert metrics.get("mailbox_deposit_count", 0) >= 1
    assert metrics.get("mailbox_dedup_drop_count", 0) >= 1


@pytest.mark.asyncio
async def test_deposit_mailbox_event_replaces_same_dedupe_key(tmp_path: Path):
    manager = SessionManager(tmp_path)
    session_id = "web_session_demo_agent"
    old_event = {
        "event_id": "evt_old",
        "event_type": "job_completed",
        "summary": "old summary",
        "dedupe_key": "job:daily",
    }
    new_event = {
        "event_id": "evt_new",
        "event_type": "job_completed",
        "summary": "new summary",
        "dedupe_key": "job:daily",
    }

    assert await manager.deposit_mailbox_event(session_id, old_event) is True
    assert await manager.deposit_mailbox_event(session_id, new_event) is True

    loaded = await manager.load_session(session_id)
    assert loaded is not None
    assert len(loaded.mailbox) == 1
    assert loaded.mailbox[0]["event_id"] == "evt_new"
    assert loaded.mailbox[0]["summary"] == "new summary"
    metrics = manager.get_metrics_snapshot()
    assert metrics.get("mailbox_dedup_drop_count", 0) >= 1


@pytest.mark.asyncio
async def test_deposit_mailbox_event_drops_stale_suppressed_event(tmp_path: Path):
    manager = SessionManager(tmp_path)
    session_id = "web_session_demo_agent"
    stale_event = {
        "event_id": "evt_stale",
        "event_type": "heartbeat_result",
        "summary": "stale update",
        "suppress_if_stale": True,
        "timestamp": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
    }

    assert await manager.deposit_mailbox_event(session_id, stale_event) is True

    loaded = await manager.load_session(session_id)
    assert loaded is not None
    assert loaded.mailbox == []
    metrics = manager.get_metrics_snapshot()
    assert metrics.get("mailbox_stale_drop_count", 0) >= 1


@pytest.mark.asyncio
async def test_ack_with_lock_already_held(tmp_path: Path):
    """ack_mailbox_events with lock_already_held=True succeeds even when
    the asyncio lock is already held by the caller (Bug 3 fix)."""
    manager = SessionManager(tmp_path)
    session_id = "web_session_demo_agent"
    event = {"event_id": "evt_lock", "event_type": "heartbeat_result", "summary": "x"}
    await manager.deposit_mailbox_event(session_id, event)

    # Acquire the in-process asyncio lock, simulating process_message() context
    lock = manager._get_lock(session_id)
    await lock.acquire()
    try:
        ok = await manager.ack_mailbox_events(
            session_id, ["evt_lock"], lock_already_held=True,
        )
        assert ok is True
        loaded = await manager.load_session(session_id)
        assert loaded is not None
        assert loaded.mailbox == []
    finally:
        lock.release()


@pytest.mark.asyncio
async def test_ack_with_flock_already_held(tmp_path: Path):
    """ack_mailbox_events with lock_already_held=True succeeds even when
    the caller already holds the cross-process flock on the session file.

    This reproduces the production bug: core_service holds flock(fd1),
    then _ack_mailbox_events → persistence.update_atomic tries flock(fd2)
    on the same lock file → EWOULDBLOCK on macOS → ACK silently fails.
    """
    import fcntl
    import os

    manager = SessionManager(tmp_path)
    persistence = manager.persistence
    session_id = "web_session_demo_agent"
    event = {"event_id": "evt_flock", "event_type": "heartbeat_result", "summary": "x"}
    await manager.deposit_mailbox_event(session_id, event)

    # Simulate core_service holding the flock (exactly as process_message does)
    lock_path = persistence._get_lock_path(session_id)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        ok = await manager.ack_mailbox_events(
            session_id, ["evt_flock"], lock_already_held=True,
        )
        assert ok is True, (
            "ack_mailbox_events failed while caller holds flock — "
            "persistence.update_atomic tried to re-acquire the same flock"
        )
        loaded = await manager.load_session(session_id)
        assert loaded is not None
        assert loaded.mailbox == [], (
            f"Mailbox should be empty after ACK, but has {len(loaded.mailbox)} events"
        )
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@pytest.mark.asyncio
async def test_ack_deadlocks_without_flag(tmp_path: Path):
    """Without lock_already_held=True, ack_mailbox_events times out when
    the asyncio lock is already held (regression proof for Bug 3)."""
    manager = SessionManager(tmp_path)
    session_id = "web_session_demo_agent"
    event = {"event_id": "evt_dl", "event_type": "heartbeat_result", "summary": "x"}
    await manager.deposit_mailbox_event(session_id, event)

    lock = manager._get_lock(session_id)
    await lock.acquire()
    try:
        # Default path tries to acquire the same lock → timeout
        ok = await manager.ack_mailbox_events(
            session_id, ["evt_dl"], timeout=0.1, blocking=False,
        )
        assert ok is False
        # Event should still be present
        loaded = await manager.load_session(session_id)
        assert loaded is not None
        assert len(loaded.mailbox) == 1
    finally:
        lock.release()
