"""Unit tests for archived job session lifecycle."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.everbot.core.session.session import SessionData, SessionManager


def _make_job_session(
    session_id: str,
    *,
    state: str = "active",
    archived_at: str | None = None,
    updated_at: str | None = None,
) -> SessionData:
    now_iso = datetime.now(timezone.utc).isoformat()
    return SessionData(
        session_id=session_id,
        agent_name="demo",
        model_name="gpt-4",
        session_type="job",
        history_messages=[],
        mailbox=[],
        variables={},
        created_at=now_iso,
        updated_at=updated_at or now_iso,
        state=state,
        archived_at=archived_at,
        timeline=[],
        context_trace={},
        revision=1,
    )


@pytest.mark.asyncio
async def test_mark_session_archived_sets_state_and_timestamp(tmp_path: Path):
    manager = SessionManager(tmp_path)
    session_id = "job_demo_1"
    session = _make_job_session(session_id, state="active")
    await manager.persistence.save_data(session)

    ok = await manager.mark_session_archived(session_id)
    assert ok is True

    loaded = await manager.load_session(session_id)
    assert loaded is not None
    assert loaded.state == "archived"
    assert isinstance(loaded.archived_at, str) and loaded.archived_at


@pytest.mark.asyncio
async def test_cleanup_archived_job_sessions_by_retention_and_max_count(tmp_path: Path):
    manager = SessionManager(tmp_path)
    now = datetime.now(timezone.utc)

    old_archived = _make_job_session(
        "job_old_archived",
        state="archived",
        archived_at=(now - timedelta(days=10)).isoformat(),
        updated_at=(now - timedelta(days=10)).isoformat(),
    )
    mid_archived = _make_job_session(
        "job_mid_archived",
        state="archived",
        archived_at=(now - timedelta(days=2)).isoformat(),
        updated_at=(now - timedelta(days=2)).isoformat(),
    )
    new_archived = _make_job_session(
        "job_new_archived",
        state="archived",
        archived_at=(now - timedelta(days=1)).isoformat(),
        updated_at=(now - timedelta(days=1)).isoformat(),
    )
    active_job = _make_job_session("job_active", state="active")

    for session in (old_archived, mid_archived, new_archived, active_job):
        await manager.persistence.save_data(session)

    removed = await manager.cleanup_archived_job_sessions(retention_days=7, max_sessions=1)
    assert removed == 2

    assert await manager.load_session("job_old_archived") is None
    assert await manager.load_session("job_mid_archived") is None
    assert await manager.load_session("job_new_archived") is not None
    active_loaded = await manager.load_session("job_active")
    assert active_loaded is not None
    assert active_loaded.state == "active"

