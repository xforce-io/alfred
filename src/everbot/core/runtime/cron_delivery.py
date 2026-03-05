"""CronDelivery — result delivery for cron task execution.

Extracted from HeartbeatRunner to decouple delivery logic from
task execution, enabling independent testing and reuse.
"""

import inspect
import logging
from datetime import datetime
from typing import Any, Callable, Optional

from ..models.system_event import build_system_event

logger = logging.getLogger(__name__)


class CronDelivery:
    """Delivers cron task execution results to the primary session.

    Handles:
    - HEARTBEAT_OK suppression (avoid polluting user mailbox with ack-only results)
    - Mailbox event deposit (SystemEvent for primary session)
    - History injection (assistant message in primary session)
    - Realtime push (SSE / Telegram)
    """

    SUMMARY_MAX_CHARS: int = 500

    def __init__(
        self,
        *,
        session_manager: Any,
        primary_session_id: str,
        heartbeat_session_id: str,
        agent_name: str,
        ack_max_chars: int = 300,
        broadcast_scope: str = "agent",
        realtime_push: bool = True,
        on_result: Optional[Callable] = None,
    ):
        self.session_manager = session_manager
        self.primary_session_id = primary_session_id
        self.heartbeat_session_id = heartbeat_session_id
        self.agent_name = agent_name
        self.ack_max_chars = ack_max_chars
        self.broadcast_scope = broadcast_scope
        self.realtime_push = realtime_push
        self.on_result = on_result

    def should_deliver(self, response: str) -> bool:
        """Determine if a heartbeat result should be delivered to the user.

        Suppression rules (HEARTBEAT_OK mechanism):
        - No HEARTBEAT_OK token → deliver
        - Token at start/end AND remaining content <= ack_max_chars → suppress
        - Token at start/end BUT remaining > ack_max_chars → deliver
        - Token in middle → deliver
        """
        stripped = response.strip()
        token = "HEARTBEAT_OK"

        if token not in stripped:
            return True

        if stripped.startswith(token):
            remaining = stripped[len(token):].strip()
        elif stripped.endswith(token):
            remaining = stripped[:-len(token)].strip()
        else:
            return True

        return len(remaining) > self.ack_max_chars

    async def deliver_result(self, result: str, run_id: str, *, heartbeat_mode: str = "unknown") -> bool:
        """Deliver a cron task result to the primary session.

        Returns True if the result was actually delivered (not suppressed).
        """
        if not self.should_deliver(result):
            return False

        await self.inject_to_history(result, run_id)
        await self.deposit_event(result, run_id, heartbeat_mode=heartbeat_mode)

        if self.realtime_push:
            await self._emit_realtime(result, run_id)

        return True

    async def inject_to_history(self, result: str, run_id: str) -> bool:
        """Inject result as an assistant message in primary session history."""
        prefixed_content = (
            "[此消息由心跳系统自动执行例行任务生成]\n\n"
            + result
        )
        message = {
            "role": "assistant",
            "content": prefixed_content,
            "metadata": {
                "source": "heartbeat",
                "run_id": run_id,
                "injected_at": datetime.now().isoformat(),
            },
        }
        ok = await self.session_manager.inject_history_message(
            self.primary_session_id,
            message,
            timeout=5.0,
            blocking=True,
        )
        if not ok:
            logger.warning("[%s] Failed to inject heartbeat result to history", self.agent_name)
        return ok

    async def deposit_event(self, content: str, run_id: str, *, heartbeat_mode: str = "unknown") -> bool:
        """Deposit result into primary-session mailbox as SystemEvent."""
        dedupe_key = f"heartbeat:{self.agent_name}:{heartbeat_mode}"
        event = build_system_event(
            event_type="heartbeat_result",
            source_session_id=self.heartbeat_session_id,
            summary=content[:self.SUMMARY_MAX_CHARS],
            detail=content,
            artifacts=[],
            priority=0,
            suppress_if_stale=True,
            dedupe_key=dedupe_key,
        )
        ok = await self.session_manager.deposit_mailbox_event(
            self.primary_session_id,
            event,
            timeout=5.0,
            blocking=True,
        )
        if not ok:
            logger.warning("[%s] Failed to deposit heartbeat event to mailbox", self.agent_name)
        return ok

    async def deposit_job_event(
        self,
        *,
        event_type: str,
        source_session_id: str,
        summary: str,
        detail: Optional[str],
        run_id: str,
    ) -> bool:
        """Deposit isolated job result to primary mailbox."""
        event = build_system_event(
            event_type=event_type,
            source_session_id=source_session_id,
            summary=summary[:self.SUMMARY_MAX_CHARS],
            detail=detail,
            artifacts=[],
            priority=0,
            suppress_if_stale=False,
            dedupe_key=f"{event_type}:{self.agent_name}:{run_id}:{source_session_id}",
        )
        return await self.session_manager.deposit_mailbox_event(
            self.primary_session_id,
            event,
            timeout=5.0,
            blocking=True,
        )

    async def _emit_realtime(self, result: str, run_id: str) -> None:
        """Emit realtime push event (SSE / Telegram)."""
        from .events import emit

        message = {
            "type": "message",
            "role": "assistant",
            "content": result,
            "summary": result[:self.SUMMARY_MAX_CHARS],
            "detail": result,
            "source_type": "heartbeat_delivery",
            "run_id": run_id,
            "deliver": True,
        }
        await emit(
            self.primary_session_id,
            message,
            agent_name=self.agent_name,
            scope=self.broadcast_scope,
            source_type="heartbeat_delivery",
            run_id=run_id,
        )

    async def emit_callback(self, result: str) -> None:
        """Dispatch result to on_result callback (sync/async)."""
        if not self.on_result:
            return
        callback_result = self.on_result(self.agent_name, result)
        if inspect.isawaitable(callback_result):
            await callback_result
