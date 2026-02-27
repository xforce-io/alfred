---
name: coding-master
description: "Code review, analysis, development, and ops for registered repos — review uncommitted changes, check git status, search code, run tests, fix bugs, develop features, and submit PRs"
version: "0.1.0"
tags: [coding, review, development, bugfix, pr, automation]
---

# Coding Master Skill

All commands use: `$D = python $SKILL_DIR/scripts/dispatch.py`

All commands return JSON: `{"ok": true, "data": {...}}` or `{"ok": false, "error": "...", "error_code": "..."}`. Always check `ok`. On error, check `error_code` and `hint` field for actionable recovery.

**Engine fallback** (applies to `analyze` and `develop`): If `ENGINE_ERROR`, retry with the other engine (`claude`↔`codex`). If both fail, you may do analysis/development yourself, but `test`, `submit-pr`, `release` **must** go through `$D`.

## When to Use

- "搜索/分析 X 的代码" → `quick-find --repos X` (lock-free, preferred)
- Review code / check diffs / git status → `quick-status` (lock-free)
- Run tests / lint → `quick-test` (lock-free)
- Bug fix / feature / code modification → **Full Workflow** (Phase 0–7)
- Manage config → `config-list`, `config-add`, `config-set`, `config-remove`

## Config

```bash
_bash("$D config-list")
_bash("$D config-add repo dolphin git@github.com:user/dolphin.git")
_bash("$D config-add workspace my-app ~/dev/my-app")
_bash("$D config-add env my-app-prod deploy@server:/opt/my-app")
_bash("$D config-set repo dolphin default_branch develop")
_bash("$D config-remove env old-env")
```

---

## Quick Queries (Lock-Free)

Read-only, no workspace lock required. **`--workspace`** = workspace slot name (env0/env1/env2 or registered name). **`--repos`** = registered repo name — searches source path directly, no lock needed.

### quick-status

```bash
_bash("$D quick-status --workspace alfred")
```
Output: `data.git` (branch, dirty, last_commit), `data.runtime`, `data.project` (test/lint commands), `data.lock` (null or {task, phase, expired}).

**Code Review**: Never bare `git diff`. Use: (1) `quick-status` + `git diff --stat`, (2) `git diff -- <file>` per file, (3) `_get_cached_result_detail(reference_id, scope='skill', limit=20000)` if truncated.

### quick-test

```bash
_bash("$D quick-test --workspace alfred")                        # all tests
_bash("$D quick-test --workspace alfred --path tests/unit/ --lint")  # specific + lint
```
Output: `data.test` (passed, total, output), `data.overall` ("passed"|"failed"), `data.lint` (if `--lint`).

### quick-find

```bash
_bash("$D quick-find --repos alfred --query 'HeartbeatRunner'")                    # single repo
_bash("$D quick-find --repos alfred,dolphin --query 'HeartbeatRunner' --glob '*.py'")  # multi-repo
_bash("$D quick-find --workspace env0 --query 'def test_' --glob '*.py'")          # workspace
```
Output (`--repos`): `data.repos` (dict by repo name → match lines), `data.count`, `data.truncated`.
Output (`--workspace`): `data.matches` (list), `data.count`, `data.truncated`.

### quick-env

```bash
_bash("$D quick-env --env alfred-prod")
_bash("$D quick-env --env alfred-prod --commands \"tail -50 /var/log/app.log\"")
```
Output: `data.modules` (process status, errors, log tail). To fix issues → transition to Full Workflow.

---

## Full Workflow (Phase 0–7)

> `workspace-check` creates `.coding-master/session.json`. All subsequent commands require this session (`NO_SESSION` error otherwise).

### Phase 0: Workspace Check

```bash
# Repo-based (recommended) — auto-allocates workspace slot
_bash("$D workspace-check --repos dolphin --task 'fix: heartbeat bug' --engine codex")
# Multi-repo, specific slot, auto-clean dirty state
_bash("$D workspace-check --repos dolphin,shared-lib --workspace env0 --auto-clean --task 'cross-repo refactor' --engine claude")
# Direct workspace (legacy)
_bash("$D workspace-check --workspace alfred --task 'fix: heartbeat bug' --engine codex")
```
Output: `data.snapshot` (repos array with git/runtime/project info, `base_commit`, `primary_repo`).

Repo mode uses `git clone` (not file copy). Reused slots sync to `origin/<branch>`. Dirty slots → `WORKSPACE_GIT_DIRTY`, add `--auto-clean` to reset.

**WAIT for user confirmation** before proceeding.

### Phase 1: Env Probing (skip for pure feature dev)

```bash
_bash("$D env-probe --workspace alfred --env alfred-prod")
_bash("$D env-probe --workspace alfred --env alfred-prod --commands \"journalctl -u alfred --since '2 hours ago'\"")
```
Output: `data.modules` (process status, errors, logs).

### Phase 2: Analysis

```bash
_bash("$D analyze --workspace alfred --task 'heartbeat 定时任务没触发' --engine codex")
```
Output: `data.summary`, `data.complexity` (trivial|standard|complex), `data.feature_plan_created`, `data.feature_count`.

