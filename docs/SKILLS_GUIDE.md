# Skills 系统使用指南

## 概述

EverBot 的 Skills 系统基于 Dolphin 的 ResourceSkillkit，允许 agent 动态加载和使用高层指令集（Skills）来增强自己的能力。

## 架构设计

### 方案 B：独立 GlobalSkills 实例

每个 agent 拥有独立的 GlobalSkills 实例和专属的 skills 目录配置：

```
~/.alfred/
├── skills/                           # 全局共享 skills
│   ├── example-skill/
│   │   └── SKILL.md
│   └── web-research/
│       └── SKILL.md
│
└── agents/
    └── demo_agent/
        ├── skills/                   # Agent 专属 skills（最高优先级）
        │   └── custom-skill/
        │       └── SKILL.md
        ├── AGENTS.md
        └── agent.dph
```

### 关键实现

1. **AgentFactory 改造** (`src/everbot/agent_factory.py`):
   - 移除 `_global_skills` 缓存
   - 添加 `_create_agent_config()` 方法，为每个 agent 创建独立配置
   - 在 `create_agent()` 中为每个 agent 创建独立的 GlobalSkills

2. **配置管理** (`config/dolphin.yaml`):
   ```yaml
   resource_skills:
     enabled: true
     directories:
       - "~/.alfred/skills"  # 全局共享
       # agent 专属目录由 AgentFactory 动态添加
   ```

3. **Skills 目录优先级**:
   - Agent 专属: `~/.alfred/agents/{agent_name}/skills/` （最高）
   - 全局共享: `~/.alfred/skills/` （次级）

## Skill 格式

每个 skill 必须包含 `SKILL.md` 文件：

```markdown
---
name: skill-name
description: Brief description
version: "1.0.0"  # optional
tags: [tag1, tag2]  # optional
---

# Skill Instructions

Your detailed instructions, guides, examples, etc.

## Available Resources

You can add additional resources:
- `scripts/`: Executable scripts
- `references/`: Reference docs, data files
```

## 使用 Skills

### 1. 查看可用 Skills

Agent 启动时会自动扫描所有配置的 skills 目录，可用 skills 的元数据会注入到 system prompt 中。

### 2. 加载 Skill

使用工具函数加载完整的 skill 内容：

```python
# 加载 skill 的完整指令
_load_resource_skill(skill_name="web-research")

# 加载 skill 中的资源文件
_load_skill_resource(skill_name="web-research", resource_path="scripts/search.py")
```

### 3. Agent 自主下载 Skills

Agent 可以使用 `_bash` 或 `_python` 工具下载远程 skills：

```python
import subprocess
import os

skill_name = "new-skill"
skill_url = "https://example.com/skills/new-skill/SKILL.md"
skill_dir = os.path.expanduser(f"~/.alfred/agents/demo_agent/skills/{skill_name}")

# 创建目录
os.makedirs(skill_dir, exist_ok=True)

# 下载 SKILL.md
subprocess.run([
    "curl", "-fsSL", "-o", f"{skill_dir}/SKILL.md", skill_url
], check=True)
```

下载后需要重启 agent 或在下次对话时 skill 会自动可用。

## 示例 Skills

### 1. example-skill

位置: `~/.alfred/skills/example-skill/`

演示 SKILL.md 格式和使用方法的示例。

### 2. web-research

位置: `~/.alfred/skills/web-research/`

提供结构化网络调研方法论的 skill。

## 测试验证

运行测试脚本验证配置：

```bash
PYTHONPATH=src python tests/test_agent_skills.py
```

预期输出：
- ✓ resource_skills 配置存在
- ✓ Agent 专属目录在最高优先级
- ✓ Skills 目录列表正确

## 实际使用示例

启动 agent 并测试：

```bash
# 启动 everbot
bin/everbot start

# 在 web UI 或 CLI 中与 demo_agent 对话
User: 有哪些可用的 skills？
Agent: [列出可用 skills]

User: 加载 web-research skill
Agent: [使用 _load_resource_skill 加载并应用指令]
```

## 进阶：创建自定义 Skill

1. 创建 skill 目录：
   ```bash
   mkdir -p ~/.alfred/agents/demo_agent/skills/my-skill
   ```

2. 创建 `SKILL.md`：
   ```bash
   cat > ~/.alfred/agents/demo_agent/skills/my-skill/SKILL.md << 'EOF'
   ---
   name: my-skill
   description: My custom skill
   ---

   # My Skill Instructions

   [Your instructions here]
   EOF
   ```

3. 重启 agent，skill 会自动可用。

## 注意事项

1. **命名规范**: Skill 名称应使用 `kebab-case`，只包含字母、数字和连字符
2. **文件大小**: 默认限制单个 skill 包不超过 8MB
3. **安全性**: 只从可信来源下载 skills
4. **版本管理**: 使用 `version` 字段管理 skill 版本
5. **文档**: 为自定义 skills 提供清晰的使用说明

## 故障排查

### Skills 未被发现

- 检查 `SKILL.md` 文件是否存在
- 检查 YAML frontmatter 格式是否正确
- 检查 `resource_skills.directories` 配置
- 重启 agent

### 加载失败

- 检查 skill 名称是否正确
- 检查文件权限
- 查看日志了解详细错误信息

## 参考资料

- Dolphin ResourceSkillkit 源码: `/Users/xupeng/dev/github/dolphin/src/dolphin/lib/skillkits/resource/`
- EverBot 设计文档: `docs/EVERBOT_DESIGN.md`
- 示例 skills: `~/.alfred/skills/`
