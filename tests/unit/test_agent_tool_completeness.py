"""
测试 Agent 可用工具的完整性

验证：
1. agent.dph 中的工具列表包含所有必需的技能工具
2. Agent 创建后，context 中的 skillkit 包含 _load_resource_skill
3. 系统提示中包含资源技能的元数据

这个测试是为了防止类似的配置遗漏问题再次发生。
"""

import pytest
import asyncio
from pathlib import Path
from src.everbot.core.agent.factory import AgentFactory
from src.everbot.infra.user_data import UserDataManager
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
    """测试 Agent 工具配置的完整性"""

    @pytest.fixture
    def demo_agent(self):
        """创建 demo_agent 实例（同步版本，内部运行异步代码）"""
        import asyncio
        
        async def create_agent():
            user_data = UserDataManager()
            demo_workspace = user_data.agents_dir / "demo_agent"

            if not demo_workspace.exists():
                return None

            factory = AgentFactory()
            agent = await factory.create_agent(
                agent_name="demo_agent",
                workspace_path=demo_workspace
            )
            return agent
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            agent = loop.run_until_complete(create_agent())
        finally:
            loop.close()
        if agent is None:
            pytest.skip("Demo agent 工作区不存在")
        return agent

    def test_agent_dph_contains_required_tools(self):
        """测试 agent.dph 文件包含所有必需的工具"""
        user_data = UserDataManager()
        agent_dph_path = user_data.agents_dir / "demo_agent" / "agent.dph"

        if not agent_dph_path.exists():
            pytest.skip(f"agent.dph 文件不存在: {agent_dph_path}")

        content = agent_dph_path.read_text(encoding="utf-8")

        # 检查每个必需的工具
        missing_tools = []
        for tool in REQUIRED_TOOLS:
            if tool not in content:
                missing_tools.append(tool)

        assert not missing_tools, (
            f"agent.dph 缺少以下必需工具: {missing_tools}\n"
            f"请将这些工具添加到 agent.dph 的 tools 参数中"
        )

    def test_skillkit_has_load_resource_skill(self, demo_agent):
        """测试 context 中的 skillkit 包含 _load_resource_skill"""
        context = demo_agent.executor.context
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

    def test_resource_skills_metadata_available(self, demo_agent):
        """测试资源技能的元数据可用"""
        context = demo_agent.executor.context
        skillkit = context.get_skillkit()

        assert skillkit is not None, "Context 中没有 skillkit"

        # 收集元数据
        metadata = Skillkit.collect_metadata_from_skills(skillkit)

        # 检查是否有资源技能的元数据
        assert "Available Resource Skills" in metadata or len(metadata) > 0, (
            "资源技能元数据为空。\n"
            "这可能意味着 ResourceSkillkit 没有正确加载，或者没有安装任何技能。"
        )

    def test_owner_skillkit_correctly_set(self, demo_agent):
        """测试技能的 owner_skillkit 正确设置"""
        context = demo_agent.executor.context
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

    def test_all_required_tools_available(self, demo_agent):
        """测试所有必需的工具都可用"""
        context = demo_agent.executor.context
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
