"""
测试 Agent 实际加载和使用 Skills

这个测试会创建一个真实的 agent 并尝试调用 _load_resource_skill 工具。
"""

import pytest
import asyncio
from pathlib import Path
from src.everbot.core.agent.factory import AgentFactory
from src.everbot.infra.user_data import UserDataManager


@pytest.mark.asyncio
async def test_skill_loading():
    """测试实际加载 skill"""

    print("=" * 60)
    print("测试实际加载 Skills")
    print("=" * 60)

    user_data = UserDataManager()
    demo_workspace = user_data.agents_dir / "demo_agent"

    if not demo_workspace.exists():
        print(f"❌ Demo agent 工作区不存在: {demo_workspace}")
        return

    # 创建 agent
    print("\n1. 创建 demo_agent...")
    factory = AgentFactory()
    agent = await factory.create_agent(
        agent_name="demo_agent",
        workspace_path=demo_workspace
    )

    print(f"✓ Agent 创建成功")

    # 检查可用工具
    print("\n2. 检查可用工具...")
    executor = agent.executor

    # 获取所有可用的工具
    if hasattr(executor, 'skillkits'):
        print(f"✓ Executor 有 skillkits 属性")

        # 列出所有 skillkit
        for skillkit in executor.skillkits:
            skillkit_name = skillkit.getName()
            print(f"  - {skillkit_name}")

            # 如果是 resource_skillkit，检查其功能
            if skillkit_name == "resource_skillkit":
                print(f"\n3. 检查 ResourceSkillkit...")

                # 获取可用 skills
                if hasattr(skillkit, 'get_available_skills'):
                    skills = skillkit.get_available_skills()
                    print(f"✓ 可用 Skills ({len(skills)}):")
                    for skill in skills:
                        meta = skillkit.get_skill_meta(skill)
                        if meta:
                            print(f"    - {skill}")
                            print(f"      描述: {meta.description}")
                            print(f"      路径: {meta.base_path}")

                # 测试加载 skill
                print(f"\n4. 测试加载 example-skill...")
                if hasattr(skillkit, 'load_skill'):
                    content = skillkit.load_skill("example-skill")
                    if content and not content.startswith("Error"):
                        print(f"✓ 加载成功!")
                        print(f"  内容长度: {len(content)} 字符")
                        print(f"\n  内容预览:")
                        lines = content.split('\n')[:10]
                        for line in lines:
                            print(f"    {line}")
                    else:
                        print(f"❌ 加载失败: {content}")
    else:
        print(f"⚠️  Executor 没有 skillkits 属性")

    # 尝试通过对话调用 skill 工具
    print(f"\n5. 通过对话测试 skill 工具...")
    try:
        # 发送一个简单的消息测试 agent 是否能看到 skills
        result = await agent.continue_chat(
            user_input="列出所有可用的 resource skills"
        )

        print(f"✓ Agent 响应:")
        if result and hasattr(result, 'answer'):
            print(f"  {result.answer[:500]}...")
        elif isinstance(result, dict) and 'answer' in result:
            print(f"  {result['answer'][:500]}...")
        else:
            print(f"  {str(result)[:500]}...")
    except Exception as e:
        print(f"⚠️  调用失败: {e}")

    print("\n" + "=" * 60)
    print("测试完成")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(test_skill_loading())
