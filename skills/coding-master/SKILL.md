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

根据用户意图自主选择操作并组合执行，不强制走预定义流程。参数不确定时先跑 `$D <command> --help`。

### 只读操作（无需 workspace lock）

| 命令 | 说明 |
|------|------|
| `$D quick-status --repos <name>` | git 状态/diff |
| `$D quick-test --repos <name> [--path PATH] [--lint]` | 跑测试 |
| `$D quick-find --repos <name> --query <pattern> [--glob GLOB]` | 搜索代码 |
| `$D analyze --repos <name> --task "<desc>" [--engine ENGINE]` | 引擎深度分析（timeout=600） |

### 写入操作（需要 workspace lock）

流程：`workspace-check` → 写入操作 → `release`。workspace-check 返回的 `data.snapshot.workspace.name` 即 `<ws>`。

| 命令 | 说明 |
|------|------|
| `$D workspace-check --repos <name> --task "<desc>" [--engine ENGINE]` | 获取锁，返回 workspace |
| `$D develop --workspace <ws> --task "<desc>" [--engine ENGINE]` | 引擎写代码（timeout=600） |
| `$D test --workspace <ws>` | 工作区内跑测试 |
| `$D submit-pr --workspace <ws> --title "<title>" [--body "<body>"]` | 提交 PR |
| `$D release --workspace <ws>` | 释放锁（必须） |

### 复杂度判断

**直接组合操作**（多数场景）：单步或明确指令，直接组合上面的命令
- "提交 pr" → workspace-check + submit-pr + release
- "跑下测试" → quick-test --repos
- "看看有什么问题" → analyze --repos

**启动 workflow**（复杂任务）：需要 research→plan→implement→verify 闭环、预计超 20 次工具调用、多文件修改+测试验证 → 加载 SOP 后按流程执行

### 参考文档（复杂任务时加载）

| 文档 | 加载命令 |
|------|---------|
| sop-quick-queries.md | `_load_skill_resource("coding-master", "references/sop-quick-queries.md")` |
| sop-deep-review.md | `_load_skill_resource("coding-master", "references/sop-deep-review.md")` |
| sop-bugfix-workflow.md | `_load_skill_resource("coding-master", "references/sop-bugfix-workflow.md")` |
| sop-feature-dev.md | `_load_skill_resource("coding-master", "references/sop-feature-dev.md")` |

## Common Rules

- **Review uses `--repos`**: Review/analysis is read-only — use `analyze --repos <name>` directly. No `workspace-check` or `release` needed. Only bugfix and feature-dev flows require workspace lock.
- **Engine commands need long timeout**: `analyze` and `develop` take 2-5 minutes. Always use `_bash(cmd="...", timeout=600)` for engine commands. If timeout returns a `command_id`, continue waiting with `_bash(command_id="...", timeout=300)` — do NOT cancel.
- **Engine fallback**: If `ENGINE_ERROR`, retry with the other engine (`codex`↔`claude`). If both fail, do it yourself, but `test`, `submit-pr`, `release` **must** go through `$D`.
- **Error handling**: Always check `error_code` + `hint`. `PATH_NOT_FOUND` → run `config-list` for correct names. `WORKSPACE_LOCKED` → use `--repos` for read-only fallback.
- **Safety**: Never push to main/master. Never force push. Never auto-merge PRs. Always release workspace when done.
- **User confirmation**: WAIT for user at workspace-check, plan confirmation, test results, and PR submission.
