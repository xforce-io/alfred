"""Unit tests for system event serialization compatibility."""

from src.everbot.core.models.system_event import SystemEvent, build_system_event


def test_build_system_event_includes_schema_fields():
    event = build_system_event(
        event_type="heartbeat_result",
        source_session_id="heartbeat_session_demo",
        summary="hello",
        detail="world",
    )
    assert event["schema"] == "everbot.system_event"
    assert event["schema_version"] == 1
    assert event["event_type"] == "heartbeat_result"


def test_system_event_from_dict_keeps_backward_compat_defaults():
    legacy = {
        "event_id": "evt_1",
        "event_type": "job_completed",
        "source_session_id": "job_1",
        "timestamp": "2026-02-12T00:00:00+00:00",
        "summary": "done",
    }
    event = SystemEvent.from_dict(legacy)
    assert event.schema == "everbot.system_event"
    assert event.schema_version == 1
    assert event.event_id == "evt_1"

