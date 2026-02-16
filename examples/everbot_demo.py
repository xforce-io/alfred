"""
EverBot 使用示例

演示如何使用 EverBot 的各个组件。
"""

import asyncio
from pathlib import Path
from datetime import datetime

from src.everbot.infra.user_data import UserDataManager
from src.everbot.infra.workspace import WorkspaceLoader
from src.everbot.core.session.session import SessionManager, SessionData
from src.everbot.infra.config import load_config, save_config


async def demo_basic_setup():
    """演示基本设置"""
    print("=" * 60)
    print("示例 1: 基本设置")
    print("=" * 60)

    # 1. 初始化用户数据管理器
    user_data = UserDataManager()
    user_data.ensure_directories()
    print(f"✓ EverBot 目录: {user_data.alfred_home}")

    # 2. 创建 Agent 工作区
    agent_name = "demo_agent"
    user_data.init_agent_workspace(agent_name)
    agent_dir = user_data.get_agent_dir(agent_name)
    print(f"✓ Agent 工作区: {agent_dir}")

    # 3. 列出所有 Agent
    agents = user_data.list_agents()
    print(f"✓ 已有 Agent: {agents}")

    print()


async def demo_workspace_loading():
    """演示工作区加载"""
    print("=" * 60)
    print("示例 2: 工作区加载")
    print("=" * 60)

    user_data = UserDataManager()
    agent_name = "demo_agent"
    workspace_path = user_data.get_agent_dir(agent_name)

    # 1. 修改工作区文件
    heartbeat_path = workspace_path / "HEARTBEAT.md"
    heartbeat_path.write_text("""# 心跳任务

## 待办
- [ ] 检查今日新闻
- [ ] 生成日报

## 已完成
- [x] 初始化工作区 (2026-02-01)
""")

    agents_md_path = workspace_path / "AGENTS.md"
    agents_md_path.write_text("""# Demo Agent 行为规范

## 身份
你是一个演示助理。

## 核心职责
1. 展示 EverBot 功能
2. 响应心跳任务

## 沟通风格
- 友好
- 简洁
""")

    # 2. 加载工作区
    loader = WorkspaceLoader(workspace_path)
    instructions = loader.load()

    print(f"✓ AGENTS.md: {len(instructions.agents_md or '')} 字符")
    print(f"✓ HEARTBEAT.md: {len(instructions.heartbeat_md or '')} 字符")
    print(f"✓ MEMORY.md: {len(instructions.memory_md or '')} 字符")
    print(f"✓ USER.md: {len(instructions.user_md or '')} 字符")

    # 3. 构建系统提示
    system_prompt = loader.build_system_prompt()
    print(f"\n系统提示预览 ({len(system_prompt)} 字符):")
    print("-" * 60)
    print(system_prompt[:300] + "...")
    print("-" * 60)

    print()


async def demo_session_management():
    """演示 Session 管理"""
    print("=" * 60)
    print("示例 3: Session 管理")
    print("=" * 60)

    user_data = UserDataManager()
    session_manager = SessionManager(user_data.sessions_dir)

    # 1. 创建 Mock Agent
    class MockAgent:
        def __init__(self, name):
            self.name = name
            self.executor = MockExecutor()

    class MockExecutor:
        def __init__(self):
            self.context = MockContext()

    class MockContext:
        def __init__(self):
            self._variables = {}
            self._messages = []

        def get_variable(self, name):
            return self._variables.get(name)

        def set_variable(self, name, value):
            self._variables[name] = value

        def get_history_messages(self, normalize=False):
            return self._messages

        def set_history_messages(self, messages):
            self._messages = messages

        def set_session_id(self, session_id):
            pass

        def clear_history(self):
            self._messages = []

    # 2. 创建 Agent 并设置数据
    agent = MockAgent("demo_agent")
    agent.executor.context.set_variable("workspace_instructions", "Test instructions")
    agent.executor.context.set_variable("model_name", "gpt-4")
    agent.executor.context._messages = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]

    # 3. 保存 Session
    session_id = "demo_session_001"
    await session_manager.save_session(session_id, agent, model_name="gpt-4")
    print(f"✓ Session 已保存: {session_id}")

    # 4. 加载 Session
    session_data = await session_manager.load_session(session_id)
    if session_data:
        print(f"✓ Session 已加载: {session_data.session_id}")
        print(f"  - Agent: {session_data.agent_name}")
        print(f"  - 模型: {session_data.model_name}")
        print(f"  - 历史消息: {len(session_data.history_messages)} 条")
        print(f"  - 创建时间: {session_data.created_at}")

    # 5. 并发控制测试
    print("\n测试并发控制...")
    async with session_manager.session_context(session_id, timeout=5.0) as acquired:
        if acquired:
            print("✓ 成功获取 Session 锁")
            # 模拟操作
            await asyncio.sleep(0.1)
        else:
            print("✗ 获取 Session 锁失败")

    print()


async def demo_config_management():
    """演示配置管理"""
    print("=" * 60)
    print("示例 4: 配置管理")
    print("=" * 60)

    user_data = UserDataManager()
    config_path = user_data.alfred_home / "demo_config.yaml"

    # 1. 创建配置
    config = {
        "everbot": {
            "enabled": True,
            "agents": {
                "demo_agent": {
                    "workspace": str(user_data.get_agent_dir("demo_agent")),
                    "heartbeat": {
                        "enabled": True,
                        "interval": 30,
                        "active_hours": [8, 22],
                    }
                }
            }
        }
    }

    # 2. 保存配置
    save_config(config, str(config_path))
    print(f"✓ 配置已保存: {config_path}")

    # 3. 加载配置
    loaded_config = load_config(str(config_path))
    print(f"✓ 配置已加载")
    print(f"  - EverBot 启用: {loaded_config['everbot']['enabled']}")
    print(f"  - Agent 数量: {len(loaded_config['everbot']['agents'])}")

    print()


async def main():
    """主函数"""
    print("\n" + "=" * 60)
    print("EverBot 使用示例")
    print("=" * 60 + "\n")

    await demo_basic_setup()
    await demo_workspace_loading()
    await demo_session_management()
    await demo_config_management()

    print("=" * 60)
    print("所有示例执行完成！")
    print("=" * 60)
    print("\n下一步:")
    print("1. 查看 ~/.alfred/ 目录下生成的文件")
    print("2. 修改 ~/.alfred/agents/demo_agent/HEARTBEAT.md 添加任务")
    print("3. 运行 python -m src.everbot.cli start 启动守护进程")
    print()


if __name__ == "__main__":
    asyncio.run(main())
