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
    user's actual message.  Cap at 2000 chars to preserve structured reports."""
    long_detail = "x" * 5000
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
    detail_lines = [line for line in message.split("\n") if line.strip().startswith("Detail:")]
    assert len(detail_lines) == 1
    detail_content = detail_lines[0].split("Detail:", 1)[1].strip()
    assert len(detail_content) <= 2010, (
        f"Detail should be truncated to ~2000 chars, got {len(detail_content)}"
    )
    assert detail_content.endswith("...")


# ---------------------------------------------------------------------------
# Intent hijack via stale mailbox events.
#
# Root cause: multimodal messages (images) skip mailbox consumption in
# process_message (core_service.py L236-239), so heartbeat events deposited
# before the multimodal turn are NOT acked.  They survive into the next text
# turn and get prepended to the user message by compose_message_with_mailbox_updates.
#
# Real incident: user sent a paper screenshot (multimodal, mailbox skipped) →
# bot discussed Meta-Harness paper → user replied "好的，我也好奇具体怎么做的"
# (text, mailbox consumed).  A stale heartbeat about "反共识信号已生成" was
# prepended.  The LLM bound the user's ambiguous reply to the heartbeat
# topic instead of the paper being discussed.
#
# The tests below document the message format that enables this hijack.
# The actual bug is tested in test_channel_core_service.py::
# test_multimodal_message_skips_mailbox_ack_bug.
# ---------------------------------------------------------------------------


def test_compose_message_heartbeat_placed_before_user_message():
    """Heartbeat updates are always placed ABOVE the user's message.

    When the heartbeat summary introduces a specific topic and the user
    message is short/ambiguous, the LLM may bind the user's intent to
    the heartbeat topic rather than the conversation history.

    This test documents the current (problematic) message structure.
    """
    user_message = "好的，我也好奇具体怎么做的"
    mailbox = [
        {
            "event_id": "evt_hb",
            "event_type": "heartbeat_result",
            "summary": "每日反共识信号已顺利生成，包含伊朗危机、DRAM周期、高盛喊单三个核心信号",
            "detail": "每日反共识信号已顺利生成，包含伊朗危机、DRAM周期、高盛喊单三个核心信号",
        },
    ]

    message, ack_ids = compose_message_with_mailbox_updates(user_message, mailbox)

    # Current structure: heartbeat comes first, user message last
    lines = message.split("\n")

    # The heartbeat topic "反共识信号" appears BEFORE the user's message
    heartbeat_pos = message.find("反共识信号")
    user_msg_pos = message.find("好的，我也好奇具体怎么做的")
    assert heartbeat_pos < user_msg_pos, (
        "Heartbeat content should appear before user message in current format"
    )

    # The composed message has the structure:
    #   ## Background Updates
    #   (说明文字)
    #   - [heartbeat_result] 反共识信号...
    #     Detail: ...
    #
    #   ## User Message
    #   好的，我也好奇具体怎么做的
    assert "## Background Updates" in message
    assert "## User Message" in message

    # PROBLEM: The "反共识信号" heartbeat is the closest context to the
    # user's ambiguous "具体怎么做的", making the LLM likely to treat the
    # heartbeat topic as what the user is asking about.


def test_compose_message_heartbeat_topic_proximity_to_user_message():
    """Measure how close the heartbeat content is to the user message.

    The shorter the distance, the more likely the LLM treats the heartbeat
    topic as the referent for the user's pronoun/reference.
    """
    user_message = "具体怎么做的"
    mailbox = [
        {
            "event_id": "evt_1",
            "event_type": "heartbeat_result",
            "summary": "Evaluated 2/8 skills",
        },
        {
            "event_id": "evt_2",
            "event_type": "heartbeat_result",
            "summary": "每日反共识信号已顺利生成",
            "detail": "每日反共识信号已顺利生成，包含伊朗危机等三个核心信号",
        },
    ]

    message, _ = compose_message_with_mailbox_updates(user_message, mailbox)

    # The last heartbeat event is closest to "## User Message"
    bg_section_end = message.find("## User Message")
    last_heartbeat_end = message.rfind("反共识信号", 0, bg_section_end)
    assert last_heartbeat_end != -1, "Heartbeat topic should be present before user message"

    # The gap between last heartbeat content and user message header is small
    gap = bg_section_end - last_heartbeat_end
    # With current format this gap is typically < 100 chars (just a blank line + header)
    assert gap < 150, (
        f"Gap between heartbeat topic and user message is {gap} chars — "
        f"close enough for LLM to mis-attribute user intent"
    )


def test_compose_message_multiple_events_last_one_closest_to_user():
    """When multiple heartbeat events are included, the LAST one is closest
    to the user message and most likely to hijack intent.

    In the real incident, a benign "Evaluated 2/8 skills" was followed by
    a topical "反共识信号已顺利生成" — the latter was closest to the user's
    message and became the mis-attributed referent.
    """
    user_message = "好的，我也好奇具体怎么做的"
    mailbox = [
        {
            "event_id": "evt_benign",
            "event_type": "heartbeat_result",
            "summary": "Evaluated 2/8 skills",
        },
        {
            "event_id": "evt_topical",
            "event_type": "heartbeat_result",
            "summary": "每日反共识信号已顺利生成",
            "detail": "每日反共识信号已顺利生成，包含三个核心信号，系统运行正常",
        },
    ]

    message, _ = compose_message_with_mailbox_updates(user_message, mailbox)

    user_section_start = message.find("## User Message")
    # The topical event (反共识) appears after the benign one and closer to user
    benign_pos = message.find("Evaluated 2/8 skills")
    topical_pos = message.find("反共识信号已顺利生成")
    assert benign_pos < topical_pos < user_section_start, (
        "Events are ordered chronologically; last event is closest to user message"
    )
