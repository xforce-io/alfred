"""Phase boundary context management.

Handles history clearing, system prompt construction, artifact injection,
and context mode (clean/inherit) logic at phase boundaries.
"""

from __future__ import annotations

import logging
from typing import Any, List

from dolphin.core.common.constants import KEY_HISTORY

from .models import PhaseConfig

logger = logging.getLogger(__name__)

# Rough token estimate: 1 token ≈ 3 chars
_INHERIT_THRESHOLD_CHARS = 32_000 * 3  # ~32K tokens


class PhaseContextManager:
    """Manages agent context at phase boundaries."""

    def __init__(self, agent: Any):
        self._agent = agent

    def clear_history(self) -> None:
        """Clear the agent's conversation history."""
        self._agent.executor.context.set_variable(KEY_HISTORY, [])

    def estimate_history_tokens(self) -> int:
        """Rough estimate of current history size in tokens."""
        history = self._agent.executor.context.get_var_value(KEY_HISTORY) or []
        total_chars = 0
        for msg in history:
            if isinstance(msg, dict):
                content = msg.get("content", "")
                if isinstance(content, str):
                    total_chars += len(content)
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict):
                            total_chars += len(str(part.get("text", "")))
            elif isinstance(msg, str):
                total_chars += len(msg)
        return total_chars // 3

    def determine_context_mode(self, iteration: int) -> str:
        """Determine context mode for a PhaseGroup iteration.

        Per design doc 7.3:
        - iteration 1: clean (fresh)
        - iteration 2: inherit if history < 32K tokens, else clean
        - iteration 3+: clean + inject failure_history summaries
        """
        if iteration <= 1:
            return "clean"
        if iteration == 2:
            est_chars = self.estimate_history_tokens() * 3
            if est_chars < _INHERIT_THRESHOLD_CHARS:
                return "inherit"
            return "clean"
        return "clean"

    def prepare_phase_context(
        self,
        *,
        artifact_injection: str,
        retry_context: str,
        failure_history: List[str],
        context_mode: str,
    ) -> str:
        """Prepare the user message for a phase turn.

        Clears or inherits history based on context_mode, then
        builds a composite user message with artifact injection,
        retry context, and failure history.
        """
        if context_mode == "clean":
            self.clear_history()

        parts: List[str] = []

        if artifact_injection:
            parts.append(artifact_injection)

        if retry_context:
            parts.append(f"## 重试上下文\n\n{retry_context}")

        if failure_history and context_mode == "clean":
            parts.append("## 历史失败记录\n\n" + "\n".join(
                f"- 第{i+1}轮: {h}" for i, h in enumerate(failure_history)
            ))

        parts.append("请开始本阶段的工作。")
        return "\n\n".join(parts)

    def build_phase_system_prompt(
        self,
        base_instructions: str,
        phase_config: PhaseConfig,
        *,
        instruction_content: str = "",
        is_verify: bool = False,
    ) -> str:
        """Build complete system prompt for a phase.

        Includes base workspace instructions, phase-specific instruction,
        tool restriction prompt, and artifact output protocol.
        """
        parts: List[str] = []

        if base_instructions:
            parts.append(base_instructions)

        if instruction_content:
            parts.append(f"## 本阶段指令\n\n{instruction_content}")

        # Tool restriction (prompt-level, not enforced at execution)
        if phase_config.allowed_tools:
            tool_list = ", ".join(phase_config.allowed_tools)
            parts.append(
                f"## 工具限制\n\n"
                f"在本阶段中，你只可以使用以下工具: {tool_list}\n"
                f"不要使用其他工具。"
            )

        # Artifact output protocol
        if not phase_config.verification_cmd:
            parts.append(
                "## 阶段产出协议\n\n"
                "在本阶段工作完成后，你必须用以下标签输出阶段产出：\n"
                "<phase_artifact>\n"
                "你的产出内容（Markdown 格式）\n"
                "</phase_artifact>"
            )

        # Verify protocol
        if is_verify and phase_config.verify_protocol == "structured_tag":
            parts.append(
                "## 验证结论协议\n\n"
                "在验证完成后，你必须输出验证结论标签：\n"
                "- 通过：<verify_result>PASS</verify_result>\n"
                "- 失败：<verify_result>FAIL: 具体失败原因</verify_result>"
            )

        return "\n\n".join(parts)
