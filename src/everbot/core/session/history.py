"""
History 管理
"""

from pathlib import Path
from typing import List, Dict, Any
from datetime import datetime
import logging
from dolphin.core.common.constants import KEY_HISTORY

logger = logging.getLogger(__name__)


class HistoryManager:
    """
    History 管理器

    使用 Dolphin API 管理对话历史，不直接操作内部列表。
    """

    MAX_HISTORY_ROUNDS = 10  # 最多保留 10 轮对话

    def __init__(self, memory_path: Path):
        self.memory_path = Path(memory_path)

    def trim_if_needed(self, agent: Any) -> bool:
        """
        裁剪过长的 History

        使用 Dolphin API 而非直接操作内部列表。

        Args:
            agent: DolphinAgent 实例

        Returns:
            是否执行了裁剪
        """
        try:
            context = agent.executor.context
            history = context.get_history_messages(normalize=True)

            max_messages = self.MAX_HISTORY_ROUNDS * 2  # user + assistant

            if len(history) <= max_messages:
                return False

            # 1. 提取要归档的消息
            archived_messages = history[:-max_messages]

            # 2. 归档到 MEMORY.md
            self._archive_to_memory(archived_messages)

            # 3. 使用 Dolphin API 重置 History
            trimmed_messages = history[-max_messages:]
            # context.clear_history() might also not exist, let's just use set_variable to overwrite
            context.set_variable(KEY_HISTORY, trimmed_messages)

            logger.info(f"裁剪 History: 归档 {len(archived_messages)} 条，保留 {len(trimmed_messages)} 条")
            return True

        except Exception as e:
            logger.error(f"裁剪 History 失败: {e}")
            return False

    def _archive_to_memory(self, messages: List[Dict[str, Any]]):
        """
        将消息归档到 MEMORY.md

        Args:
            messages: 要归档的消息列表
        """
        if not messages:
            return

        try:
            # 格式化为摘要
            summary_lines = [
                "",
                "---",
                "",
                f"## 历史对话归档 ({datetime.now().strftime('%Y-%m-%d %H:%M')})",
                "",
            ]

            for msg in messages:
                role = "用户" if msg.get("role") == "user" else "助手"
                content = msg.get("content", "")[:200]  # 截断
                if len(msg.get("content", "")) > 200:
                    content += "..."
                summary_lines.append(f"**{role}**: {content}")
                summary_lines.append("")

            # 追加到 MEMORY.md
            self.memory_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.memory_path, "a", encoding="utf-8") as f:
                f.write("\n".join(summary_lines))

            logger.info(f"已归档 {len(messages)} 条消息到 {self.memory_path}")

        except Exception as e:
            logger.error(f"归档消息失败: {e}")
