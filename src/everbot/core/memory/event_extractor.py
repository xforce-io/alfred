"""LLM-based event memory extraction from conversation history.

Event extractor produces time-anchored memories — decisions made,
todos surfaced, incidents observed, milestones reached. It is paired
with ``EventStore`` to persist results under ``events/YYYY-MM.md``.

Profile extraction (long-lived user portrait — preferences, facts,
workflows) lives in a separate ``profile_extractor`` module.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import _extractor_helpers as _helpers
from .models import MemoryEntry, new_id

logger = logging.getLogger(__name__)

_EVENT_CATEGORIES = {"decision", "todo", "incident", "interaction", "milestone"}

# importance → initial score (kept identical to profile mapping so the
# two layers feel consistent under prompt injection)
_IMPORTANCE_SCORES = {
    "high": 0.8,
    "medium": 0.6,
    "low": 0.4,
}

_EVENT_PROMPT = """\
你是一个事件记忆抽取器。从下面的对话中提取**这次会话中发生的事件**。

## 应该提取（关于时点的事实）
- **decision**: 用户做出的具体一次性决定（带时间锚定）
- **todo**: 用户提到的待办、deadline、跟进事项
- **incident**: 出现的问题、报错、异常、故障
- **interaction**: 重要的交互节点（达成共识、用户表达情绪等）
- **milestone**: 项目/任务的进度推进

## 不应该提取
- 用户的长期偏好、习惯、画像（那是画像抽取器的职责）
- 助手的回答内容、搜索结果
- 工具调用细节
- 一次性问答（"今天天气怎么样"）
- 已经在过去会话中记录过的同一个事件

## 输入
当前会话发生时间: {session_time}

## 对话内容
{messages_text}

## 输出
严格 JSON 格式：

```json
{{
  "new_events": [
    {{
      "content": "一句话描述事件",
      "category": "decision|todo|incident|interaction|milestone",
      "event_at": "ISO8601 时间戳；不明确就用 {session_time}",
      "importance": "high|medium|low",
      "due_at": "仅 todo 用，可选；ISO8601 截止时间"
    }}
  ]
}}
```

注意：
- **宁缺毋滥**：大多数对话只产生 0~2 条事件；没有事件就返回空 new_events
- category 必须是上述 5 类之一
- importance: high=关键节点（重要决定/严重故障）, medium=普通事件, low=次要线索
- **due_at 仅在 category=todo 且对话中明确说了截止时间时填**；其他情况省略
"""


@dataclass
class EventExtractResult:
    """Result of event extraction — ready-to-persist MemoryEntry instances.

    Unlike ``ProfileExtractor``'s result, this carries no reinforcement
    list: events are append-only, never collapsed into existing entries.
    """
    new_events: List[MemoryEntry] = field(default_factory=list)


class EventExtractor:
    """Extract time-anchored event memories from conversation using LLM."""

    def __init__(self, context: Any):
        self._context = context

    async def extract(
        self,
        messages: List[Dict[str, Any]],
        session_id: str = "",
        session_time: Optional[str] = None,
    ) -> EventExtractResult:
        """Extract events from conversation messages.

        Args:
            messages: Conversation history (list of role/content dicts).
            session_id: Source session identifier (recorded on each entry).
            session_time: ISO8601 timestamp anchoring "now" for the LLM.
                Defaults to current UTC time. The LLM uses this to fill
                ``event_at`` when an event has no explicit timestamp in
                the conversation.

        Returns:
            ``EventExtractResult`` with ready-to-write MemoryEntry instances.
        """
        if not messages:
            return EventExtractResult()

        anchor = session_time or datetime.now(timezone.utc).isoformat()

        prompt = _EVENT_PROMPT.format(
            session_time=anchor,
            messages_text=_helpers.format_messages(messages),
        )

        try:
            raw = await self._call_llm(prompt)
        except Exception:
            logger.warning("Event extraction LLM call failed", exc_info=True)
            return EventExtractResult()

        return self._parse_response(raw, session_id=session_id, fallback_event_at=anchor)

    async def _call_llm(self, prompt: str) -> str:
        """Call Dolphin LLM. Kept as a method to preserve test patch surface."""
        return await _helpers.call_dolphin_llm(self._context, prompt)

    def _parse_response(
        self,
        raw: str,
        *,
        session_id: str,
        fallback_event_at: str,
    ) -> EventExtractResult:
        """Parse LLM JSON response into MemoryEntry instances."""
        data = _helpers.extract_json_object(raw)
        if data is None:
            logger.warning("Failed to parse event extraction response")
            return EventExtractResult()

        now_iso = datetime.now(timezone.utc).isoformat()
        events: List[MemoryEntry] = []

        for item in data.get("new_events", []):
            entry = self._build_entry(
                item,
                session_id=session_id,
                fallback_event_at=fallback_event_at,
                created_at=now_iso,
            )
            if entry is not None:
                events.append(entry)

        return EventExtractResult(new_events=events)

    @staticmethod
    def _build_entry(
        item: Any,
        *,
        session_id: str,
        fallback_event_at: str,
        created_at: str,
    ) -> Optional[MemoryEntry]:
        """Validate and build a single ``MemoryEntry`` from raw LLM output."""
        if not isinstance(item, dict):
            return None
        content = str(item.get("content", "")).strip()
        if not content:
            return None
        category = str(item.get("category", "")).strip()
        if category not in _EVENT_CATEGORIES:
            logger.debug("Skipping event with unknown category: %r", category)
            return None
        importance = str(item.get("importance", "medium"))
        score = _IMPORTANCE_SCORES.get(importance, 0.6)
        event_at = str(item.get("event_at") or "").strip() or fallback_event_at

        # due_at is only meaningful for todos. Ignore it on other categories
        # so a hallucinated value can't sneak in and accidentally extend a
        # decision's protection window via apply_event_decay.
        due_at_raw = str(item.get("due_at") or "").strip()
        due_at = due_at_raw if (due_at_raw and category == "todo") else None

        return MemoryEntry(
            id=new_id(),
            content=content,
            category=category,
            score=score,
            created_at=created_at,
            last_activated=created_at,
            activation_count=1,
            source_session=session_id,
            kind="event",
            event_at=event_at,
            due_at=due_at,
        )