### Phase 3: Plan Confirmation

- **trivial** → auto-proceed to Phase 4
- **standard** → **WAIT**: "继续"→Phase 4, "用方案2"→Phase 4 with alt, "再看看日志"→back to Phase 2, "取消"→release
- **complex** → Feature Plan auto-generated; enter **Feature Loop** (see below)

### Phase 4: Development

```bash
_bash("$D develop --workspace alfred --task 'fix timezone' --plan 'unify to tz-aware datetime' --branch fix/heartbeat-tz --engine codex")
```
Output: `data.summary`, `data.files_changed`. Auto-proceed to Phase 5.

### Phase 5: Test

```bash
_bash("$D test --workspace alfred")
```
Passed → report + ask to submit PR. Lint-only fail → auto-fix. Test fail → report to user. Max **2 auto-fix rounds**, then escalate. **WAIT before Phase 6.**

### Phase 5.5: Self-Review

Scan `git diff` file-by-file for unnecessary changes, convention violations. Minor → auto-fix; significant → report. Include in PR body.

### Phase 6: Submit PR

```bash
_bash("$D submit-pr --workspace alfred --title 'fix: heartbeat timezone' --body '...'")
```
Output: `data.pr_url`. If env was probed → ask "需要部署验证吗？"; otherwise release immediately.

### Phase 7: Env Verification (optional)

```bash
_bash("$D env-verify --workspace alfred --env alfred-staging")
```
`data.resolved` → true: release; false: offer auto-fix / release / rollback (`release --cleanup`).

### Release (mandatory — every task must end here)

```bash
_bash("$D release --workspace alfred")              # normal
_bash("$D release --workspace alfred --cleanup")     # rollback branch
```
Lease: 2h default. Renew with `$D renew-lease --workspace alfred`.

---

## Feature Management

For `complexity: complex` tasks. Feature Plan auto-generated in Phase 2, or create manually.

```bash
_bash("$D feature-plan --workspace alfred --task 'refactor auth' --features '[{\"title\":\"extract middleware\",\"task\":\"...\",\"acceptance_criteria\":[{\"type\":\"test\",\"target\":\"pytest tests/auth/\",\"description\":\"pass\"}]},{\"title\":\"add JWT\",\"task\":\"...\",\"depends_on\":[0]}]'")
_bash("$D feature-list --workspace alfred")
_bash("$D feature-criteria --workspace alfred --index 0 --action view")
_bash("$D feature-criteria --workspace alfred --index 0 --action append --criteria '{\"type\":\"test\",\"target\":\"pytest tests/auth/test_jwt.py\",\"description\":\"pass\"}'")
_bash("$D feature-verify --workspace alfred --index 0 --engine codex")
_bash("$D feature-done --workspace alfred --index 0 --branch feat/auth --pr '#15'")
_bash("$D feature-update --workspace alfred --index 1 --status skipped")
```

Criteria types: `test` (subprocess, blocks done), `assert` (engine verify, blocks done), `manual` (reminder only).

**Feature Loop**: `feature-next` → `develop` → `test` → `feature-verify` → `feature-done` (max 3 attempts). Ask user before each feature. When `all_complete` → `submit-pr` → `release`.

---

## Error Handling

| error_code | Action |
|------------|--------|
| `NO_SESSION` / `LOCK_NOT_FOUND` / `LEASE_EXPIRED` | Run `workspace-check` to start/restart session |
| `PATH_NOT_FOUND` / `INVALID_ARGS` | Check `hint` field; run `config-list` for correct names; try `--repos <name>` |
| `WORKSPACE_LOCKED` | Use `quick-find --repos` for read-only search; or ask user to release workspace |
| `GIT_DIRTY` | Ask user to commit or stash |
| `WORKSPACE_GIT_DIRTY` | Add `--auto-clean` to `workspace-check`, or `release --cleanup` |
| `SSH_UNREACHABLE` | Ask user whether to skip env probing |
| `ENGINE_TIMEOUT` / `ENGINE_ERROR` | Release workspace; retry with other engine or simpler task |
| `COMMAND_DENIED` | Env command blocked by security policy — inform user |
| `TEST_FAILED` | Max 2 auto-fix rounds, then ask user (manual fix / abandon / keep branch) |

**Cancellation**: `release --workspace <name>` at any phase. Add `--cleanup` to rollback branch (Phase 4-5). If PR exists (Phase 6+), tell user to close PR first.

---

## Safety Rules

1. Never push to main/master — always feature/fix branches
2. Never force push; never auto-merge PRs
3. Env probing is read-only — no writes, restarts, or deployments
4. **WAIT for user** at Phase 0, Phase 3 (standard/complex), Phase 5, Phase 6
5. Respect locks — never force acquire; `WORKSPACE_LOCKED` → use `--repos` for read-only fallback
6. Max 2 auto-fix rounds, then escalate
7. `dispatch.py` is the sole workflow entry point — no direct `_bash`/`_write_file` on workspace files (except code authoring when engines unavailable)
8. Always release — forgetting blocks future tasks
