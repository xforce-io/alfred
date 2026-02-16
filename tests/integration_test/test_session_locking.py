"""
Integration tests for session lock behavior.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.everbot.core.session.session import SessionManager


@pytest.mark.asyncio
async def test_acquire_session_times_out_when_lock_is_held(tmp_path: Path):
    manager = SessionManager(tmp_path)
    session_id = "lock_timeout_case"

    first = await manager.acquire_session(session_id, timeout=0.1)
    assert first is True
    try:
        second = await manager.acquire_session(session_id, timeout=0.05)
        assert second is False
    finally:
        manager.release_session(session_id)


@pytest.mark.asyncio
async def test_session_context_releases_lock_after_exception(tmp_path: Path):
    manager = SessionManager(tmp_path)
    session_id = "lock_release_case"

    with pytest.raises(RuntimeError):
        async with manager.session_context(session_id, timeout=0.1) as acquired:
            assert acquired is True
            raise RuntimeError("simulated failure")

    reacquired = await manager.acquire_session(session_id, timeout=0.05)
    assert reacquired is True
    manager.release_session(session_id)
