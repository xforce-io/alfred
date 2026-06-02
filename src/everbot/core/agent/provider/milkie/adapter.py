"""Map milkie native SSE events onto alfred :class:`TurnEvent`.

垂直切片(纯文本对话)只需两类事件:

* ``message_delta {text}``           → :attr:`TurnEventType.LLM_DELTA`
* ``agent.run.completed {status,…}`` → 终态:
    * ``completed`` / ``interrupted`` → :attr:`TurnEventType.TURN_COMPLETE`
    * ``error``                       → :attr:`TurnEventType.TURN_ERROR`

其余事件(``error`` 帧、``agent.run.started``、``tool.*``、未知名…)在本切片
里返回 ``None`` 优雅忽略 —— 终态的错误信息由 ``agent.run.completed`` 的
``error`` 字段携带,无需 ``error`` 帧重复。pid 合成、stage 分类等更完整的映射
留待后续阶段(见 xforce-io/alfred#32)。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from everbot.core.runtime.turn_policy import TurnEvent, TurnEventType


def milkie_event_to_turn_event(event: str, data: Dict[str, Any]) -> Optional[TurnEvent]:
    if event == "message_delta":
        return TurnEvent(type=TurnEventType.LLM_DELTA, content=data.get("text") or "")

    if event == "agent.run.completed":
        status = data.get("status") or ""
        output = data.get("output") or ""
        if status == "error":
            return TurnEvent(
                type=TurnEventType.TURN_ERROR,
                error=data.get("error") or "",
                status=status,
                answer=output,
            )
        return TurnEvent(
            type=TurnEventType.TURN_COMPLETE,
            answer=output,
            status=status,
        )

    return None
