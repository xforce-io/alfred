import pytest

from src.everbot.core.session.session import SessionData, SessionManager


def _make_session_data(session_id: str, *, history: list, timeline: list, context_trace: dict):
    return SessionData(
        session_id=session_id,
        agent_name="demo_agent",
        model_name="gpt-4",
        history_messages=history,
        variables={},
        created_at="2026-02-09T09:00:00",
        updated_at="2026-02-09T09:00:01",
        timeline=timeline,
        context_trace=context_trace,
    )


@pytest.mark.asyncio
async def test_migrate_heartbeat_session_when_primary_missing(tmp_path):
    manager = SessionManager(tmp_path)
    heartbeat_id = "heartbeat_demo_agent"
    primary_id = manager.get_primary_session_id("demo_agent")

    heartbeat_data = _make_session_data(
        heartbeat_id,
        history=[{"role": "assistant", "content": "HEARTBEAT_OK"}],
        timeline=[{"type": "turn_end", "timestamp": "2026-02-09T09:00:01"}],
        context_trace={"execution_summary": {"total_stages": 1}},
    )
    await manager.persistence.save_data(heartbeat_data)

    changed = await manager.migrate_legacy_sessions_for_agent("demo_agent")
    assert changed is True

    migrated = await manager.load_session(primary_id)
    assert migrated is not None
    assert migrated.history_messages == heartbeat_data.history_messages
    assert migrated.context_trace == heartbeat_data.context_trace
    assert "heartbeat_demo_agent" in migrated.variables.get("_migrated_from", [])

    heartbeat_path = manager.persistence._get_session_path(heartbeat_id)
    assert not heartbeat_path.exists()
    backups = list(tmp_path.glob("heartbeat_demo_agent.json.migrated_*"))
    assert backups, "Expected migrated backup for heartbeat session file."


@pytest.mark.asyncio
async def test_migrate_keeps_primary_history_and_merges_timeline(tmp_path):
    manager = SessionManager(tmp_path)
    primary_id = manager.get_primary_session_id("demo_agent")
    heartbeat_id = "heartbeat_demo_agent"

    primary_data = _make_session_data(
        primary_id,
        history=[{"role": "user", "content": "hello"}],
        timeline=[{"type": "turn_start", "timestamp": "2026-02-09T09:00:00"}],
        context_trace={"execution_summary": {"total_stages": 2}},
    )
    heartbeat_data = _make_session_data(
        heartbeat_id,
        history=[{"role": "assistant", "content": "hb"}],
        timeline=[{"type": "turn_end", "timestamp": "2026-02-09T09:00:02"}],
        context_trace={"execution_summary": {"total_stages": 1}},
    )
    await manager.persistence.save_data(primary_data)
    await manager.persistence.save_data(heartbeat_data)

    changed = await manager.migrate_legacy_sessions_for_agent("demo_agent")
    assert changed is True

    migrated = await manager.load_session(primary_id)
    assert migrated is not None
    # Keep existing primary history stable to avoid context surprises.
    assert migrated.history_messages == primary_data.history_messages
    assert len(migrated.timeline) == 2
    assert [evt["type"] for evt in migrated.timeline] == ["turn_start", "turn_end"]
    assert "heartbeat_demo_agent" in migrated.variables.get("_migrated_from", [])
