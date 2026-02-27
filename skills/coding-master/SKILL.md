---
name: coding-master
description: "Code review, analysis, development, and ops for registered repos — review uncommitted changes, check git status, search code, run tests, fix bugs, develop features, and submit PRs"
version: "0.1.0"
tags: [coding, review, development, bugfix, pr, automation]
---

# Coding Master Skill

All commands use: `$D = python $SKILL_DIR/scripts/dispatch.py`

All commands return JSON: `{"ok": true, "data": {...}}` or `{"ok": false, "error": "...", "error_code": "..."}`. Always check `ok`. On error, check `error_code` and `hint` field for actionable recovery.

## Intent Routing (mandatory)

Identify user intent, then load the matching SOP **before** executing any command. **Do NOT skip SOP loading.**

| User Intent | SOP | Load Command |
|-------------|-----|--------------|
| Search code, view git status/diff, run tests, quick check | Quick Queries | `_load_skill_resource("coding-master", "references/sop-quick-queries.md")` |
| Review / 审查 / 分析项目 / "看看有什么问题" | Deep Review | `_load_skill_resource("coding-master", "references/sop-deep-review.md")` |
| Fix bug / 修复 / 排查报错 / "不工作了" | Bugfix Workflow | `_load_skill_resource("coding-master", "references/sop-bugfix-workflow.md")` |
| New feature / 开发 / 重构 / add functionality | Feature Dev | `_load_skill_resource("coding-master", "references/sop-feature-dev.md")` |

**Intent keywords**:
- "review" / "审查" / "分析项目" / "code review" / "看看有什么问题" / "改进项" → **Deep Review**
- "fix" / "修复" / "bug" / "报错" / "不工作" / "出错" → **Bugfix Workflow**
- "添加" / "实现" / "开发" / "重构" / "新功能" / "feature" → **Feature Dev**
- "搜索" / "查找" / "status" / "diff" / "测试" / "test" → **Quick Queries**

## Common Rules

- **Engine fallback**: If `ENGINE_ERROR`, retry with the other engine (`codex`↔`claude`). If both fail, do it yourself, but `test`, `submit-pr`, `release` **must** go through `$D`.
- **Error handling**: Always check `error_code` + `hint`. `PATH_NOT_FOUND` → run `config-list` for correct names. `WORKSPACE_LOCKED` → use `--repos` for read-only fallback.
- **Safety**: Never push to main/master. Never force push. Never auto-merge PRs. Always release workspace when done.
- **User confirmation**: WAIT for user at workspace-check, plan confirmation, test results, and PR submission.
