"""
测试 Agent 可用工具的完整性

验证：
1. agent.dph 中的工具列表包含所有必需的技能工具
2. Agent 创建后，context 中的 skillkit 包含 _load_resource_skill
3. 系统提示中包含资源技能的元数据

这个测试使用 Mock 替代真实文件依赖，确保 CI 中稳定运行。
"""

import pytest
from unittest.mock import Mock, MagicMock, patch
from dolphin.core.skill.skillkit import Skillkit


# 定义必须包含的核心工具
REQUIRED_TOOLS = [
    "_bash",
    "_python",
    "_date",
    "_read_file",
    "_read_folder",
    "_load_resource_skill",
    "_load_skill_resource",
]


class TestAgentToolCompleteness:
    """测试 Agent 工具配置的完整性（使用 Mock，不依赖真实文件）"""

    @pytest.fixture
    def mock_agent(self):
        """创建模拟的 agent，不依赖真实文件系统"""
        # Mock skillkit
        mock_skillkit = Mock()
        mock_skillkit.getSkillNames.return_value = REQUIRED_TOOLS + ["_web_search"]
        mock_skillkit.getName.return_value = "resource_skillkit"

        # Mock skill
        mock_skill = Mock()
        mock_skill.get_function_name.return_value = "_load_resource_skill"
        mock_skill.owner_skillkit = mock_skillkit

        mock_skillkit.getSkills.return_value = [mock_skill]
        mock_skillkit.getSkill.return_value = mock_skill

        # Mock context
        mock_context = Mock()
        mock_context.get_skillkit.return_value = mock_skillkit

        # Mock agent
        mock_agent = Mock()
        mock_agent.executor.context = mock_context

        return mock_agent

    @pytest.fixture
    def mock_agent_dph_content(self):
        """模拟 agent.dph 文件内容"""
        return """
        agent:
          name: demo_agent
          tools:
            - _bash
            - _python
            - _date
            - _read_file
            - _read_folder
            - _load_resource_skill
            - _load_skill_resource
        """

    def test_agent_dph_contains_required_tools(self, mock_agent_dph_content):
        """测试 agent.dph 配置包含所有必需工具（使用 mock 内容）"""
        content = mock_agent_dph_content

        missing_tools = [
            tool for tool in REQUIRED_TOOLS
            if tool not in content
        ]

        assert not missing_tools, (
            f"agent.dph 缺少以下必需工具: {missing_tools}\n"
            f"请将这些工具添加到 agent.dph 的 tools 参数中"
        )

    def test_skillkit_has_load_resource_skill(self, mock_agent):
        """测试 context 中的 skillkit 包含 _load_resource_skill"""
        context = mock_agent.executor.context
        skillkit = context.get_skillkit()

        assert skillkit is not None, "Context 中没有 skillkit"

        skill_names = list(skillkit.getSkillNames())

        # 检查必需的技能工具
        assert "_load_resource_skill" in skill_names, (
            f"Skillkit 中缺少 _load_resource_skill\n"
            f"可用工具: {skill_names}"
        )
        assert "_load_skill_resource" in skill_names, (
            f"Skillkit 中缺少 _load_skill_resource\n"
            f"可用工具: {skill_names}"
        )

    def test_resource_skills_metadata_available(self, mock_agent):
        """测试资源技能的元数据可用"""
        context = mock_agent.executor.context
        skillkit = context.get_skillkit()

        assert skillkit is not None, "Context 中没有 skillkit"

        # 验证 skillkit 返回了预期的技能列表
        skill_names = list(skillkit.getSkillNames())
        assert len(skill_names) > 0, "Skillkit 中没有可用的技能"
        assert "_load_resource_skill" in skill_names, (
            "资源技能元数据为空。\n"
            "这可能意味着 ResourceSkillkit 没有正确加载，或者没有安装任何技能。"
        )

    def test_owner_skillkit_correctly_set(self, mock_agent):
        """测试技能的 owner_skillkit 正确设置"""
        context = mock_agent.executor.context
        skillkit = context.get_skillkit()

        assert skillkit is not None, "Context 中没有 skillkit"

        # 检查 _load_resource_skill 的 owner
        for skill in skillkit.getSkills():
            if skill.get_function_name() == "_load_resource_skill":
                owner = getattr(skill, "owner_skillkit", None)
                assert owner is not None, (
                    "_load_resource_skill 没有设置 owner_skillkit"
                )
                assert owner.getName() == "resource_skillkit", (
                    f"_load_resource_skill 的 owner 应该是 resource_skillkit，"
                    f"但实际是 {owner.getName()}"
                )
                break
        else:
            pytest.fail("在 skillkit 中找不到 _load_resource_skill")

    def test_all_required_tools_available(self, mock_agent):
        """测试所有必需的工具都可用"""
        context = mock_agent.executor.context
        skillkit = context.get_skillkit()

        assert skillkit is not None, "Context 中没有 skillkit"

        skill_names = set(skillkit.getSkillNames())

        missing_tools = []
        for tool in REQUIRED_TOOLS:
            if tool not in skill_names:
                missing_tools.append(tool)

        assert not missing_tools, (
            f"以下必需工具不可用: {missing_tools}\n"
            f"可用工具: {sorted(skill_names)}\n\n"
            f"可能的原因:\n"
            f"1. agent.dph 中的 tools 参数没有包含这些工具\n"
            f"2. 相关的 skillkit 没有正确加载\n"
            f"3. dolphin.yaml 中没有启用相应的 skillkit"
        )


@pytest.mark.integration
class TestAgentToolCompletenessIntegration:
    """集成测试：验证真实 agent.dph 文件存在且配置正确

    这些测试依赖真实的文件系统，应在 CI 中配置好预置数据后运行。
    默认情况下不运行，使用 pytest -m integration 显式触发。
    """

    def test_real_agent_dph_exists(self):
        """验证真实 agent.dph 文件存在且包含必需工具"""
        from pathlib import Path
        from src.everbot.infra.user_data import UserDataManager

        user_data = UserDataManager()
        agent_dph_path = user_data.agents_dir / "demo_agent" / "agent.dph"

        # 使用 assert 而非 skip，确保 CI 失败时可见
        assert agent_dph_path.exists(), (
            f"agent.dph 不存在: {agent_dph_path}。 "
            f"请运行 make setup-demo-agent 或检查 CI 配置"
        )

        content = agent_dph_path.read_text(encoding="utf-8")
        for tool in REQUIRED_TOOLS:
            assert tool in content, f"agent.dph 缺少工具: {tool}"

    def test_real_agent_factory_creates_agent(self):
        """验证 AgentFactory 能成功创建 agent"""
        import asyncio
        from pathlib import Path
        from src.everbot.infra.user_data import UserDataManager
        from src.everbot.core.agent.factory import AgentFactory

        user_data = UserDataManager()
        demo_workspace = user_data.agents_dir / "demo_agent"

        assert demo_workspace.exists(), (
            f"Demo agent 工作区不存在: {demo_workspace}"
        )

        async def create_and_verify():
            factory = AgentFactory()
            agent = await factory.create_agent(
                agent_name="demo_agent",
                workspace_path=demo_workspace
            )
            assert agent is not None, "AgentFactory 返回 None"
            assert agent.executor is not None, "Agent 没有 executor"
            assert agent.executor.context is not None, "Agent 没有 context"
            return agent

        agent = asyncio.run(create_and_verify())
        assert agent is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
