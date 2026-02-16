"""
真实 Dolphin Agent 使用示例

演示如何使用 EverBot 创建和运行真实的 Dolphin Agent。
"""

import asyncio
from pathlib import Path

from src.everbot import (
    UserDataManager,
    AgentFactory,
    WorkspaceLoader,
)


async def demo_create_and_chat():
    """演示创建 Agent 并进行对话"""
    print("=" * 60)
    print("示例: 创建真实的 Dolphin Agent 并对话")
    print("=" * 60)

    # 1. 初始化
    user_data = UserDataManager()
    user_data.ensure_directories()

    # 2. 创建 Agent 工作区
    agent_name = "chat_assistant"
    user_data.init_agent_workspace(agent_name)
    agent_dir = user_data.get_agent_dir(agent_name)
    print(f"✓ Agent 工作区: {agent_dir}")

    # 3. 自定义工作区文件
    (agent_dir / "AGENTS.md").write_text("""# Chat Assistant 行为规范

## 身份
你是一个友好的聊天助理。

## 核心职责
1. 回答用户问题
2. 提供有用的建议
3. 进行友好的对话

## 沟通风格
- 友好、热情
- 简洁明了
- 适当使用emoji（如果合适）
""")

    # 4. 创建 Agent
    factory = AgentFactory(default_model="gpt-4")
    agent = await factory.create_agent(agent_name, agent_dir)
    print(f"✓ Agent 已创建: {agent.name}")

    # 5. 验证 Context
    context = agent.executor.context
    print(f"✓ 工作区指令已加载: {len(context.get_var_value('workspace_instructions'))} 字符")

    # 6. 进行对话
    print("\n" + "=" * 60)
    print("开始对话（输入 'quit' 退出）")
    print("=" * 60)

    while True:
        user_input = input("\n你: ").strip()
        if user_input.lower() in ["quit", "exit", "q"]:
            break

        if not user_input:
            continue

        print("助手: ", end="", flush=True)

        try:
            # 使用 continue_chat 方法
            response = ""
            async for event in agent.continue_chat(message=user_input, stream_mode="delta"):
                if "_progress" in event:
                    for progress in event["_progress"]:
                        if progress.get("stage") == "llm":
                            answer = progress.get("answer", "")
                            if answer:
                                # 打印增量
                                delta = answer[len(response):]
                                print(delta, end="", flush=True)
                                response = answer

            print()  # 换行

        except Exception as e:
            print(f"\n错误: {e}")
            break

    print("\n对话结束！")


async def demo_agent_info():
    """演示查看 Agent 信息"""
    print("\n" + "=" * 60)
    print("示例: 查看 Agent 信息")
    print("=" * 60)

    user_data = UserDataManager()
    agents = user_data.list_agents()

    if not agents:
        print("暂无 Agent")
        return

    print(f"\n共 {len(agents)} 个 Agent:")
    for agent_name in agents:
        print(f"\n- {agent_name}")
        agent_dir = user_data.get_agent_dir(agent_name)
        print(f"  路径: {agent_dir}")

        # 加载工作区
        loader = WorkspaceLoader(agent_dir)
        instructions = loader.load()

        if instructions.agents_md:
            print(f"  ✓ AGENTS.md: {len(instructions.agents_md)} 字符")
        if instructions.heartbeat_md:
            print(f"  ✓ HEARTBEAT.md: {len(instructions.heartbeat_md)} 字符")


async def main():
    """主函数"""
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "info":
        await demo_agent_info()
    else:
        await demo_create_and_chat()


if __name__ == "__main__":
    asyncio.run(main())
