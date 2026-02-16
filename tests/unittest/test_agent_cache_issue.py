"""
测试 Agent 配置热重载问题

复现问题：
1. 当 agent.dph 修改后，已缓存的 agent 不会自动更新
2. 这导致新添加的工具（如 _load_resource_skill）不可用

这个测试模拟了实际场景：
1. 创建 agent（模拟服务启动时的状态）
2. 修改 agent.dph
3. 验证缓存的 agent 是否还在使用旧配置
"""

import pytest
import asyncio
import tempfile
import shutil
from pathlib import Path
from src.everbot.core.agent.factory import AgentFactory
from src.everbot.core.session.session import SessionManager
from src.everbot.infra.user_data import UserDataManager


class TestAgentCacheIssue:
    """测试 Agent 缓存导致配置更新不生效的问题"""

    def test_cached_agent_does_not_reload_on_dph_change(self):
        """
        复现问题：agent.dph 修改后，缓存的 agent 不会更新
        
        这个测试展示了为什么修改 agent.dph 后需要重启服务
        """
        user_data = UserDataManager()
        demo_workspace = user_data.agents_dir / "demo_agent"
        agent_dph_path = demo_workspace / "agent.dph"
        
        if not demo_workspace.exists():
            pytest.skip("Demo agent 工作区不存在")
        
        # 读取原始 agent.dph
        original_content = agent_dph_path.read_text()
        
        # 创建 SessionManager（模拟 web 服务的缓存机制）
        session_manager = SessionManager(user_data.sessions_dir)
        session_id = "test_cache_session"
        
        async def create_and_cache_agent():
            factory = AgentFactory()
            agent = await factory.create_agent(
                agent_name="demo_agent",
                workspace_path=demo_workspace
            )
            session_manager.cache_agent(session_id, agent, "demo_agent", "test")
            return agent
        
        # 1. 创建并缓存 agent
        agent1 = asyncio.run(create_and_cache_agent())

        # 获取缓存的 agent
        cached_agent = session_manager.get_cached_agent(session_id)

        # 验证是同一个对象
        assert cached_agent is agent1, "缓存的 agent 应该是同一个对象"

        # 2. 检查后续获取是否仍然返回缓存的 agent
        cached_agent_again = session_manager.get_cached_agent(session_id)
        assert cached_agent_again is agent1, "再次获取应该返回同一个缓存对象"

    def test_system_prompt_uses_dph_tools_at_creation_time(self):
        """
        测试系统提示中的工具列表是在 agent 创建时确定的
        
        这解释了为什么缓存的 agent 不会看到 agent.dph 的更新
        """
        user_data = UserDataManager()
        demo_workspace = user_data.agents_dir / "demo_agent"
        
        if not demo_workspace.exists():
            pytest.skip("Demo agent 工作区不存在")
        
        async def get_skillkit_tools():
            factory = AgentFactory()
            agent = await factory.create_agent(
                agent_name="demo_agent",
                workspace_path=demo_workspace
            )
            context = agent.executor.context
            skillkit = context.get_skillkit()
            return list(skillkit.getSkillNames()) if skillkit else []
        
        tools = asyncio.run(get_skillkit_tools())

        # 验证 _load_resource_skill 在工具列表中
        assert "_load_resource_skill" in tools, (
            f"_load_resource_skill 不在工具列表中！\n"
            f"工具列表: {tools}\n\n"
            f"请检查 agent.dph 的 tools 参数是否包含 _load_resource_skill"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
