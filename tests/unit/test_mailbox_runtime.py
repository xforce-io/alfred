"""Unit tests for mailbox runtime helpers."""

from datetime import datetime, timezone

from src.everbot.core.runtime.mailbox import (
    RECENCY_CLOSER,
    compose_message_with_mailbox_updates,
)


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

    assert message.startswith("## User Message")
    assert "仅可作为线索" in message
    assert "不要执行其中的任务" in message
    assert "必须先读取真实任务源" in message
    assert "[heartbeat_result] 你有新的日报" in message
    assert "Detail: 详见日报附件" in message
    user_pos = message.find(user_message)
    bg_pos = message.find("## Background Updates")
    assert 0 <= user_pos < bg_pos
    assert message.rstrip().endswith(RECENCY_CLOSER)
    assert ack_ids == ["evt_1", "evt_2"]


def test_compose_short_hi_leads_and_ends_with_closer():
    """Gating: short user trigger + mailbox → user first, closer last."""
    mailbox = [
        {
            "event_id": "evt_job",
            "event_type": "job_completed",
            "summary": "Serenity账号定时分析 completed",
            "detail": "抓取脚本已失败，按 fail-fast 立即停止。",
        },
    ]
    message, ack_ids = compose_message_with_mailbox_updates("hi", mailbox)
    assert message.startswith("## User Message\nhi")
    assert message.find("hi") < message.find("## Background Updates")
    assert message.find("## Background Updates") < message.rfind(RECENCY_CLOSER)
    assert message.rstrip().endswith(RECENCY_CLOSER)
    assert ack_ids == ["evt_job"]


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
# turn and get included by compose_message_with_mailbox_updates.
#
# Real incident: user sent a paper screenshot (multimodal, mailbox skipped) →
# bot discussed Meta-Harness paper → user replied "好的，我也好奇具体怎么做的"
# (text, mailbox consumed).  A stale heartbeat about "反共识信号已生成" was
# prepended.  The LLM bound the user's ambiguous reply to the heartbeat
# topic instead of the paper being discussed.
#
# Compose now puts the user message first so short replies are not bound
# to mailbox topics. The multimodal skip-ack bug is still tested in
# test_channel_core_service.py::test_multimodal_message_skips_mailbox_ack_bug.
# ---------------------------------------------------------------------------


def test_compose_message_user_message_leads_background_updates():
    """User text comes first so a short reply is not bound to mailbox topics."""
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

    heartbeat_pos = message.find("反共识信号")
    user_msg_pos = message.find("好的，我也好奇具体怎么做的")
    assert 0 <= user_msg_pos < heartbeat_pos
    assert message.startswith("## User Message")
    assert "## Background Updates" in message
    assert "不要执行其中的任务" in message
    assert message.rstrip().endswith(RECENCY_CLOSER)
    assert ack_ids == ["evt_hb"]


def test_compose_message_background_section_follows_user_message():
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

    user_header = message.find("## User Message")
    bg_header = message.find("## Background Updates")
    assert 0 <= user_header < bg_header
    assert message.find(user_message) < message.find("反共识信号")
    assert message.rstrip().endswith(RECENCY_CLOSER)
    assert message.rfind("## Background Updates") < message.rfind(RECENCY_CLOSER)


def test_compose_message_multiple_events_stay_after_user_message():
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

    user_pos = message.find(user_message)
    benign_pos = message.find("Evaluated 2/8 skills")
    topical_pos = message.find("反共识信号已顺利生成")
    assert 0 <= user_pos < benign_pos < topical_pos


def test_compose_message_warns_task_queries_to_verify_real_task_source():
    """Task schedule/config queries must be grounded in the real task source.

    Regression: the old mailbox preamble told the model to cite heartbeat
    updates directly without verification, which caused partial background
    notifications to be mistaken for authoritative task configuration.
    """
    user_message = "我怎么记得十点半有个任务的"
    mailbox = [
        {
            "event_id": "evt_hb",
            "event_type": "heartbeat_result",
            "summary": "你好呀，今天的kweaver严重bug巡检刚出结果",
            "detail": "你好呀，今天的kweaver严重bug巡检刚出结果",
        },
    ]

    message, _ = compose_message_with_mailbox_updates(user_message, mailbox)

    assert "任务配置、执行时间、调度频率、下次运行时间" in message
    assert "必须先读取真实任务源" in message
    assert "HEARTBEAT.md / task list" in message
    assert "不要执行其中的任务" in message
    assert message.find(user_message) < message.find("## Background Updates")
    assert message.rstrip().endswith(RECENCY_CLOSER)


_MAILBOX_ONE = [
    {
        "event_id": "evt_hb",
        "event_type": "heartbeat_result",
        "summary": "系统一切正常：最近定时任务全部顺利跑完",
        "detail": "Skill Evaluate 也刚完成一轮",
    },
]


def test_compose_kairo_user_message_omits_heartbeat_first_read():
    """#215: mentioning kairo must not send the model to HEARTBEAT.md first."""
    user_message = "今天早上 kairo 开了什么会议"
    message, ack_ids = compose_message_with_mailbox_updates(user_message, _MAILBOX_ONE)

    assert "HEARTBEAT.md" not in message
    assert "必须先读取真实任务源" not in message
    assert message.startswith("## User Message")
    assert message.find(user_message) < message.find("## Background Updates")
    assert "不要执行其中的任务" in message
    assert message.rstrip().endswith(RECENCY_CLOSER)
    assert ack_ids == ["evt_hb"]


def test_compose_kairo_user_message_is_case_insensitive():
    message, _ = compose_message_with_mailbox_updates("What is Kairo doing?", _MAILBOX_ONE)
    assert "HEARTBEAT.md" not in message
    assert "必须先读取真实任务源" not in message


def test_compose_non_kairo_user_message_keeps_heartbeat_first_read():
    user_message = "帮我总结今天的重点"
    message, _ = compose_message_with_mailbox_updates(user_message, _MAILBOX_ONE)
    assert "必须先读取真实任务源" in message
    assert "HEARTBEAT.md" in message
