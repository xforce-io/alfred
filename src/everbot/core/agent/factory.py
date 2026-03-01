"""
Dolphin Agent 工厂

创建和初始化真实的 Dolphin Agent。
"""

from pathlib import Path
from typing import Dict, Any, Optional, List, Set
from datetime import datetime
import json
import logging
import re

from dolphin.sdk import DolphinAgent, Env, GlobalSkills
from dolphin.core.config.global_config import GlobalConfig
from dolphin.core.common.constants import (
    KEY_HISTORY_COMPACT_ON_PERSIST,
    KEY_HISTORY_COMPACT_RECENT_TURNS,
)

from ...infra.workspace import WorkspaceLoader
from ...infra.user_data import get_user_data_manager

logger = logging.getLogger(__name__)


class AgentFactory:
    """
    Agent 工厂

    负责创建和初始化 Dolphin Agent 实例。
    """
    SKILLS_SECTION_START = "<!-- AUTO_SKILLS_SECTION_START -->"
    SKILLS_SECTION_END = "<!-- AUTO_SKILLS_SECTION_END -->"

    def __init__(
        self,
        global_config_path: Optional[str] = None,
        default_model: Optional[str] = None,
    ):
        """
        初始化 Agent 工厂

        Args:
            global_config_path: Dolphin 全局配置文件路径
            default_model: 默认模型名称
        """
        self.global_config_path = global_config_path or self._find_global_config()
        self.default_model = default_model
        self._global_config: Optional[GlobalConfig] = None
        # 不再缓存 GlobalSkills，每个 agent 创建独立实例

    def _find_global_config(self) -> str:
        """
        查找全局配置文件

        按优先级查找：
        1. ~/.alfred/dolphin.yaml
        2. ./config/dolphin.yaml
        3. 项目根目录的 config/dolphin.yaml（绝对路径）
        4. 使用默认配置（空路径）
        """
        candidates = [
            Path("~/.alfred/dolphin.yaml").expanduser(),
            Path("./config/dolphin.yaml").resolve(),
            Path(__file__).resolve().parents[4] / "config" / "dolphin.yaml",
        ]

        for path in candidates:
            if path.exists():
                logger.info(f"使用 Dolphin 配置: {path}")
                return str(path)

        logger.warning("未找到 Dolphin 配置文件，使用默认配置")
        return ""

    def _get_global_config(self) -> GlobalConfig:
        """获取或创建 GlobalConfig"""
        if self._global_config is None:
            if self.global_config_path:
                # 使用 from_yaml 正确加载配置
                self._global_config = GlobalConfig.from_yaml(self.global_config_path)
            else:
                self._global_config = GlobalConfig()
            logger.info("GlobalConfig 已初始化")
        return self._global_config

    def _create_agent_config(
        self, workspace_path: Path
    ) -> GlobalConfig:
        """
        为特定 agent 创建配置，添加专属 skills 目录

        Args:
            workspace_path: Agent 工作区路径

        Returns:
            新配置实例，包含 agent 专属的 skills 目录
        """
        # 每次从 YAML 重新加载配置（避免 deepcopy 问题）
        if self.global_config_path:
            agent_config = GlobalConfig.from_yaml(self.global_config_path)
        else:
            agent_config = GlobalConfig()

        # 添加 agent 专属 skills 目录
        agent_skills_dir = str(workspace_path / "skills")

        # 确保 resource_skills 配置存在
        if not hasattr(agent_config, 'resource_skills'):
            agent_config.resource_skills = {}

        if not isinstance(agent_config.resource_skills, dict):
            agent_config.resource_skills = {}

        # 获取或创建 directories 列表（拷贝一份避免修改原列表）
        base_directories = agent_config.resource_skills.get('directories', [])
        if not isinstance(base_directories, list):
            base_directories = []

        # 创建新的目录列表
        directories = list(base_directories)

        # 添加 agent 专属目录（最高优先级）
        if agent_skills_dir not in directories:
            directories.insert(0, agent_skills_dir)
            logger.info(f"为 agent 添加专属 skills 目录: {agent_skills_dir}")

        # 确保全局 skills 目录也在列表中
        global_skills_dir = str(Path("~/.alfred/skills").expanduser())
        if global_skills_dir not in directories:
            directories.append(global_skills_dir)
            logger.info(f"添加全局 skills 目录: {global_skills_dir}")

        # Add bundled repository skills as fallback when available.
        bundled_skills_dir = Path(__file__).resolve().parents[4] / "skills"
        if bundled_skills_dir.exists():
            bundled_skills_dir_str = str(bundled_skills_dir)
            if bundled_skills_dir_str not in directories:
                directories.append(bundled_skills_dir_str)
                logger.info(f"添加仓库内置 skills 目录: {bundled_skills_dir_str}")

        agent_config.resource_skills['directories'] = directories

        # 确保 resource_skills 是启用的
        if 'enabled' not in agent_config.resource_skills:
            agent_config.resource_skills['enabled'] = True

        # 注入变量供 SKILL.md 中的 $WORKSPACE_ROOT 等占位符替换
        variables = agent_config.resource_skills.get('variables', {})
        variables['WORKSPACE_ROOT'] = str(workspace_path)
        agent_config.resource_skills['variables'] = variables

        return agent_config

    async def create_agent(
        self,
        agent_name: str,
        workspace_path: Path,
        model_name: Optional[str] = None,
        extra_variables: Optional[Dict[str, Any]] = None,
    ) -> DolphinAgent:
        """
        创建 Dolphin Agent

        Args:
            agent_name: Agent 名称
            workspace_path: Agent 工作区路径
            model_name: 模型名称
            extra_variables: 额外的变量

        Returns:
            初始化完成的 DolphinAgent 实例
        """
        workspace_path = Path(workspace_path)

        # 1. 为此 agent 创建专属配置（包含专属 skills 目录）
        agent_config = self._create_agent_config(workspace_path)
        actual_model = model_name or self.default_model or agent_config.default_llm

        # 2. 加载工作区指令
        logger.info(f"创建 Agent: {agent_name}, 使用模型: {actual_model}")
        loader = WorkspaceLoader(workspace_path)
        workspace_instructions = loader.build_system_prompt()
        workspace_instructions = self._append_runtime_paths(
            workspace_instructions=workspace_instructions,
            workspace_path=workspace_path,
        )

        # 3. 检查 agent.dph 文件
        agent_dph_path = workspace_path / "agent.dph"
        if not agent_dph_path.exists():
            raise FileNotFoundError(
                f"Agent 定义文件不存在: {agent_dph_path}\n"
                f"请先运行: bin/everbot init {agent_name}"
            )

        # 3.1 兼容旧版 YAML 风格 agent.dph（迁移并仅保留 agent.dph）
        agent_dph_path = self._ensure_compatible_agent_dph(
            agent_name=agent_name,
            workspace_path=workspace_path,
            agent_dph_path=agent_dph_path,
            model_name=actual_model,
            workspace_instructions=workspace_instructions,
        )

        # 4. 准备变量
        variables = {
            "workspace_instructions": workspace_instructions,
            "model_name": actual_model,
            "current_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "agent_name": agent_name,
        }
        if extra_variables:
            variables.update(extra_variables)

        # 5. 为此 agent 创建独立的 GlobalSkills（包含专属 skills 目录）
        logger.info(f"为 Agent {agent_name} 创建独立 GlobalSkills")
        agent_skills = GlobalSkills(agent_config)
        logger.info(f"GlobalSkills 创建完成")

        # 6. 创建 Agent
        agent = DolphinAgent(
            name=agent_name,
            file_path=str(agent_dph_path),
            global_config=agent_config,
            global_skills=agent_skills,
            variables=variables,
            verbose=False,
        )

        # 7. 初始化
        await agent.initialize()
        logger.info(f"Agent 已初始化: {agent_name}")

        # 初始化 trajectory 记录
        trajectory_path = str(workspace_path / "tmp" / "trajectory.json")
        agent.executor.context.init_trajectory(trajectory_path, overwrite=True)
        logger.info(f"Trajectory initialized: {trajectory_path}")

        runtime_skills = self._extract_runtime_available_skills(agent.global_skills)
        if runtime_skills:
            skills_section = self._build_skills_prompt_section(runtime_skills)
            workspace_instructions = self._upsert_skills_prompt_section(
                workspace_instructions, skills_section
            )
            logger.info("Injected %s runtime available skills into prompt.", len(runtime_skills))

        # 8. 设置初始变量到 Context
        context = agent.executor.context
        for key, value in variables.items():
            if key == "workspace_instructions":
                value = workspace_instructions
            context.set_variable(key, value)
        context.set_variable("session_created_at", datetime.now().isoformat())

        # Enable history compaction: drop tool chains from old turns before
        # persisting so that context doesn't grow unboundedly across turns.
        # recent_turns=0 means ALL previous turns are trimmed to
        # user + pinned + assistant_final (no tool chains).  The current
        # turn's full tool chain lives in SCRATCHPAD, not in history.
        context.set_variable(KEY_HISTORY_COMPACT_ON_PERSIST, True)
        context.set_variable(KEY_HISTORY_COMPACT_RECENT_TURNS, 0)

        return agent

    def _ensure_compatible_agent_dph(
        self,
        *,
        agent_name: str,
        workspace_path: Path,
        agent_dph_path: Path,
        model_name: str,
        workspace_instructions: str,
    ) -> Path:
        """
        Ensure the agent definition file is in a parsable DPH format.

        Some older workspaces used a YAML-like `agent.dph` format. Dolphin expects DPH blocks
        containing `->` or `>>`. If we detect a YAML-like definition, we generate a compatible
        DPH file and use it instead.
        """
        try:
            raw = agent_dph_path.read_text(encoding="utf-8")
        except Exception:
            logger.debug("Failed to read agent.dph at %s", agent_dph_path, exc_info=True)
            return agent_dph_path

        if "->" in raw or ">>" in raw:
            return agent_dph_path

        definition = self._try_parse_legacy_yaml_agent(raw)
        if definition is None:
            return agent_dph_path

        migrated_content = self._render_generated_dph(
            agent_name=agent_name,
            model_name=model_name,
            workspace_instructions=workspace_instructions,
            legacy_system_prompt=definition.get("system_prompt"),
            legacy_tools=definition.get("tools"),
        )

        try:
            baks_dir = workspace_path / "baks"
            baks_dir.mkdir(parents=True, exist_ok=True)

            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            legacy_backup = baks_dir / f"agent.dph.legacy.{ts}.bak"
            agent_dph_path.replace(legacy_backup)

            old_generated = workspace_path / "agent.generated.dph"
            if old_generated.exists():
                old_generated.replace(baks_dir / f"agent.generated.dph.{ts}.bak")

            agent_dph_path.write_text(migrated_content, encoding="utf-8")
            logger.warning(
                "Detected legacy YAML-style agent.dph. Migrated to DPH and backed up legacy file to: %s",
                legacy_backup,
            )
            return agent_dph_path
        except Exception:
            logger.warning("Failed to migrate legacy agent.dph at %s", agent_dph_path, exc_info=True)
            return agent_dph_path

    def _try_parse_legacy_yaml_agent(self, raw: str) -> Optional[Dict[str, Any]]:
        """Try to parse YAML-like agent.dph. Returns dict if it looks like legacy YAML."""
        try:
            import yaml
        except Exception:
            logger.debug("yaml module not available, skipping legacy agent.dph parse")
            return None

        try:
            data = yaml.safe_load(raw)
        except Exception:
            logger.debug("Failed to parse legacy YAML agent.dph", exc_info=True)
            return None

        if not isinstance(data, dict):
            return None

        if "system_prompt" not in data and "model" not in data and "tools" not in data:
            return None

        return data

    def _render_generated_dph(
        self,
        *,
        agent_name: str,
        model_name: str,
        workspace_instructions: str,
        legacy_system_prompt: Optional[str],
        legacy_tools: Any,
    ) -> str:
        """Render a minimal DPH definition that Dolphin can parse."""
        system_prompt = (legacy_system_prompt or "").strip()
        if "$workspace_instructions" not in system_prompt:
            system_prompt = (system_prompt + "\n\n$workspace_instructions").strip()

        tools = self._map_legacy_tools_to_dph(legacy_tools)
        tools_expr = ", ".join(tools)

        return f"""'''
{agent_name} Agent

{system_prompt}
''' -> system

/explore/(model="$model_name", tools=[{tools_expr}])
$workspace_instructions

Current time: $current_time
-> answer
"""

    def _map_legacy_tools_to_dph(self, legacy_tools: Any) -> list[str]:
        """Map legacy YAML tool entries to DPH tool identifiers."""
        enabled = set()
        if isinstance(legacy_tools, list):
            for item in legacy_tools:
                if not isinstance(item, dict):
                    continue
                if item.get("enabled") is False:
                    continue
                tool_type = str(item.get("type", "")).strip().lower()
                if tool_type in {"bash", "shell"}:
                    enabled.add("_bash")
                if tool_type in {"python", "py"}:
                    enabled.add("_python")
                if tool_type in {"date", "time"}:
                    enabled.add("_date")

        # Default tools for a usable local assistant.
        if not enabled:
            enabled = {"_bash", "_python", "_date"}

        return sorted(enabled)

    def _append_runtime_paths(self, *, workspace_instructions: str, workspace_path: Path) -> str:
        """Append runtime path hints to workspace instructions for agent self-service."""
        user_data = get_user_data_manager()
        parts = []
        if workspace_instructions.strip():
            parts.append(workspace_instructions.strip())

        # 使用 ~ 替换用户目录，避免泄露用户名
        def safe_path(path: Path) -> str:
            path_str = str(path)
            home_dir = str(Path.home())
            if path_str.startswith(home_dir):
                return "~" + path_str[len(home_dir):]
            return path_str

        parts.append(
            "\n".join(
                [
                    "# Runtime Paths",
                    "",
                    "These paths are available on the local machine:",
                    f"- Workspace root: {safe_path(workspace_path)}",
                    f"- Workspace files:",
                    f"  - {safe_path(workspace_path / 'AGENTS.md')}",
                    f"  - {safe_path(workspace_path / 'USER.md')}",
                    f"  - {safe_path(workspace_path / 'MEMORY.md')}",
                    f"  - {safe_path(workspace_path / 'HEARTBEAT.md')}",
                    f"- Agent temp dir: {safe_path(workspace_path / 'tmp')}  (use this for ALL temporary files)",
                    f"- Alfred home: {safe_path(user_data.alfred_home)}",
                    f"- Sessions dir: {safe_path(user_data.sessions_dir)}",
                    f"- Logs dir: {safe_path(user_data.logs_dir)}",
                    "- Path rule: `~/.alfred/...` is already rooted at home after expansion. Never prepend the repository path to it.",
                    "",
                    "If you need to read these files, prefer a file-reading tool if available (e.g. read_file). Otherwise use a shell tool to run `cat`.",
                ]
            )
        )

        return "\n\n---\n\n".join([p for p in parts if p])

    def _get_skills_directories(self, agent_config: GlobalConfig) -> List[Path]:
        """获取技能目录列表"""
        directories = []

        # 从配置获取
        if hasattr(agent_config, 'resource_skills') and isinstance(agent_config.resource_skills, dict):
            config_dirs = agent_config.resource_skills.get('directories', [])
            for d in config_dirs:
                path = Path(d).expanduser()
                if path.exists() and path.is_dir():
                    directories.append(path)

        return directories

    def _parse_skill_metadata(self, skill_dir: Path) -> Optional[Dict[str, Any]]:
        """解析 SKILL.md 获取技能元数据"""
        skill_md = skill_dir / "SKILL.md"

        if not skill_md.exists():
            return None

        try:
            content = skill_md.read_text(encoding="utf-8")
        except Exception:
            logger.debug("Failed to read SKILL.md at %s", skill_md, exc_info=True)
            return None

        # 提取标题（第一个 # 标题）
        title_match = re.search(r'^#\s+(.+)$', content, re.MULTILINE)
        title = title_match.group(1) if title_match else skill_dir.name

        # 提取描述（标题后的第一段）
        desc_match = re.search(r'^#\s+.+$\n\n(.+?)(?:\n\n|\n#|$)', content, re.MULTILINE | re.DOTALL)
        description = desc_match.group(1).strip() if desc_match else ""

        # 使用 ~ 替换用户目录，避免泄露用户名
        path_str = str(skill_dir)
        home_dir = str(Path.home())
        if path_str.startswith(home_dir):
            path_str = "~" + path_str[len(home_dir):]

        return {
            "name": skill_dir.name,
            "title": title,
            "description": description[:150] + "..." if len(description) > 150 else description,
            "path": path_str,
        }

    def _get_installed_skills(self, agent_config: GlobalConfig) -> List[Dict[str, Any]]:
        """获取所有已安装的技能"""
        skills = []
        seen = set()

        for skills_dir in self._get_skills_directories(agent_config):
            for item in skills_dir.iterdir():
                if not item.is_dir() or item.name.startswith("."):
                    continue

                # 跳过重复（优先级高的目录先遍历）
                if item.name in seen:
                    continue
                seen.add(item.name)

                skill_info = self._parse_skill_metadata(item)
                if skill_info:
                    skills.append(skill_info)

        return skills

    def _build_skills_prompt_section(self, skills: List[Dict[str, Any]]) -> str:
        """构建技能列表的 prompt 部分"""
        if not skills:
            return ""

        lines = [
            "# 已安装技能",
            "",
            "以下是当前可用的技能。你可以根据用户需求调用这些技能：",
            "",
        ]

        for skill in skills:
            lines.append(f"- **{skill['title']}** (`{skill['name']}`)")
            if skill['description']:
                lines.append(f"  {skill['description']}")

        lines.append("")
        lines.append("要使用技能，请调用 `_load_resource_skill(skill_name)` 加载详细说明。")
        lines.append("")
        lines.append("## 发现更多技能")
        lines.append("")
        lines.append(
            "技能注册表 `~/.alfred/skills-registry.json` 包含更多可安装的技能。"
            "用 `_bash` 或 `_read_file` 读取该文件即可查看完整目录。"
        )

        return "\n".join(lines)

    def _upsert_skills_prompt_section(self, workspace_instructions: str, skills_section: str) -> str:
        """Replace or append the auto-generated skills section in workspace instructions."""
        block = (
            f"{self.SKILLS_SECTION_START}\n"
            f"{skills_section}\n"
            f"{self.SKILLS_SECTION_END}"
        )
        pattern = re.compile(
            rf"{re.escape(self.SKILLS_SECTION_START)}.*?{re.escape(self.SKILLS_SECTION_END)}",
            re.DOTALL,
        )
        if pattern.search(workspace_instructions):
            return pattern.sub(block, workspace_instructions)

        if workspace_instructions.strip():
            return workspace_instructions + "\n\n---\n\n" + block
        return block

    def _load_disabled_skills(self) -> Set[str]:
        """Load disabled skill names from ~/.alfred/skills-state.json"""
        state_file = Path.home() / ".alfred" / "skills-state.json"
        if not state_file.exists():
            return set()
        try:
            with open(state_file) as f:
                state = json.load(f)
                return set(state.get("disabled", []))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to read skills-state.json: %s", e)
            return set()

    def _extract_runtime_available_skills(self, global_skills: Any) -> List[Dict[str, Any]]:
        """Extract actually available resource skills from runtime skillkit."""
        resource_skillkit = None

        # Locate the ResourceSkillkit via the owner_skillkit binding on
        # the _load_resource_skill function that it registers.
        installed = getattr(global_skills, "installedSkillset", None)
        if installed is not None:
            loader_skill = installed.getSkill("_load_resource_skill") if hasattr(installed, "getSkill") else None
            if loader_skill is not None:
                resource_skillkit = getattr(loader_skill, "owner_skillkit", None)

        if resource_skillkit is None:
            logger.debug("ResourceSkillkit not found via owner_skillkit, skills injection skipped.")
            return []

        get_available_skills = getattr(resource_skillkit, "get_available_skills", None)
        get_skill_meta = getattr(resource_skillkit, "get_skill_meta", None)
        if not callable(get_available_skills):
            return []

        available_skills = []
        for name in get_available_skills() or []:
            title = name
            description = ""
            path_str = ""
            if callable(get_skill_meta):
                try:
                    meta = get_skill_meta(name)
                except Exception:
                    logger.debug("Failed to get metadata for skill %r", name, exc_info=True)
                    meta = None
                if meta is not None:
                    title = getattr(meta, "name", None) or title
                    description = getattr(meta, "description", "") or ""
                    base_path = getattr(meta, "base_path", "")
                    if base_path:
                        path_str = str(base_path)
                        home_dir = str(Path.home())
                        if path_str.startswith(home_dir):
                            path_str = "~" + path_str[len(home_dir):]

            available_skills.append(
                {
                    "name": name,
                    "title": title,
                    "description": description[:150] + "..." if len(description) > 150 else description,
                    "path": path_str,
                }
            )

        # Filter out disabled skills
        disabled = self._load_disabled_skills()
        if disabled:
            before_count = len(available_skills)
            available_skills = [s for s in available_skills if s["name"] not in disabled]
            filtered_count = before_count - len(available_skills)
            if filtered_count:
                logger.info("Filtered out %d disabled skill(s).", filtered_count)

        return available_skills


# 创建全局单例
_default_factory: Optional[AgentFactory] = None


def get_agent_factory(
    global_config_path: Optional[str] = None,
    default_model: Optional[str] = None,
) -> AgentFactory:
    """
    获取全局 Agent 工厂单例

    Args:
        global_config_path: Dolphin 全局配置文件路径
        default_model: 默认模型名称

    Returns:
        AgentFactory 实例
    """
    global _default_factory
    if _default_factory is None:
        _default_factory = AgentFactory(
            global_config_path=global_config_path,
            default_model=default_model,
        )
    return _default_factory


async def create_agent(agent_name: str, workspace_path: Path) -> DolphinAgent:
    """
    便捷函数：创建 Agent

    使用全局单例工厂创建 Agent。

    Args:
        agent_name: Agent 名称
        workspace_path: Agent 工作区路径

    Returns:
        初始化完成的 DolphinAgent 实例
    """
    factory = get_agent_factory()
    return await factory.create_agent(agent_name, workspace_path)
