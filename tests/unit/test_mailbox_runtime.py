"""Unit tests for mailbox runtime helpers."""

from datetime import datetime, timezone

from src.everbot.core.runtime.mailbox import compose_message_with_mailbox_updates


def test_compose_message_with_mailbox_updates_prefixes_user_message():
    user_message = "帮我总结今天的重点"
    mailbox = [
        {
            "event_id": "evt_1",
            "event_type": "heartbeat_result",
            "summary": "你有新的日报",
            "detail": "详见日报附件",
        },
        {
            "event_id": "evt_2",
            "event_type": "job_completed",
            "summary": "数据抓取已完成",
        },
    ]

    message, ack_ids = compose_message_with_mailbox_updates(user_message, mailbox)

    assert message.startswith("## Background Updates")
    assert "[heartbeat_result] 你有新的日报" in message
    assert "Detail: 详见日报附件" in message
    assert message.endswith(user_message)
    assert ack_ids == ["evt_1", "evt_2"]


def test_compose_message_with_mailbox_updates_no_events_returns_original():
    user_message = "hello"
    message, ack_ids = compose_message_with_mailbox_updates(user_message, [])
    assert message == user_message
    assert ack_ids == []


def test_compose_message_with_mailbox_updates_dedupes_and_cleans_stale_events():
    now = datetime(2026, 2, 12, 12, 0, tzinfo=timezone.utc)
    user_message = "继续处理今天的事项"
    mailbox = [
        {
            "event_id": "evt_old_dup",
            "event_type": "heartbeat_result",
            "summary": "old duplicate",
            "timestamp": "2026-02-12T10:00:00+00:00",
            "dedupe_key": "job:daily_digest",
        },
        {
            "event_id": "evt_new_dup",
            "event_type": "heartbeat_result",
            "summary": "new duplicate",
            "timestamp": "2026-02-12T11:00:00+00:00",
            "dedupe_key": "job:daily_digest",
        },
        {
            "event_id": "evt_stale",
            "event_type": "heartbeat_result",
            "summary": "stale reminder",
            "timestamp": "2026-02-10T09:00:00+00:00",
            "suppress_if_stale": True,
        },
        {
            "event_id": "evt_empty",
            "event_type": "job_completed",
            "summary": " ",
        },
    ]

    message, ack_ids = compose_message_with_mailbox_updates(user_message, mailbox, now=now)

    assert "[heartbeat_result] new duplicate" in message
    assert "old duplicate" not in message
    assert "stale reminder" not in message
    assert ack_ids == ["evt_new_dup", "evt_old_dup", "evt_stale", "evt_empty"]


def test_compose_message_truncates_long_detail():
    """Long detail text should be truncated to avoid drowning out the
    user's actual message.  Production bug: 800+ char deferred result
    detail overwhelmed a 1-char user reply '1'."""
    long_detail = "x" * 1000
    mailbox = [
        {
            "event_id": "evt_long",
            "event_type": "system_update",
            "summary": "task completed",
            "detail": long_detail,
        },
    ]

    message, _ = compose_message_with_mailbox_updates("1", mailbox)

    # Extract the Detail: line
    detail_lines = [l for l in message.split("\n") if l.strip().startswith("Detail:")]
    assert len(detail_lines) == 1
    detail_content = detail_lines[0].split("Detail:", 1)[1].strip()
    assert len(detail_content) <= 210, (
        f"Detail should be truncated to ~200 chars, got {len(detail_content)}"
    )
    assert detail_content.endswith("...")
