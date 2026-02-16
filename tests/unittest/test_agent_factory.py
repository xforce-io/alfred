"""
Agent Factory 测试
"""

import pytest
import asyncio
from pathlib import Path
import tempfile

from src.everbot.infra.user_data import UserDataManager
from src.everbot.core.agent.factory import AgentFactory


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


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
