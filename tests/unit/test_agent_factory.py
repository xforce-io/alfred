"""
Agent Factory 测试
"""

import pytest
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch
import tempfile

from src.everbot.infra.user_data import UserDataManager
from src.everbot.core.agent.factory import AgentFactory, get_agent_factory
import src.everbot.core.agent.factory as factory_module


@pytest.mark.asyncio
async def test_agent_factory_create():
    """测试 Agent 创建"""
    with tempfile.TemporaryDirectory() as tmpdir:
        # 1. 初始化工作区
        manager = UserDataManager(alfred_home=Path(tmpdir))
        manager.ensure_directories()
        manager.init_agent_workspace("test_agent")

        # 2. 创建 Agent 工厂
        factory = AgentFactory(
            global_config_path="",  # 使用默认配置
            default_model="gpt-4",
        )

        # 3. 创建 Agent
        workspace_path = manager.get_agent_dir("test_agent")
        agent = await factory.create_agent("test_agent", workspace_path)

        # 4. 验证
        assert agent is not None
        assert agent.name == "test_agent"
        assert hasattr(agent, "executor")
        assert hasattr(agent.executor, "context")

        # 5. 验证 Context 变量
        context = agent.executor.context
        assert context.get_var_value("agent_name") == "test_agent"
        assert context.get_var_value("model_name") == "gpt-4"
        assert context.get_var_value("workspace_instructions") is not None


@pytest.mark.asyncio
async def test_agent_factory_with_workspace_instructions():
    """测试工作区指令注入"""
    with tempfile.TemporaryDirectory() as tmpdir:
        # 1. 初始化工作区
        manager = UserDataManager(alfred_home=Path(tmpdir))
        manager.ensure_directories()
        manager.init_agent_workspace("test_agent")

        # 2. 自定义工作区文件
        agent_dir = manager.get_agent_dir("test_agent")
        (agent_dir / "AGENTS.md").write_text("# Test Agent Behavior")
        (agent_dir / "USER.md").write_text("# Test User Profile")

        # 3. 创建 Agent
        factory = AgentFactory()
        agent = await factory.create_agent("test_agent", agent_dir)

        # 4. 验证工作区指令已注入
        context = agent.executor.context
        instructions = context.get_var_value("workspace_instructions")
        assert "Test Agent Behavior" in instructions
        assert "Test User Profile" in instructions


@pytest.mark.asyncio
async def test_agent_factory_legacy_yaml_agent_dph_is_supported():
    """Legacy YAML-style agent.dph should be migrated to a compatible DPH file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = UserDataManager(alfred_home=Path(tmpdir))
        manager.ensure_directories()
        manager.init_agent_workspace("legacy_agent")

        agent_dir = manager.get_agent_dir("legacy_agent")
        (agent_dir / "agent.dph").write_text(
            """# legacy YAML-style definition
name: legacy_agent
description: Legacy agent
system_prompt: |
  $workspace_instructions
model:
  name: $model_name
tools:
  - type: bash
    enabled: true
  - type: python
    enabled: true
