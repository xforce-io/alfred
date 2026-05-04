"""LLM-based profile memory extraction from conversation history.

Profile extractor produces user-portrait memories — preferences, facts,
workflows, decisions, experiences. It is paired with ``ProfileStore`` to
persist results to ``MEMORY.md``.

Event extraction (time-anchored decisions, todos, incidents) lives in a
separate ``event_extractor`` module.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List

from . import _extractor_helpers as _helpers
from .models import MemoryEntry

logger = logging.getLogger(__name__)

_PROFILE_PROMPT = """\
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
- **严格去重**：仔细对比已有记忆，如果新提取的内容与已有记忆表达相同或相似的意思（即使措辞不同），必须放入 reinforced_ids 而非 new_memories。例如"用户偏好简洁输出"和"用户反感冗长展示"是同一个意思，不要重复提取。
- **宁缺毋滥**：如果对话中没有透露用户画像的**全新**信息，返回空的 new_memories 列表。大多数对话不应产生新记忆。
- importance: high=核心偏好/身份特征, medium=有用的用户信息, low=可能有用的线索
"""


@dataclass
class ExtractResult:
    """Result of memory extraction."""

    new_memories: List[Dict[str, str]] = field(default_factory=list)
    reinforced_ids: List[str] = field(default_factory=list)


class ProfileExtractor:
    """Extract user-portrait memories from conversation using LLM."""

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

        prompt = _PROFILE_PROMPT.format(
            existing_summary=existing_summary,
            messages_text=_helpers.format_messages(messages),
        )

        # Call LLM
        try:
            raw = await self._call_llm(prompt)
            return self._parse_response(raw)
        except Exception:
            logger.warning("Memory extraction LLM call failed", exc_info=True)
            return ExtractResult()

    async def _call_llm(self, prompt: str) -> str:
        """Call Dolphin LLM. Kept as a method to preserve test patch surface."""
        return await _helpers.call_dolphin_llm(self._context, prompt)

    def _parse_response(self, raw: str) -> ExtractResult:
        """Parse LLM JSON response with fallback strategies."""
        data = _helpers.extract_json_object(raw)

        if data is None:
            # Profile-specific last resort: find any JSON object that
            # mentions ``new_memories`` even if surrounded by prose.
            m = re.search(r"\{[^{}]*\"new_memories\"[^{}]*\}", raw, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group(0))
                except json.JSONDecodeError:
                    data = None

        if data is None:
            logger.warning("Failed to parse profile extraction response")
            return ExtractResult()

        return self._build_result(data)

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
