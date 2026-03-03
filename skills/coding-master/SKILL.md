---
name: coding-master
description: "Code review, analysis, development, and ops for registered repos — review uncommitted changes, check git status, search code, run tests, fix bugs, develop features, and submit PRs"
version: "0.1.0"
tags: [coding, review, development, bugfix, pr, automation]
---

# Coding Master Skill

All commands use: `$D = python $SKILL_DIR/scripts/dispatch.py`

All commands return JSON: `{"ok": true, "data": {...}}` or `{"ok": false, "error": "...", "error_code": "..."}`. Always check `ok`. On error, check `error_code` and `hint` field for actionable recovery.

## Available Operations

根据用户意图自主选择操作并组合执行，不强制走预定义流程。

### 只读操作（无需 workspace lock）

| 命令 | 说明 | 典型场景 |
|------|------|---------|
| `$D quick-status --repos <name>` | git 状态/diff | "看下状态"、"有什么改动" |
| `$D quick-test --repos <name>` | 跑测试 | "跑下测试"、"测试过了吗" |
| `$D quick-find --repos <name>` | 搜索代码 | "找下这个函数"、"搜一下" |
| `$D analyze --repos <name>` | 引擎深度分析 | "review 下代码"、"看看有什么问题" |

### 写入操作（需要 workspace lock）

执行顺序：`workspace-check` → 写入操作 → `release`

| 命令 | 说明 | 典型场景 |
|------|------|---------|
| `$D workspace-check --repos <name>` | 获取工作区锁 | 所有写操作前必须 |
| `$D develop ...` | 引擎写代码 | "修一下"、"改一下"、"加个功能" |
| `$D test ...` | 工作区内跑测试 | 开发后验证 |
| `$D submit-pr ...` | 提交 PR | "提交 pr"、"发个 PR" |
| `$D release ...` | 释放工作区锁 | 写操作结束后必须 |

### 复杂度判断：直接执行 vs 启动 Workflow

根据用户意图和上下文判断复杂度，选择执行方式：

**直接组合操作**（多数场景）：
- 单步操作或明确指令 → 直接组合上面的命令执行
- 例："提交 pr" → `workspace-check` + `submit-pr` + `release`
- 例："跑下测试" → `quick-test --repos`
- 例："看看有什么问题" → `analyze --repos`

**启动 workflow**（复杂任务）：
- 需要 research + plan + implement + verify 闭环
- 预计超过 20 次工具调用
- 涉及多文件修改且需要测试验证
- 例："修一下登录 500 错误" → 加载 SOP 后按流程执行
- 例："加个用户认证模块" → 加载 SOP 后按流程执行

### 参考文档（复杂任务时加载）

仅在启动 workflow / 复杂任务时加载对应 SOP，简单操作无需加载。

| 文档 | 用途 | 加载命令 |
|------|------|---------|
| sop-quick-queries.md | 只读命令参考 | `_load_skill_resource("coding-master", "references/sop-quick-queries.md")` |
| sop-deep-review.md | 深度 review 流程 | `_load_skill_resource("coding-master", "references/sop-deep-review.md")` |
| sop-bugfix-workflow.md | bugfix 完整流程 | `_load_skill_resource("coding-master", "references/sop-bugfix-workflow.md")` |
| sop-feature-dev.md | feature 开发流程 | `_load_skill_resource("coding-master", "references/sop-feature-dev.md")` |

## Common Rules

- **Review uses `--repos`**: Review/analysis is read-only — use `analyze --repos <name>` directly. No `workspace-check` or `release` needed. Only bugfix and feature-dev flows require workspace lock.
- **Engine commands need long timeout**: `analyze` and `develop` take 2-5 minutes. Always use `_bash(cmd="...", timeout=600)` for engine commands. If timeout returns a `command_id`, continue waiting with `_bash(command_id="...", timeout=300)` — do NOT cancel.
- **Engine fallback**: If `ENGINE_ERROR`, retry with the other engine (`codex`↔`claude`). If both fail, do it yourself, but `test`, `submit-pr`, `release` **must** go through `$D`.
- **Error handling**: Always check `error_code` + `hint`. `PATH_NOT_FOUND` → run `config-list` for correct names. `WORKSPACE_LOCKED` → use `--repos` for read-only fallback.
- **Safety**: Never push to main/master. Never force push. Never auto-merge PRs. Always release workspace when done.
- **User confirmation**: WAIT for user at workspace-check, plan confirmation, test results, and PR submission.
