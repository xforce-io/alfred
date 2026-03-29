"""
用户数据统一管理
"""

from pathlib import Path
from typing import List, Dict, Optional
import re
import logging

logger = logging.getLogger(__name__)


class UserDataManager:
    """
    用户数据统一管理器

    统一管理用户的所有数据：
    - 配置
    - Agent 工作区
    - 会话历史
    - 日志
    """

    def __init__(self, alfred_home: Optional[Path] = None):
        import os
        if alfred_home is None:
            env_home = os.environ.get("ALFRED_HOME")
            if env_home:
                alfred_home = Path(env_home).expanduser()
        self.alfred_home = alfred_home or Path("~/.alfred").expanduser()

    # --- 路径属性 ---

    @property
    def config_path(self) -> Path:
        """主配置文件路径"""
        return self.alfred_home / "config.yaml"

    @property
    def dolphin_config_path(self) -> Path:
        """Dolphin 配置文件路径"""
        return self.alfred_home / "dolphin.yaml"

    @property
    def agents_dir(self) -> Path:
        """Agent 工作区目录"""
        return self.alfred_home / "agents"

    @property
    def sessions_dir(self) -> Path:
        """会话存储目录"""
        return self.alfred_home / "sessions"

    @property
    def logs_dir(self) -> Path:
        """日志目录"""
        return self.alfred_home / "logs"

    @property
    def pid_file(self) -> Path:
        """Daemon PID file path."""
        return self.alfred_home / "everbot.pid"

    @property
    def status_file(self) -> Path:
        """Daemon status snapshot file path."""
        return self.alfred_home / "everbot.status.json"

    @property
    def heartbeat_log_file(self) -> Path:
        """Heartbeat log file path."""
        return self.logs_dir / "heartbeat.log"

    @property
    def heartbeat_events_file(self) -> Path:
        """Structured heartbeat events JSONL file path."""
        return self.logs_dir / "heartbeat_events.jsonl"

    @property
    def skills_dir(self) -> Path:
        """全局技能目录"""
        return self.alfred_home / "skills"

    @property
    def skill_logs_dir(self) -> Path:
        """SLM evaluation segment logs (global, legacy — prefer agent-scoped)"""
        return self.alfred_home / "skill_logs"

    def get_agent_skill_logs_dir(self, agent_name: str) -> Path:
        """Per-agent SLM skill usage log directory."""
        return self.get_agent_dir(agent_name) / "skill_logs"

    def get_agent_skill_eval_dir(self, agent_name: str) -> Path:
        """Per-agent SLM evaluation data directory (version pointers + reports)."""
        return self.get_agent_dir(agent_name) / "skill_eval"

    @property
    def trajectories_dir(self) -> Path:
        """执行轨迹目录"""
        return self.alfred_home / "trajectories"

    # --- Agent 管理 ---

    def get_agent_dir(self, agent_name: str) -> Path:
        """获取 Agent 工作区目录"""
        return self.agents_dir / agent_name

    def get_agent_tmp_dir(self, agent_name: str) -> Path:
        """Return per-agent tmp directory path."""
        return self.get_agent_dir(agent_name) / "tmp"

    @staticmethod
    def _sanitize_session_id_for_filename(session_id: str) -> str:
        """Map session id to a filesystem-safe suffix."""
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", session_id or "")
        return safe.strip("._-") or "session"

    def get_session_trajectory_path(self, agent_name: str, session_id: str) -> Path:
        """Return trajectory file path isolated by session id."""
        safe_session = self._sanitize_session_id_for_filename(session_id)
        return self.get_agent_tmp_dir(agent_name) / f"trajectory_{safe_session}.json"

    def list_agents(self) -> List[str]:
        """列出所有 Agent"""
        if not self.agents_dir.exists():
            return []

        agents = []
        for d in self.agents_dir.iterdir():
            if d.is_dir() and (d / "agent.dph").exists():
                agents.append(d.name)

        return sorted(agents)

    def get_workspace_files(self, agent_name: str) -> Dict[str, Optional[str]]:
        """
        获取 Agent 工作区文件内容

        Returns:
            文件名 -> 内容的字典
        """
        agent_dir = self.get_agent_dir(agent_name)
        files = {}

        for filename in ["SOUL.md", "AGENTS.md", "HEARTBEAT.md", "MEMORY.md", "USER.md"]:
            file_path = agent_dir / filename
            if file_path.exists():
                try:
                    files[filename] = file_path.read_text(encoding="utf-8")
                except Exception as e:
                    logger.warning("读取 %s 失败: %s", filename, e)
                    files[filename] = None
            else:
                files[filename] = None

        return files

    def get_skill_log_recorder(
        self,
        agent_name: str = "",
        workspace_path: Optional[Path] = None,
    ) -> "Optional[Any]":
        """Return a SkillLogRecorder with per-agent log isolation.

        Args:
            agent_name: Agent name — logs go to ``agents/{name}/skill_logs/``.
                        When empty, falls back to the global ``skill_logs/``.
            workspace_path: Agent workspace for private skills lookup.
        """
        try:
            from ..core.slm.skill_log_recorder import SkillLogRecorder
            logs_dir = self.get_agent_skill_logs_dir(agent_name) if agent_name else self.skill_logs_dir
            # Build 3-tier skill dirs for version lookup
            skill_dirs: list[Path] = []
            if workspace_path:
                skill_dirs.append(Path(workspace_path) / "skills")
            skill_dirs.append(self.skills_dir)
            bundled = Path(__file__).resolve().parents[3] / "skills"
            if bundled.exists():
                skill_dirs.append(bundled)
            return SkillLogRecorder(skill_logs_dir=logs_dir, skill_dirs=skill_dirs)
        except Exception as _err:
            logger.warning("Failed to create SkillLogRecorder: %s", _err)
            return None

    # --- 初始化 ---

    def ensure_directories(self):
        """确保必要目录存在"""
        for dir_path in [
            self.agents_dir,
            self.sessions_dir,
            self.logs_dir,
            self.skills_dir,
            self.trajectories_dir,
        ]:
            dir_path.mkdir(parents=True, exist_ok=True)
            logger.debug("确保目录存在: %s", dir_path)

    def init_agent_workspace(self, agent_name: str):
        """
        初始化 Agent 工作区

        创建默认的工作区文件和目录结构。
        """
        agent_dir = self.get_agent_dir(agent_name)
        agent_dir.mkdir(parents=True, exist_ok=True)

        # 创建默认文件
        templates = {
            "SOUL.md": f"""# {agent_name} 的灵魂

## 身份

我是 {agent_name}，一个专为你打造的个人 AI 助手。

## 人格特征

- 直接、简洁，不废话
- 遇到不确定时，诚实说不知道，然后主动去查
- 对用户的真实需求保持敏锐

## 说话风格

- 默认用中文回复，除非用户用英文
- 语气自然，像在和朋友交流
- 不用"好的！"、"当然！"等无意义的开场白

## 核心价值

- 帮用户解决真实问题，而非表现聪明
- 行动先于解释
- 对用户的时间保持尊重
""",
            "AGENTS.md": f"""# {agent_name} 行为规范

## 身份
你是 {agent_name} 助理。

## 核心职责
（待补充）

## 沟通风格
- 简洁专业
- 数据驱动

## 权限与工具
- 你拥有 `_bash` 工具，可以执行本地命令。
- **允许访问网络**：你可以使用 `curl` 或 `wget` 等命令访问互联网获取实时信息或下载资源。
- 你可以使用 `_python` 执行复杂的逻辑处理。

## 技能系统 (Skills)

### 已安装技能
系统启动时会自动扫描并注入已安装技能列表到你的 prompt 中。以系统动态注入的列表为准。
要查看某个技能的详细用法，调用 `_load_resource_skill(skill_name)` 加载其完整说明。

### 发现更多技能
技能注册表位于 `~/.alfred/skills-registry.json`，包含所有可安装技能的目录。
你可以用 `_bash` 或 `_read_file` 读取该文件，然后向用户展示可用技能。

### 技能目录位置
- **全局技能目录**: `~/.alfred/skills/` — 所有 agent 共享
- **专属技能目录**: `~/.alfred/agents/{agent_name}/skills/` — 仅当前 agent 可用

## 心跳机制 (Heartbeat)

系统支持后台心跳执行机制，允许你定时执行任务。

1. **管理任务**：通过 `routine_cli.py` 管理定时任务（add/list/update/remove）。**禁止直接编辑 `HEARTBEAT.md`**，文件格式由系统维护，手动修改会导致解析失败。
2. **执行规则**：系统会定期按照 `HEARTBEAT.md` 唤醒你。如果你在心跳模式下工作，请直接行动并更新任务记录。
3. **推送逻辑**：你的心跳执行过程可能会被推送到 UI（场景：用户长时间闲置）。

## 限制
- 严禁执行具有破坏性的命令（如 `rm -rf /`）。
- 保持操作透明，重要操作前请告知用户。
""",
            "HEARTBEAT.md": """# 心跳任务

## Tasks

```json
{
  "version": 2,
  "tasks": []
}
```

## 执行记录
<!-- 由 EverBot 自动追加 -->
""",
            "MEMORY.md": """# 长期记忆

（暂无记录）
""",
            "USER.md": """# 用户画像

（待补充）
""",
            "agent.dph": f"""/explore/(model="$model_name", system_prompt="$workspace_instructions", tools=[_bash, _python, _date, _read_file, _read_folder])
{agent_name} Agent

当前时间：$current_time

请根据用户的要求提供帮助。
-> answer
""",
        }

        for filename, content in templates.items():
            file_path = agent_dir / filename
            if not file_path.exists():
                file_path.write_text(content, encoding="utf-8")
                logger.info("创建文件: %s", file_path)

        # 创建 Agent 专属技能目录
        (agent_dir / "skills").mkdir(exist_ok=True)

        logger.info("Agent 工作区初始化完成: %s", agent_name)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_default_user_data: Optional[UserDataManager] = None


def get_user_data_manager(alfred_home: Optional[Path] = None) -> UserDataManager:
    """Return the shared UserDataManager singleton.

    On first call the instance is created (optionally with *alfred_home*).
    Subsequent calls return the cached instance regardless of *alfred_home*.
    """
    global _default_user_data
    if _default_user_data is None:
        _default_user_data = UserDataManager(alfred_home=alfred_home)
    return _default_user_data


def reset_user_data_manager() -> None:
    """Reset the singleton (mainly for tests)."""
    global _default_user_data
    _default_user_data = None
