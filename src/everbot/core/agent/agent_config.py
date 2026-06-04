"""Agent 配置工具(dolphin-free)。

原先这些是 dolphin ``AgentFactory`` 的方法,被 cron/heartbeat/core_service 当纯配置
工具使用(与 dolphin agent 创建无关)。#38 删 dolphin 时抽到此处,逻辑不变。
"""
from __future__ import annotations

from pathlib import Path

from ...infra.config import get_config
from ...infra.user_data import get_user_data_manager


def resolve_agent_model(agent_name: str) -> str:
    """从 config 解析 agent 的模型名(无需实例)。

    优先级:per-agent model(``everbot.agents.<name>.model``) > 全局 ``everbot.default_model``
    > 空串。
    """
    app_config = get_config() or {}
    everbot = app_config.get("everbot", {}) or {}
    agent_section = (everbot.get("agents", {}) or {}).get(agent_name, {}) or {}
    per_agent = agent_section.get("model")
    if per_agent:
        return per_agent
    return everbot.get("default_model") or ""


def _repo_skills_dir() -> Path:
    # agent_config.py = …/src/everbot/core/agent/agent_config.py → parents[4] = repo root
    return Path(__file__).resolve().parents[4] / "skills"


def append_runtime_paths(*, workspace_instructions: str, workspace_path: Path) -> str:
    """在工作区指令后追加运行时路径提示(供 agent 自助读文件)。"""
    user_data = get_user_data_manager()
    parts = []
    if workspace_instructions.strip():
        parts.append(workspace_instructions.strip())

    def safe_path(path: Path) -> str:
        path_str = str(path)
        home_dir = str(Path.home())
        if path_str.startswith(home_dir):
            return "~" + path_str[len(home_dir):]
        return path_str

    workspace_path = Path(workspace_path)
    parts.append(
        "\n".join(
            [
                "# Runtime Paths",
                "",
                "These paths are available on the local machine:",
                f"- Workspace root: {safe_path(workspace_path)}",
                "- Workspace files:",
                f"  - {safe_path(workspace_path / 'SOUL.md')}",
                f"  - {safe_path(workspace_path / 'AGENTS.md')}",
                f"  - {safe_path(workspace_path / 'USER.md')}",
                f"  - {safe_path(workspace_path / 'MEMORY.md')}",
                f"  - {safe_path(workspace_path / 'HEARTBEAT.md')}",
                f"  - {safe_path(workspace_path / 'CODING.md')}  (agent-owned notes)",
                f"- Agent temp dir: {safe_path(workspace_path / 'tmp')}  (use this for ALL temporary files)",
                f"- Alfred home: {safe_path(user_data.alfred_home)}",
                f"- Sessions dir: {safe_path(user_data.sessions_dir)}",
                f"- Logs dir: {safe_path(user_data.logs_dir)}",
                f"- Global skills dir: {safe_path(user_data.skills_dir)}",
                f"- Built-in skills dir: {safe_path(_repo_skills_dir())}",
                "- Path rule: `~/.alfred/...` is already rooted at home after expansion. Never prepend the repository path to it.",
                "",
                "If you need to read these files, prefer a file-reading tool if available (e.g. read_file). Otherwise use a shell tool to run `cat`.",
            ]
        )
    )
    return "\n\n".join(parts)
