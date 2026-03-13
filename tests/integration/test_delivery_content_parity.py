"""TDD tests for delivery content parity between Telegram and LLM context.

Problem: the system produces two different representations of the same
heartbeat result (inspector push_message vs cron job result), delivered as
independent events.  Users see push_message on Telegram but the LLM only
sees the job result — with a completely different title.  When users
reference the push_message wording, the LLM cannot match it.

Target architecture:
  - Inspector is the sole delivery gate.
  - Cron job stores result but does NOT deliver to channels directly.
  - Inspector generates a delivery event carrying both:
      summary = push_message (for Telegram notification + LLM title)
      detail  = job result   (for LLM context, follow-up questions)
  - Channel sends summary to Telegram, deposits event to mailbox.
  - compose_message_with_mailbox_updates renders summary as the title line,
    so the LLM sees the same wording the user saw on Telegram.

Current bugs (documented in TestCurrentBehavior):
  1. emit_push_message sets detail=push_message (same as summary), not job result
  2. Cron job emits its own event to channels independently
  3. Two events with different content for the same logical result
"""

import asyncio
from unittest.mock import patch


from src.everbot.core.models.system_event import build_system_event
from src.everbot.core.runtime.inspector import InspectionResult, emit_push_message
from src.everbot.core.runtime.mailbox import compose_message_with_mailbox_updates


# ── Constants ─────────────────────────────────────────────────────────

PUSH_MESSAGE = "【系统健康快照】轨迹审查发现3项高优先级信号"
JOB_RESULT = (
    "## Routine Task Summary: `routine_ce36cfea` - 会话轨迹健康检测\n\n"
    "**执行状态**: ✅ 已完成\n\n"
    "### 关键发现 (严重级别)\n\n"
    "#### 🔴 High 严重问题 (3项)\n"
    "1. **频繁工具级失败** - 13次工具错误消息\n"
    "2. **日志错误密度高** - LLM 调用3次重试后失败\n"
    "3. **潜在的重复响应循环** - 3次重复助手消息"
)


# ── Current Behavior: documents the existing content parity gap ──────


class TestCurrentBehavior:
    """Document the current broken behavior where push_message and job
    result are delivered as separate events with mismatched content."""

    def test_emit_push_message_sets_detail_equal_to_summary(self):
        """Currently emit_push_message sets detail=push_message, which is
        the same as summary.  The job result is lost."""
        emitted_events = []

        async def capture_emit(source_session_id, data, **kwargs):
            emitted_events.append(data)

        with patch("src.everbot.core.runtime.events.emit", side_effect=capture_emit):
            asyncio.run(emit_push_message(
                PUSH_MESSAGE,
                primary_session_id="heartbeat_session_demo_agent",
                agent_name="demo_agent",
                run_id="run_001",
            ))

        assert len(emitted_events) == 1
        event = emitted_events[0]
        # BUG: detail == push_message; job result is absent
        assert event["detail"] == PUSH_MESSAGE, (
            "Current behavior: detail is push_message, not job result"
        )
        assert "Routine Task Summary" not in event["detail"], (
            "Current behavior: job result is not in the delivery event"
        )

    def test_emit_push_message_without_detail_falls_back(self):
        """When detail is not provided, emit_push_message falls back to
        using push_message as detail (backward compatibility)."""
        emitted_events = []

        async def capture_emit(source_session_id, data, **kwargs):
            emitted_events.append(data)

        with patch("src.everbot.core.runtime.events.emit", side_effect=capture_emit):
            asyncio.run(emit_push_message(
                PUSH_MESSAGE,
                primary_session_id="heartbeat_session_demo_agent",
                agent_name="demo_agent",
                run_id="run_001",
                # no detail= argument
            ))

        event = emitted_events[0]
        assert event["detail"] == PUSH_MESSAGE, (
            "Without detail arg, should fall back to push_message"
        )


# ── Target Behavior: content parity after the fix ────────────────────


