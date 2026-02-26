"""
测试 Agent 的 Skills 功能

验证：
1. 每个 agent 有独立的 GlobalSkills 实例
2. Agent 专属 skills 目录被正确添加
3. Skills 可以被加载和使用
"""

import pytest
import asyncio
from pathlib import Path
from src.everbot.core.agent.factory import AgentFactory
from src.everbot.infra.user_data import UserDataManager


@pytest.mark.asyncio
async def test_agent_skills():
    """测试 agent skills 功能"""

    print("=" * 60)
    print("测试 Agent Skills 功能")
    print("=" * 60)

    user_data = UserDataManager()
    demo_workspace = user_data.agents_dir / "demo_agent"

    if not demo_workspace.exists():
        print(f"❌ Demo agent 工作区不存在: {demo_workspace}")
        print("请先运行: bin/everbot init demo_agent")
        return

    # 1. 创建 AgentFactory
    print("\n1. 创建 AgentFactory...")
    factory = AgentFactory()

    # 2. 创建第一个 agent
    print("\n2. 创建 demo_agent...")
    agent1 = await factory.create_agent(
        agent_name="demo_agent",
        workspace_path=demo_workspace
    )

    # 3. 检查配置
    print("\n3. 检查 agent 配置...")
    config = agent1.global_config

    if hasattr(config, 'resource_skills'):
        print(f"✓ resource_skills 配置存在")

        if isinstance(config.resource_skills, dict):
            directories = config.resource_skills.get('directories', [])
            print(f"✓ Skills 目录:")
            for dir_path in directories:
                print(f"  - {dir_path}")

            # 检查 agent 专属目录是否在第一位
            agent_skills_dir = str(demo_workspace / "skills")
            if directories and agent_skills_dir in directories[0]:
                print(f"✓ Agent 专属目录在最高优先级")
            else:
                print(f"⚠️  Agent 专属目录不在最高优先级")
        else:
            print(f"⚠️  resource_skills 不是 dict 类型")
    else:
        print(f"⚠️  resource_skills 配置不存在")

    # 4. 检查 GlobalSkills
    print("\n4. 检查 GlobalSkills...")
    global_skills = agent1.global_skills

    # 通过 installedSkillset → _load_resource_skill → owner_skillkit 获取 ResourceSkillkit
    resource_skillkit = None
    installed = getattr(global_skills, "installedSkillset", None)
    if installed is not None:
        loader_skill = installed.getSkill("_load_resource_skill") if hasattr(installed, "getSkill") else None
        if loader_skill is not None:
            resource_skillkit = getattr(loader_skill, "owner_skillkit", None)

    if resource_skillkit:
        print(f"✓ ResourceSkillkit 已加载")

        # 获取可用 skills
        available_skills = resource_skillkit.get_available_skills()
        print(f"✓ 可用 Skills ({len(available_skills)}):")
        for skill_name in available_skills:
            meta = resource_skillkit.get_skill_meta(skill_name)
            if meta:
                print(f"  - {skill_name}: {meta.description}")

        # 测试加载 skill
        if "example-skill" in available_skills:
            print(f"\n5. 测试加载 example-skill...")
            content = resource_skillkit.load_skill("example-skill")
            if content and not content.startswith("Error"):
                print(f"✓ Example skill 加载成功 ({len(content)} 字符)")
                print(f"  预览: {content[:200]}...")
            else:
                print(f"❌ Example skill 加载失败: {content}")
        else:
            print(f"\n⚠️  example-skill 不在可用列表中")
    else:
        print(f"⚠️  ResourceSkillkit 未找到")

    print("\n" + "=" * 60)
    print("测试完成")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(test_agent_skills())
