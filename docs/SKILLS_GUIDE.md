# Skills 系统使用指南

## 概述

EverBot 的 Skills 系统让 agent 动态发现和使用高层指令集（Skills）来增强自己的能力。milkie provider 在 spawn agent sidecar 时扫描工作区下的 `SKILL.md`（`discover_skills`），把技能元数据注入 system prompt 的技能段，并产出 `skill_list` manifest；agent 经 milkie 内建的 `run_command`（milkie#134）读取 `SKILL.md` 正文并执行其中的脚本。

## 架构设计

### 工作区扫描 + 按优先级合并目录

每个 agent 的 skills 由 `discover_skills` 在 spawn 时按目录优先级扫描合并，无需中心化注册：

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
        ├── skills/                   # Agent 工作区专属 skills（最高优先级）
        │   └── custom-skill/
        │       └── SKILL.md
        ├── AGENTS.md
        └── SKILLS.md                 # 工作区合成 system prompt 的来源之一
```

### 关键实现

1. **技能发现** (`src/everbot/core/agent/provider/milkie/skills.py`):
   - `resolve_skill_dirs(workspace_path)` 按优先级返回存在的 skill 目录（高优先级在前）
   - `discover_skills(...)` 扫描各目录下的 `SKILL.md`，解析 frontmatter，注入 prompt 技能段并产出 `skill_list` manifest
   - `build_milkie_skills_section(...)` 拼装注入 system prompt 的技能段

2. **per-agent allowlist** (`~/.alfred/config.yaml`):
   ```yaml
   everbot:
     agents:
       demo_agent:
         skills:
           include: []      # 非空则只保留其中的 skill
           exclude: []      # 移除其中的 skill
   ```
   `include` 引用未发现的 skill 名会 fail-loud（`ValueError`）；`exclude` 引用不存在者仅告警忽略。

3. **Skills 目录优先级**（`resolve_skill_dirs`）:
   - Agent 工作区专属: `<workspace>/skills/` （最高）
   - 全局共享: `~/.alfred/skills/` （次级）
   - 仓库内置: `<repo>/skills/` （兜底）

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

Agent sidecar spawn 时会自动扫描所有 skills 目录，可用 skills 的元数据会注入到 system prompt 的技能段（并以 `skill_list` manifest 形式呈现）。

### 2. 加载并执行 Skill

agent 经 milkie 内建的 `run_command`（milkie#134）读取目标 skill 的 `SKILL.md` 正文，并运行其中声明的脚本：

```bash
# 读取 skill 的完整指令
cat ~/.alfred/skills/web-research/SKILL.md

# 执行 skill 提供的脚本
python ~/.alfred/skills/web-research/scripts/search.py
```

### 3. Agent 自主下载 Skills

Agent 同样经 `run_command` 下载远程 skills：

```bash
skill_dir=~/.alfred/agents/demo_agent/skills/new-skill
mkdir -p "$skill_dir"
curl -fsSL -o "$skill_dir/SKILL.md" \
  https://example.com/skills/new-skill/SKILL.md
```

下载后**无需重启**:下一轮对话该 skill 即自动可用(daemon 检测到技能集变化会自动重生对应 agent 的 milkie sidecar,#43)。

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
- ✓ skills 目录被正确发现
- ✓ Agent 工作区专属目录在最高优先级
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
Agent: [经 run_command 读取 SKILL.md 并应用指令]
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

3. 下一轮对话该 skill 即自动可用（无需重启,#43）。

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
- 检查 skill 是否落在 `resolve_skill_dirs` 扫描的目录下（工作区 `skills/` / `~/.alfred/skills/` / 仓库 `skills/`），以及未被 `everbot.agents.<name>.skills.exclude` 过滤
- 发起新一轮对话触发重新发现（daemon 按技能集指纹变化自动重生 sidecar,#43;若仍未发现,查 daemon 日志中的 `discover_skills` 告警）

### 加载失败

- 检查 skill 名称是否正确
- 检查文件权限
- 查看日志了解详细错误信息

## 参考资料

- 技能发现/注入实现: `src/everbot/core/agent/provider/milkie/skills.py`（`discover_skills` / `build_milkie_skills_section`）；脚本执行经 milkie 内建 `run_command`（milkie#134）
- EverBot 设计文档: `docs/EVERBOT_DESIGN.md`
- 示例 skills: `~/.alfred/skills/`