class TestTargetDeliveryEvent:
    """After the fix, the delivery event must carry summary=push_message
    and detail=job_result as distinct fields."""

    def test_emit_push_message_carries_job_result_as_detail(self):
        """emit_push_message must accept and forward the underlying job
        result so channels can deposit it as detail in the mailbox event."""
        emitted_events = []

        async def capture_emit(source_session_id, data, **kwargs):
            emitted_events.append(data)

        with patch("src.everbot.core.runtime.events.emit", side_effect=capture_emit):
            asyncio.run(emit_push_message(
                PUSH_MESSAGE,
                primary_session_id="heartbeat_session_demo_agent",
                agent_name="demo_agent",
                run_id="run_001",
                detail=JOB_RESULT,  # NEW: pass job result as detail
            ))

        assert len(emitted_events) == 1
        event = emitted_events[0]
        assert event["summary"] == PUSH_MESSAGE[:300], (
            "summary must be the push_message (for Telegram notification)"
        )
        assert event["detail"] == JOB_RESULT, (
            "detail must be the job result (for LLM context)"
        )
        assert event["summary"] != event["detail"], (
            "summary and detail must be different"
        )

    def test_inspection_result_carries_delivery_detail(self):
        """InspectionResult must carry delivery_detail so the heartbeat
        runner can pass it through to emit_push_message."""
        result = InspectionResult(
            heartbeat_ok=True,
            push_message=PUSH_MESSAGE,
            delivery_detail=JOB_RESULT,  # NEW field
            output="HEARTBEAT_OK",
        )
        assert result.push_message == PUSH_MESSAGE
        assert result.delivery_detail == JOB_RESULT


class TestTargetContentParity:
    """End-to-end: the mailbox event from the target delivery must produce
    a Background Updates block where the LLM can find the push_message
    wording AND the job result detail."""

    def test_user_references_push_message_title(self):
        """When user asks about '系统健康快照', the LLM must see that exact
        phrase in its context (from the summary field of the mailbox event)."""
        event = build_system_event(
            event_type="heartbeat_result",
            source_session_id="heartbeat_session_demo_agent",
            summary=PUSH_MESSAGE,
            detail=JOB_RESULT,
            dedupe_key="heartbeat:demo_agent:reflect",
        )
        composed, _ = compose_message_with_mailbox_updates(
            "系统健康快照呢，提到的是啥", [event],
        )
        # Title line must contain push_message wording
        assert "系统健康快照" in composed
        # Detail must also be present (truncated is fine)
        assert "Detail:" in composed

    def test_summary_is_title_not_detail(self):
        """The title line in Background Updates must use summary, not detail."""
        event = build_system_event(
            event_type="heartbeat_result",
            source_session_id="heartbeat_session_demo_agent",
            summary=PUSH_MESSAGE,
            detail=JOB_RESULT,
            dedupe_key="heartbeat:demo_agent:reflect",
        )
        composed, _ = compose_message_with_mailbox_updates("test", [event])
        title_lines = [line for line in composed.split("\n") if line.startswith("- [")]
        assert len(title_lines) == 1
        assert "系统健康快照" in title_lines[0]
        assert "Routine Task Summary" not in title_lines[0]

    def test_single_event_not_dual(self):
        """Only ONE delivery event should exist for a single job result,
        not two (one from cron + one from inspector)."""
        # Target: inspector produces one combined event
        events = [
            build_system_event(
                event_type="heartbeat_result",
                source_session_id="heartbeat_session_demo_agent",
                summary=PUSH_MESSAGE,
                detail=JOB_RESULT,
                dedupe_key="heartbeat:demo_agent:health_check",
            ),
        ]
        composed, ack_ids = compose_message_with_mailbox_updates("test", events)
        title_lines = [line for line in composed.split("\n") if line.startswith("- [")]
        assert len(title_lines) == 1

    def test_dedup_keeps_latest_when_dual_events_exist(self):
        """If both cron result and inspector push arrive (degraded path),
        dedup should keep the latest (inspector's event with push_message)."""
        cron_event = build_system_event(
            event_type="heartbeat_result",
            source_session_id="heartbeat_session_demo_agent",
            summary=JOB_RESULT[:100],
            detail=JOB_RESULT,
            dedupe_key="heartbeat:demo_agent:health_check",
        )
        inspector_event = build_system_event(
            event_type="heartbeat_result",
            source_session_id="heartbeat_session_demo_agent",
            summary=PUSH_MESSAGE,
            detail=JOB_RESULT,
            dedupe_key="heartbeat:demo_agent:health_check",
        )
        composed, _ = compose_message_with_mailbox_updates(
            "test", [cron_event, inspector_event],
        )
        title_lines = [line for line in composed.split("\n") if line.startswith("- [")]
        assert len(title_lines) == 1
        assert "系统健康快照" in title_lines[0]
