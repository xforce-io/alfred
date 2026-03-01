"""LLM-based memory extraction from conversation history."""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .models import MemoryEntry

logger = logging.getLogger(__name__)

_EXTRACT_PROMPT = """\
你是一个记忆提取引擎。分析以下对话，提取**关于用户本人**的长期有价值信息。

## 核心原则
你的目标是了解用户是谁、喜欢什么、怎么工作——而不是记录对话中讨论的具体内容。
想象你是一个长期助手，下次见到用户时，什么信息能帮你更好地服务他？

## 应该记住的（关于用户的画像信息）
- **preference**: 用户的兴趣、偏好、喜恶（如"用户是拜仁球迷"、"喜欢简洁代码风格"）
- **fact**: 用户的个人事实（如"主要用 Python 开发"、"在北京工作"）
- **workflow**: 用户的工作习惯和流程（如"习惯用 Telegram 沟通"、"每天早上看新闻"）
- **decision**: 用户做出的重要决定（如"决定使用 FastAPI 重构后端"）
- **experience**: 用户的经历和技能（如"有 5 年 ML 经验"）

## 不应该记住的
- 对话中讨论的新闻、事件、赛果等时效性内容
- 助手回复的具体信息（天气、比分、搜索结果等）
- 一次性的问答内容（"今天天气怎么样"）
- 助手自身的行为和状态
- **系统内部文件和机制的操作细节**：如 HEARTBEAT.md、MEMORY.md、AGENTS.md、USER.md 等系统管理文件的读写行为。这些是助手运行时的内部实现，不代表用户的工作习惯。记住用户层面的意图（如"用户希望定时推送论文"），而非实现层面的细节（如"用户通过HEARTBEAT.md管理任务"）

## 已有记忆（避免重复提取）
{existing_summary}

## 对话内容
{messages_text}

## 任务
1. 从对话中提取关于**用户本人**的新记忆（不要与已有记忆语义重复）
2. 标记被本次对话强化的已有记忆 ID（用户再次表现出相同偏好/特征时）

输出严格 JSON 格式：
```json
{{
  "new_memories": [
    {{
      "content": "关于用户的简洁描述",
      "category": "preference|fact|experience|workflow|decision",
      "importance": "high|medium|low"
    }}
  ],
  "reinforced_ids": ["已有记忆ID1"]
}}
```

注意：
- 每条记忆必须是关于**用户**的，而非对话内容的摘要
- 宁缺毋滥：如果对话中没有透露用户画像信息，返回空列表
- importance: high=核心偏好/身份特征, medium=有用的用户信息, low=可能有用的线索
"""


@dataclass
class ExtractResult:
    """Result of memory extraction."""

    new_memories: List[Dict[str, str]] = field(default_factory=list)
    reinforced_ids: List[str] = field(default_factory=list)


class MemoryExtractor:
    """Extract structured memories from conversation using LLM."""

    def __init__(self, context: Any):
        self._context = context

    async def extract(
        self,
        messages: List[Dict[str, Any]],
        existing_entries: List[MemoryEntry],
    ) -> ExtractResult:
        """Extract memories from conversation messages.

        Args:
            messages: Conversation history (list of role/content dicts).
            existing_entries: Current memories for dedup reference.

        Returns:
            ExtractResult with new memories and reinforced IDs.
        """
        if not messages:
            return ExtractResult()

        # Build existing memories summary (top 50 by score)
        top_existing = sorted(existing_entries, key=lambda e: e.score, reverse=True)[:50]
        if top_existing:
            existing_lines = []
            for e in top_existing:
                existing_lines.append(f"[{e.id}] ({e.category}) {e.content}")
            existing_summary = "\n".join(existing_lines)
        else:
            existing_summary = "（暂无已有记忆）"

        # Format messages
        messages_text = _format_messages(messages)

        prompt = _EXTRACT_PROMPT.format(
            existing_summary=existing_summary,
            messages_text=messages_text,
        )

        # Call LLM
        try:
            raw = await self._call_llm(prompt)
            return self._parse_response(raw)
        except Exception:
            logger.warning("Memory extraction LLM call failed", exc_info=True)
            return ExtractResult()

    async def _call_llm(self, prompt: str) -> str:
        """Call LLM using Dolphin client pattern."""
        from dolphin.core.llm.llm_client import LLMClient
        from dolphin.core.common.enums import Messages as DolphinMessages, MessageRole

        llm_client = LLMClient(self._context)
        msgs = DolphinMessages()
        msgs.append_message(MessageRole.USER, prompt)

        config = self._context.get_config()
        model = getattr(config, "fast_llm", None) or "qwen-turbo"

        result = ""
        async for chunk in llm_client.mf_chat_stream(
            messages=msgs,
            model=model,
            temperature=0.3,
            no_cache=True,
        ):
            result = chunk.get("content") or ""

        return result.strip()

    def _parse_response(self, raw: str) -> ExtractResult:
        """Parse LLM JSON response with fallback."""
        # Try direct JSON parse
        try:
            data = json.loads(raw)
            return self._build_result(data)
        except json.JSONDecodeError:
            pass

        # Try extracting JSON from markdown code block
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
                return self._build_result(data)
            except json.JSONDecodeError:
                pass

        # Try finding any JSON object
        m = re.search(r"\{[^{}]*\"new_memories\"[^{}]*\}", raw, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
                return self._build_result(data)
            except json.JSONDecodeError:
                pass

        logger.warning("Failed to parse memory extraction response")
        return ExtractResult()

    def _build_result(self, data: dict) -> ExtractResult:
        """Build ExtractResult from parsed JSON dict."""
        new_memories = []
        for item in data.get("new_memories", []):
            if isinstance(item, dict) and item.get("content"):
                new_memories.append({
                    "content": str(item["content"]),
                    "category": str(item.get("category", "fact")),
                    "importance": str(item.get("importance", "medium")),
                })

        reinforced_ids = [
            str(rid) for rid in data.get("reinforced_ids", []) if rid
        ]

        return ExtractResult(
            new_memories=new_memories,
            reinforced_ids=reinforced_ids,
        )


def _format_messages(messages: List[Dict[str, Any]], max_chars: int = 8000) -> str:
    """Format messages for prompt, truncating if needed."""
    lines = []
    total = 0
    for msg in messages:
        role = "用户" if msg.get("role") == "user" else "助手"
        content = str(msg.get("content", ""))
        if len(content) > 500:
            content = content[:500] + "..."
        line = f"**{role}**: {content}"
        total += len(line)
        if total > max_chars:
            lines.append("... (对话过长，已截断)")
            break
        lines.append(line)
    return "\n\n".join(lines)
