# Feature 1: 更新 README 文档

## Spec
1. 检查当前 README.md 内容，识别需要更新的部分
2. 核实特性列表是否完整
3. 核实技能系统表格是否包含所有技能
4. 核实项目结构是否与代码库一致
5. 更新 Roadmap 状态
6. 确保快速开始和 CLI 命令文档准确

**Acceptance Criteria**:
- [ ] README 中的特性列表与当前代码库功能一致
- [ ] 技能系统表格包含所有已实现的技能
- [ ] 项目结构树与代码库目录结构一致
- [ ] CLI 命令说明准确可用
- [ ] Roadmap 反映当前开发计划

## Analysis

> 已验证：以下所有差异已通过实际代码库核实（2026-03-15）

### 1. 特性列表差异

README 当前列出 6 项特性，基本准确但遗漏了以下已实现能力：

- **缺失：技能管理 CLI** — `everbot skills` 子命令支持 search/install/list/update/remove/enable/disable（`cli/skills_cli.py`），README 特性列表未提及动态技能管理能力
- **缺失：Session 压缩** — `core/session/` 下已有基于 LLM 的历史摘要压缩机制，防止 token 膨胀，README 仅提到"对话历史自动持久化"
- **缺失：自省系统** — `core/scanners/`（session_scanner, reflection_state）+ `core/jobs/`（memory_review, task_discover, health_check）构成 Inspector 自动反思机制，README 未提及
- **缺失：工作流引擎** — `core/workflow/`（11 个模块：phase_runner, artifact, verification 等）支持 coding-master 多阶段工作流，README 未提及

### 2. 技能系统表格差异

README 列出 10 个技能，但代码库 `skills/` 下实际有 **15 个**技能目录。缺失的 5 个：

| 缺失技能 | 用途 |
|----------|------|
| **ops** | 运维可观测性（daemon/heartbeat/logs/metrics 诊断） |
| **memory-review** | 内联记忆整合（Session 重分析与事实提取） |
| **task-discover** | 内联任务发现（从对话中提取可执行任务） |
| **trajectory-reviewer** | 执行轨迹分析（错误检测、循环模式识别） |
| **web-search** | 多后端 Web 搜索（ddgs/tavily 自动降级） |

### 3. 项目结构差异

README 项目结构树与实际代码库有以下差异：

**src/everbot/ 缺失目录：**
- `channels/` — 渠道实现独立目录（telegram_channel.py, telegram_commands.py, telegram_media.py, telegram_skillkit.py）
- `core/jobs/` — 内联技能任务（task_discover.py, memory_review.py, health_check.py, llm_utils.py）
- `core/scanners/` — 变更检测器（base.py, session_scanner.py, reflection_state.py）
- `core/workflow/` — 工作流执行模块（phase_runner.py, artifact.py, verification.py 等 11 个文件）
- `web/static/` 和 `web/templates/` — Web 前端资源

**根目录缺失：**
- `bin/everbot-watchdog` — 看门狗守护进程脚本
- `scripts/` — 工具脚本目录（dev-setup.sh, view_trajectory.py）
- `loop_test/` — 循环测试框架（runner.py, evaluator.py, cases.yaml 等）
- `tests/cli/` — CLI 命令测试目录（3 个测试文件）

**需修正：**
- `tests/web/` → 实际目录名为 `tests/e2e/`
- `config/dolphin.yaml` — README 列出但实际不存在，config/ 下只有 `everbot.example.yaml`
- `skills/` 下应列出 15 个技能，而非 10 个
- 测试文件数量：~55 → 实际 **92 个**（unit=64, integration=18, e2e=7, cli=3）
- `docs/` 下新增 9 个设计文档未列出：evolving_design.md, glossary.md, group_conversation_design.md, heartbeat_refactor.md, history_policy_design.md, inspector_enrichment_design.md, multi_agent_orchestration_design.md, ops_skill_design.md, optimization.md

### 4. CLI 命令差异

README CLI 部分缺失以下命令（已验证 `bin/everbot` 脚本 L447 和 `cli/main.py`）：

- `./bin/everbot web [--host HOST] [--port PORT]` — 独立启动 Web 服务器（未列出）
- `./bin/everbot migrate-agent --agent NAME` — Agent 数据迁移/修复（未列出）
- `./bin/everbot skills <subcommand>` — 整套技能管理子命令 search/install/list/update/remove/enable/disable（未列出）
- `heartbeat --force` 选项未文档化
- `restart` 命令已存在于 shell 脚本中（L379），README 正确列出了它
- `start` 命令新增 `--ssl`/`--ssl-cert`/`--ssl-key`/`--web-host`/`--web-port` 选项未文档化
- `stop` 命令有 `--timeout N`/`--force|-9` 选项未文档化

### 5. Roadmap 状态

当前 Roadmap 列出 5 项均为未完成状态。根据代码库现状：
- "macOS launchd 深度集成" — 未实现
- "Metrics 和监控告警" — ops 技能已实现部分可观测性（daemon/heartbeat/logs 诊断），可标记为部分完成
- "多用户权限管理" — 未实现
- "技能市场（远程注册表）" — skill-installer 已支持注册表安装（skills_cli.py 有 search/install），可标记为部分完成
- "高级记忆功能（RAG、向量检索）" — 未实现

### 6. 架构图差异

README 架构图缺少已实现的组件：
- `Workflow` 模块（`core/workflow/` — 多阶段工作流执行）
- `Jobs` 模块（`core/jobs/` — 内联技能任务）
- `Scanners` 模块（`core/scanners/` — 变更检测）
- `Inspector` 系统（Jobs + Scanners 的编排层）
- `Channels` 独立模块（`channels/` — Telegram 实现：命令、媒体、技能注入）

### 7. 其他需更新内容

- 测试覆盖描述需更新：~55 → 92 个测试文件；添加 CLI 测试类别
- `docs/` 参考链接部分可补充 9 个新增设计文档
- 代码示例中的 import 路径需验证（`from src.everbot import create_agent, UserDataManager`）

## Plan

1. **更新特性列表** — 补充"动态技能管理"、"Session 智能压缩"、"自省系统"、"工作流引擎"四项新特性的描述
2. **更新技能系统表格** — 添加 ops、memory-review、task-discover、trajectory-reviewer、web-search 五个缺失技能的行，保持表格格式一致
3. **更新项目结构树** — 全面对齐实际代码库：
   - 添加 `channels/`、`core/jobs/`、`core/scanners/`、`core/workflow/`、`web/static/`、`web/templates/`
   - 添加 `bin/everbot-watchdog`、`scripts/`、`loop_test/`、`tests/cli/`
   - 修正 `tests/web/` → `tests/e2e/`
   - 移除不存在的 `config/dolphin.yaml`
   - 更新 `skills/` 列表为完整 15 个
   - 更新测试文件总数描述
4. **更新 CLI 命令部分** — 添加 `web`、`migrate-agent`、`skills` 子命令；补充 `heartbeat --force`、`start --ssl*`、`stop --timeout/--force` 选项
5. **更新 Roadmap** — 标记 "技能市场" 和 "Metrics 和监控告警" 为部分完成（附简要说明）
6. **更新测试部分** — 总数改为 ~92；分类改为 unit ~64、integration ~18、e2e ~7、cli ~3
7. **更新架构图** — 添加 Workflow、Jobs/Scanners(Inspector)、Channels 模块
8. **验证代码示例** — 检查 import 路径和 API 调用是否仍然有效
9. **更新 docs 参考链接** — 补充新增的重要设计文档链接（inspector_enrichment_design.md, multi_agent_orchestration_design.md 等）

## Test Results

## Dev Log
