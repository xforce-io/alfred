# Alfred 技能与工具协议 (Skills & Tools)

作为 Alfred 系统中的 Agent，你拥有一套可扩展的技能系统。为了保持高效，请遵循以下技能发现与使用协议。

## 1. 技能发现优先级 (Skill Discovery)

当你需要完成特定任务（如 PDF 处理、论文检索等）而内置工具不足时，请按以下顺序检索可用技能：

1.  **Agent 专属技能** (`$WORKSPACE_ROOT/skills/`): 当前 Agent 特有的技能，优先级最高。
2.  **全局安装技能** (`~/.alfred/skills/`): 系统全局可用的扩展能力。
3.  **技能注册表** (`~/.alfred/skills-registry.json`): 包含可安装但尚未部署的技能元数据。

## 2. 核心内置工具 (Core Tools)

这些工具是系统原生提供的，无需额外加载：

- `_bash`: 运行终端命令，管理文件系统。
- `_python`: 执行 Python 代码进行复杂计算或数据处理。
- `_read_file` / `_write_file`: 高效地读写本地文件。
- `read_url_content`: 检索网页或 PDF 的文本内容（静态）。
- `browser_subagent`: 启动自动化浏览器进行交互式网页操作。

## 3. 已安装扩展技能 (Installed Skills)

每个扩展技能目录中必须包含 `SKILL.md` 文件。加载技能前，请先阅读该文件以了解具体的 Tool 接口。

<!-- AUTO_SKILLS_SECTION_START -->
| 技能名称 | 标题 | 说明 | 位置 |
| :--- | :--- | :--- | :--- |
| `paper-discovery` | 论文探索 | 检索并分析最新的学术论文。 | `~/.alfred/agents/demo_agent/skills/` |
| `web-research` | 网页研究 | 进行深度的网络信息采集与归纳。 | `~/.alfred/skills/` |
| `pdf-tools` | PDF 工具集 | 处理 PDF 的合并、拆分与转换。 | `~/.alfred/skills/` (需确认) |
<!-- AUTO_SKILLS_SECTION_END -->

## 4. 技能使用规范 (Usage Guidelines)

- **先阅读说明**: 调用技能对应的 Tool 前，必须先 `view_file` 该技能目录下的 `SKILL.md`。
- **环境隔离**: 尽量在 Agent 的 `tmp/` 目录下进行中间操作。
- **反馈闭环**: 如果技能执行失败，请检查依赖环境或 `package.json`（如果存在）。

## 5. 如何添加新技能

1. 在技能目录下创建 `SKILL.md`，定义接口规范。
2. 运行 `npm install` (如有 `package.json`)。
3. 更新此文件（或等待系统自动同步）。
