"""
工作区加载器
"""

from pathlib import Path
from typing import Optional
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


@dataclass
class WorkspaceInstructions:
    """工作区指令集"""
    agents_md: Optional[str] = None
    skills_md: Optional[str] = None
    user_md: Optional[str] = None
    memory_md: Optional[str] = None
    heartbeat_md: Optional[str] = None


class WorkspaceLoader:
    """
    工作区加载器

    从 Agent 工作区读取 Markdown 配置文件。
    """

    SNAPSHOT_READ_RETRIES = 3

    INSTRUCTION_FILES = {
        'agents_md': 'AGENTS.md',
        'skills_md': 'SKILLS.md',
        'user_md': 'USER.md',
        'memory_md': 'MEMORY.md',
        'heartbeat_md': 'HEARTBEAT.md',
    }

    def __init__(self, workspace_path: Path):
        self.workspace_path = Path(workspace_path)

    def _read_file(self, filename: str) -> Optional[str]:
        """读取单个文件"""
        file_path = self.workspace_path / filename
        if file_path.exists():
            try:
                content = file_path.read_text(encoding="utf-8")
                logger.debug(f"加载 {filename} ({len(content)} 字符)")
                return content
            except Exception as e:
                logger.warning(f"读取 {filename} 失败: {e}")
                return None
        return None

    def _capture_file_stats(self) -> dict[str, Optional[tuple[int, int]]]:
        """Capture per-file (mtime_ns, size) for consistency checks."""
        snapshot: dict[str, Optional[tuple[int, int]]] = {}
        for filename in self.INSTRUCTION_FILES.values():
            file_path = self.workspace_path / filename
            if not file_path.exists():
                snapshot[filename] = None
                continue
            try:
                stat = file_path.stat()
                snapshot[filename] = (int(stat.st_mtime_ns), int(stat.st_size))
            except OSError:
                snapshot[filename] = None
        return snapshot

    def _read_instruction_contents(self) -> dict[str, Optional[str]]:
        """Read all instruction file contents once."""
        contents: dict[str, Optional[str]] = {}
        for attr, filename in self.INSTRUCTION_FILES.items():
            contents[attr] = self._read_file(filename)
        return contents

    def load(self) -> WorkspaceInstructions:
        """加载所有指令文件"""
        contents: dict[str, Optional[str]] = {}

        # Snapshot consistency: retry when any instruction file changed during read.
        for attempt in range(self.SNAPSHOT_READ_RETRIES):
            before = self._capture_file_stats()
            contents = self._read_instruction_contents()
            after = self._capture_file_stats()
            if before == after:
                break
            logger.warning(
                "Workspace instruction snapshot changed during read, retrying (%s/%s)",
                attempt + 1,
                self.SNAPSHOT_READ_RETRIES,
            )

        return WorkspaceInstructions(**contents)

    def build_system_prompt(self) -> str:
        """
        构建系统提示

        将工作区文件内容组合为系统提示的一部分。
        """
        instructions = self.load()
        parts = []

        if instructions.agents_md:
            parts.append(f"# 行为规范\n\n{instructions.agents_md}")

        if instructions.skills_md:
            parts.append(f"# 技能导航\n\n{instructions.skills_md}")

        if instructions.user_md:
            parts.append(f"# 用户画像\n\n{instructions.user_md}")

        # 用 MemoryManager 加载结构化记忆
        try:
            from ..core.memory.manager import MemoryManager
            memory_prompt = MemoryManager(self.workspace_path / "MEMORY.md").get_prompt_memories()
            if memory_prompt:
                parts.append(memory_prompt)
            elif instructions.memory_md:
                # Fallback：旧格式 MEMORY.md
                memory_lines = instructions.memory_md.split('\n')[:50]
                parts.append(f"# 历史记忆\n\n" + '\n'.join(memory_lines))
        except Exception:
            if instructions.memory_md:
                memory_lines = instructions.memory_md.split('\n')[:50]
                parts.append(f"# 历史记忆\n\n" + '\n'.join(memory_lines))

        if instructions.heartbeat_md:
            parts.append(f"# 心跳任务\n\n{instructions.heartbeat_md}")

        if not parts:
            return ""

        return "\n\n---\n\n".join(parts)