""",
            encoding="utf-8",
        )

        factory = AgentFactory(global_config_path="", default_model="gpt-4")
        agent = await factory.create_agent("legacy_agent", agent_dir)

        assert agent is not None
        assert (agent_dir / "baks").exists()
        assert (agent_dir / "agent.dph").read_text(encoding="utf-8").strip().startswith("'''")


# ===========================================================================
# _normalize_skill_name
# ===========================================================================


class TestNormalizeSkillName:
    def test_hyphen_to_underscore(self):
        assert AgentFactory._normalize_skill_name("example-skill") == "example_skill"

    def test_uppercase_to_lower(self):
        assert AgentFactory._normalize_skill_name("Example_Skill") == "example_skill"

    def test_already_normalized(self):
        assert AgentFactory._normalize_skill_name("example_skill") == "example_skill"


# ===========================================================================
# _get_agent_skills_filter
# ===========================================================================


class TestGetAgentSkillsFilter:
    """Verify _get_agent_skills_filter reads config via self.global_config_path."""

    def _make_factory(self, config_path="/custom/config.yaml"):
        factory = AgentFactory.__new__(AgentFactory)
        factory.global_config_path = config_path
        return factory

    @patch("src.everbot.core.agent.factory.get_config")
    def test_reads_alfred_config_not_dolphin(self, mock_get_config):
        """Must call get_config() (Alfred config), not get_config(self.global_config_path)."""
        mock_get_config.return_value = {"everbot": {"agents": {}}}
        factory = self._make_factory("/my/config.yaml")

        factory._get_agent_skills_filter("some_agent")

        mock_get_config.assert_called_with()

    @patch("src.everbot.core.agent.factory.get_config")
    def test_include_mode(self, mock_get_config):
        mock_get_config.return_value = {
            "everbot": {"agents": {"cm": {"skills": {"include": ["example_skill", "invest"]}}}}
        }
        factory = self._make_factory()
        names, mode = factory._get_agent_skills_filter("cm")
        assert mode == "include"
        assert names == {"example_skill", "invest"}

    @patch("src.everbot.core.agent.factory.get_config")
    def test_exclude_mode(self, mock_get_config):
        mock_get_config.return_value = {
            "everbot": {"agents": {"cm": {"skills": {"exclude": ["debug_tool"]}}}}
        }
        factory = self._make_factory()
        names, mode = factory._get_agent_skills_filter("cm")
        assert mode == "exclude"
        assert names == {"debug_tool"}

    @patch("src.everbot.core.agent.factory.get_config")
    def test_no_filter_returns_all(self, mock_get_config):
        mock_get_config.return_value = {"everbot": {"agents": {"cm": {}}}}
        factory = self._make_factory()
        names, mode = factory._get_agent_skills_filter("cm")
        assert mode == "all"
        assert names is None

    @patch("src.everbot.core.agent.factory.get_config")
    def test_both_include_and_exclude_raises(self, mock_get_config):
        mock_get_config.return_value = {
            "everbot": {"agents": {"cm": {"skills": {
                "include": ["a"],
                "exclude": ["b"],
            }}}}
        }
        factory = self._make_factory()
        with pytest.raises(ValueError, match="both"):
            factory._get_agent_skills_filter("cm")

    @patch("src.everbot.core.agent.factory.get_config")
    def test_unknown_agent_returns_all(self, mock_get_config):
        mock_get_config.return_value = {"everbot": {"agents": {}}}
        factory = self._make_factory()
        names, mode = factory._get_agent_skills_filter("nonexistent")
        assert mode == "all"
        assert names is None


# ===========================================================================
# _load_custom_skillkits
# ===========================================================================


class TestLoadCustomSkillkits:
    """Verify _load_custom_skillkits uses correct config path and respects filters."""

    def _make_factory(self, config_path="/custom/config.yaml"):
        factory = AgentFactory.__new__(AgentFactory)
        factory.global_config_path = config_path
        return factory

    @patch("src.everbot.core.agent.factory.get_config")
    def test_reads_alfred_config_not_dolphin(self, mock_get_config):
        """Must call get_config() (Alfred config), not get_config(self.global_config_path)."""
        mock_get_config.return_value = {"everbot": {"agents": {}}}
        factory = self._make_factory("/my/special/config.yaml")
        agent = MagicMock()

        factory._load_custom_skillkits(agent, "test_agent")

        mock_get_config.assert_called_with()

    @patch("src.everbot.core.agent.factory.get_config")
    def test_no_skillkit_dirs_is_noop(self, mock_get_config):
        mock_get_config.return_value = {
            "everbot": {"agents": {"cm": {}}}
        }
        factory = self._make_factory()
        agent = MagicMock()

        factory._load_custom_skillkits(agent, "cm")

        # global_skills should never be accessed if no dirs configured
        agent.global_skills._loadCustomToolkitsFromPath.assert_not_called()

    @patch("src.everbot.core.agent.factory.get_config")
    def test_loads_skillkit_dir(self, mock_get_config):
        """Configured skillkit_dirs should be passed to GlobalSkills."""
        with tempfile.TemporaryDirectory() as tmpdir:
            skillkit_dir = Path(tmpdir) / "my_skillkit"
            skillkit_dir.mkdir()

            mock_get_config.return_value = {
                "everbot": {"agents": {"cm": {
                    "skillkit_dirs": [str(skillkit_dir)],
                }}}
            }
            factory = self._make_factory()
            gs = MagicMock()
            agent = MagicMock()
            agent.global_skills = gs

            factory._load_custom_skillkits(agent, "cm")

            gs._loadCustomToolkitsFromPath.assert_called_once_with(str(skillkit_dir))

    @patch("src.everbot.core.agent.factory.get_config")
    def test_include_filter_allows_matching_skillkit(self, mock_get_config):
        with tempfile.TemporaryDirectory() as tmpdir:
            skillkit_dir = Path(tmpdir) / "example-skill"
            skillkit_dir.mkdir()

            mock_get_config.return_value = {
                "everbot": {"agents": {"cm": {
                    "skillkit_dirs": [str(skillkit_dir)],
                    "skills": {"include": ["example_skill"]},
                }}}
            }
            factory = self._make_factory()
            gs = MagicMock()
            agent = MagicMock()
            agent.global_skills = gs

            factory._load_custom_skillkits(agent, "cm")

            gs._loadCustomToolkitsFromPath.assert_called_once()

    @patch("src.everbot.core.agent.factory.get_config")
    def test_include_filter_blocks_non_matching_skillkit(self, mock_get_config):
        with tempfile.TemporaryDirectory() as tmpdir:
            skillkit_dir = Path(tmpdir) / "debug-tools"
            skillkit_dir.mkdir()

            mock_get_config.return_value = {
                "everbot": {"agents": {"cm": {
                    "skillkit_dirs": [str(skillkit_dir)],
                    "skills": {"include": ["example_skill"]},
                }}}
            }
            factory = self._make_factory()
            gs = MagicMock()
            agent = MagicMock()
            agent.global_skills = gs

            factory._load_custom_skillkits(agent, "cm")

            gs._loadCustomToolkitsFromPath.assert_not_called()

    @patch("src.everbot.core.agent.factory.get_config")
    def test_exclude_filter_blocks_matching_skillkit(self, mock_get_config):
        with tempfile.TemporaryDirectory() as tmpdir:
            skillkit_dir = Path(tmpdir) / "example-skill"
            skillkit_dir.mkdir()

            mock_get_config.return_value = {
                "everbot": {"agents": {"cm": {
                    "skillkit_dirs": [str(skillkit_dir)],
                    "skills": {"exclude": ["example_skill"]},
                }}}
            }
            factory = self._make_factory()
            gs = MagicMock()
            agent = MagicMock()
            agent.global_skills = gs

            factory._load_custom_skillkits(agent, "cm")

            gs._loadCustomToolkitsFromPath.assert_not_called()

    @patch("src.everbot.core.agent.factory.get_config")
    def test_exclude_filter_allows_non_matching_skillkit(self, mock_get_config):
        with tempfile.TemporaryDirectory() as tmpdir:
            skillkit_dir = Path(tmpdir) / "example-skill"
            skillkit_dir.mkdir()

            mock_get_config.return_value = {
                "everbot": {"agents": {"cm": {
                    "skillkit_dirs": [str(skillkit_dir)],
                    "skills": {"exclude": ["debug_tools"]},
                }}}
            }
            factory = self._make_factory()
            gs = MagicMock()
            agent = MagicMock()
            agent.global_skills = gs

            factory._load_custom_skillkits(agent, "cm")

            gs._loadCustomToolkitsFromPath.assert_called_once()

    @patch("src.everbot.core.agent.factory.get_config")
    def test_hyphen_underscore_normalization_in_filter(self, mock_get_config):
        """skills.include=['example-skill'] should match dir 'example_skill' and vice versa."""
        with tempfile.TemporaryDirectory() as tmpdir:
            skillkit_dir = Path(tmpdir) / "example_skill"
            skillkit_dir.mkdir()

            mock_get_config.return_value = {
                "everbot": {"agents": {"cm": {
                    "skillkit_dirs": [str(skillkit_dir)],
                    "skills": {"include": ["example-skill"]},
                }}}
            }
            factory = self._make_factory()
            gs = MagicMock()
            agent = MagicMock()
            agent.global_skills = gs

            factory._load_custom_skillkits(agent, "cm")

            gs._loadCustomToolkitsFromPath.assert_called_once()

    @patch("src.everbot.core.agent.factory.get_config")
    def test_load_failure_does_not_propagate(self, mock_get_config):
        """_loadCustomToolkitsFromPath raising should be caught, not propagated."""
        with tempfile.TemporaryDirectory() as tmpdir:
            skillkit_dir = Path(tmpdir) / "bad_skillkit"
            skillkit_dir.mkdir()

            mock_get_config.return_value = {
                "everbot": {"agents": {"cm": {
                    "skillkit_dirs": [str(skillkit_dir)],
                }}}
            }
            factory = self._make_factory()
            gs = MagicMock()
            gs._loadCustomToolkitsFromPath.side_effect = RuntimeError("import failed")
            agent = MagicMock()
            agent.global_skills = gs

            # Should not raise
            factory._load_custom_skillkits(agent, "cm")


# ===========================================================================
# _syncAllTools after custom skillkit loading
# ===========================================================================


class TestSyncAllToolsAfterCustomLoad:
    """Custom skillkits must be visible in allTools, not just installedToolSet."""

    @pytest.mark.asyncio
    async def test_create_agent_syncs_alltools_after_custom_load(self):
        """After _load_custom_skillkits, _syncAllTools must be called so
        custom tools appear in context.all_skills (used by tools= filtering)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = UserDataManager(alfred_home=Path(tmpdir))
            manager.ensure_directories()
            manager.init_agent_workspace("test_agent")

            factory = AgentFactory(global_config_path="", default_model="gpt-4")

            workspace_path = manager.get_agent_dir("test_agent")
            agent = await factory.create_agent("test_agent", workspace_path)

            # After creation, allTools should be in sync with installedToolSet
            gs = getattr(agent, "global_skills", None)
            if gs is not None:
                installed_names = set(gs.installedToolSet.getToolNames())
                all_names = set(gs.allTools.getToolNames())
                # Every installed tool must be in allTools
                missing = installed_names - all_names
                assert not missing, (
                    f"Tools in installedToolSet but not in allTools: {missing}. "
                    f"_syncAllTools was not called after custom skillkit loading."
                )


# ===========================================================================
# get_agent_factory thread-safety
# ===========================================================================


class TestGetAgentFactoryThreadSafety:
    """Verify the singleton is created exactly once under concurrent access."""

    def setup_method(self):
        # Reset singleton state before each test
        factory_module._default_factory = None

    def teardown_method(self):
        factory_module._default_factory = None

    def test_concurrent_access_creates_single_instance(self):
        """Multiple threads calling get_agent_factory simultaneously
        should all receive the same instance."""
        results = []
        barrier = threading.Barrier(10)

        def worker():
            barrier.wait()
            instance = get_agent_factory(global_config_path="", default_model="test")
            results.append(instance)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 10
        assert all(r is results[0] for r in results), (
            "get_agent_factory returned different instances from concurrent calls"
        )

    def test_singleton_reused_on_subsequent_calls(self):
        """Subsequent calls return the same instance without locking overhead."""
        first = get_agent_factory(global_config_path="", default_model="m1")
        second = get_agent_factory()
        assert first is second


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
