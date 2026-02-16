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
