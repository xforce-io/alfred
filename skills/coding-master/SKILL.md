---
name: coding-master
description: "Code review, analysis, development, and ops for registered repos — review uncommitted changes, check git status, search code, run tests, fix bugs, develop features, and submit PRs"
version: "0.1.0"
tags: [coding, review, development, bugfix, pr, automation]
---

# Coding Master Skill

Receive coding tasks through conversation, probe runtime environments, analyze code, develop fixes, run tests, and submit pull requests — all with human-in-the-loop confirmation.

## When to Use

- User asks to review code, check uncommitted changes, or analyze diffs (e.g., "review alfred 项目的修改") → **Quick Query** (`quick-status`)
- User asks about workspace/test status or wants to search code → **Quick Query** (no lock)
- User reports a bug (e.g., "heartbeat 定时任务没触发") → **Full Workflow**
- User requests a feature (e.g., "加个 workspace list 命令") → **Full Workflow**
- User asks to fix, analyze, or modify code in a registered workspace → **Full Workflow**
- User wants to manage coding-master configuration (add/remove workspace/env) → **Config commands**

## Configuration Management

Before using the coding workflow, workspaces and environments must be registered.

### List all config

```bash
_bash("python $SKILL_DIR/scripts/dispatch.py config-list")
```

### Add repo, workspace, or env

```bash
_bash("python $SKILL_DIR/scripts/dispatch.py config-add repo dolphin git@github.com:user/dolphin.git")
_bash("python $SKILL_DIR/scripts/dispatch.py config-add workspace my-app ~/dev/my-app")
_bash("python $SKILL_DIR/scripts/dispatch.py config-add env my-app-prod deploy@server:/opt/my-app")
# Set optional fields: config-set <type> <name> <field> <value>
_bash("python $SKILL_DIR/scripts/dispatch.py config-set repo dolphin default_branch develop")
```

### Remove

```bash
_bash("python $SKILL_DIR/scripts/dispatch.py config-remove env old-env")
```

### Output format

All commands return JSON: `{"ok": true, "data": {...}}` or `{"ok": false, "error": "...", "error_code": "..."}`. Always check the `ok` field. Use `error_code` for conditional handling (e.g., `WORKSPACE_LOCKED` → suggest waiting, `GIT_DIRTY` → suggest commit/stash).

---

## Quick Queries (Lock-Free)

Read-only commands for observation and diagnostic tasks. No workspace lock required — can run even while another task holds the lock.

### When to Use Quick Queries

- "看下 alfred 测试情况" / "跑下测试" → `quick-test`
- "alfred 什么分支" / "有没有未提交的改动" → `quick-status`
- "找下 HeartbeatRunner 在哪用了" → `quick-find`

### quick-status — Workspace overview

```bash
_bash("python $SKILL_DIR/scripts/dispatch.py quick-status --workspace alfred")
```

**Parse output**: `data.git` (branch, dirty, remote_url, last_commit), `data.runtime` (type, version, package_manager), `data.project` (test_command, lint_command), `data.lock` (null if idle, or {task, phase, engine, expired} if active).

### Code Review — Reviewing uncommitted changes

**IMPORTANT**: Never run bare `git diff` — large diffs get truncated. Instead: (1) `quick-status` + `git diff --stat` for file list, (2) `git diff -- <file_path>` per file, (3) `_get_cached_result_detail(reference_id, scope='skill', limit=20000)` if still truncated. Summarize by priority.

### quick-test — Run tests (and optionally lint)

```bash
# Run all tests
_bash("python $SKILL_DIR/scripts/dispatch.py quick-test --workspace alfred")

# Run specific test path
_bash("python $SKILL_DIR/scripts/dispatch.py quick-test --workspace alfred --path tests/unit/")

# Include lint check
_bash("python $SKILL_DIR/scripts/dispatch.py quick-test --workspace alfred --lint")
```

**Parse output**: `data.test` (passed, total, passed_count, failed_count, output), `data.overall` ("passed" | "failed"). If `--lint` used, also `data.lint` (passed, output).

### quick-find — Search code

```bash
# Search for a pattern
_bash("python $SKILL_DIR/scripts/dispatch.py quick-find --workspace alfred --query 'HeartbeatRunner'")

# Filter by file type
_bash("python $SKILL_DIR/scripts/dispatch.py quick-find --workspace alfred --query 'def test_' --glob '*.py'")
```

**Parse output**: `data.matches` (list of "file:line:content"), `data.count`, `data.truncated` (true if >100 matches).

**Escalation**: If quick-test reveals failures and the user wants to fix them, transition to the full workflow by calling `workspace-check`.

### quick-env — Probe environment without workspace (lock-free)

```bash
_bash("python $SKILL_DIR/scripts/dispatch.py quick-env --env alfred-prod")
# With extra commands:
_bash("python $SKILL_DIR/scripts/dispatch.py quick-env --env alfred-prod --commands \"tail -50 /var/log/app.log\"")
```

**Parse output**: Same as `env-probe` — `data.modules` with process status, recent errors, log tail (ephemeral, no artifacts saved). To fix issues found, transition to the full workflow.

---

## Workflow — 8 Phases (Phase 7 optional)

> **Session context**: `workspace-check` creates a session file (`.coding-master/session.json`) that locks in the workspace path. All subsequent workflow commands (`analyze`, `develop`, `test`, `submit-pr`, etc.) **require** this session — calling them without running `workspace-check` first will return `error_code: NO_SESSION`.

### Phase 0: Workspace Confirmation

**When**: User mentions a coding task. There are two modes:

#### Mode A: Repo-based (recommended for isolation)

Use `--repos` to clone/update repos into a workspace slot. The workspace is auto-allocated if `--workspace` is omitted.

```bash
# Single repo (auto-allocate workspace)
_bash("python $SKILL_DIR/scripts/dispatch.py workspace-check --repos dolphin --task 'fix: heartbeat bug' --engine codex")

# Multiple repos (comma-separated, first is primary; optionally specify workspace)
_bash("python $SKILL_DIR/scripts/dispatch.py workspace-check --repos dolphin,shared-lib --workspace env0 --task 'cross-repo refactor' --engine claude")
```

**Parse output**: `data.snapshot` contains `repos` array (each with git/runtime/project info) and `primary_repo`.

#### Mode B: Direct workspace (legacy)

Use `--workspace` alone for an existing git repo registered as a workspace.

```bash
_bash("python $SKILL_DIR/scripts/dispatch.py workspace-check --workspace alfred --task 'fix: heartbeat bug' --engine codex")
```

**Parse output**: If `ok: true`, extract `data.snapshot` — it contains git status, runtime info, and project commands. If `ok: false`, report the error to user (e.g., "workspace has uncommitted changes").

**User interaction**: Present workspace summary (branch, runtime, test/lint commands) and **WAIT for user confirmation before proceeding.**

### Phase 1: Environment Probing

**When**: User reports a runtime issue (bug, error, crash). Skip for pure feature development.

**Identify env**: Use workspace name to find matching envs. If multiple envs match (e.g., alfred-local, alfred-prod), ask user which one based on context ("线上" → prod, "本地" → local).

```bash
_bash("python $SKILL_DIR/scripts/dispatch.py env-probe --workspace alfred --env alfred-prod")
```

**Parse output**: Extract `data.modules` — each has process status, recent errors, and log tail. Summarize for user.

**Directed probing**: If you need specific info:

```bash
_bash("python $SKILL_DIR/scripts/dispatch.py env-probe --workspace alfred --env alfred-prod --commands \"journalctl -u alfred --since '2 hours ago'\"")
```

### Phase 2: Problem Analysis

**When**: After Phase 0 (and optionally Phase 1) are confirmed.

```bash
_bash("python $SKILL_DIR/scripts/dispatch.py analyze --workspace alfred --task 'heartbeat 定时任务没触发' --engine codex")
```

**Parse output**: `data.summary` is the analysis report. Present location, root cause, proposed fix, and risk assessment to user.

**If engine requests more env info**: Run additional `env-probe --commands ...` and re-run `analyze` (max 2 iterations).

**Engine fallback**: If `analyze` returns `error_code: ENGINE_ERROR`, retry with the other engine (`--engine claude` or `--engine codex`). If both engines fail, you may perform the analysis yourself using your own capabilities, but all subsequent workflow steps (test, submit-pr, release) **must** still go through `dispatch.py`.

**WAIT for user confirmation of analysis and approach.**

### Phase 3: Plan Confirmation

**User decides**:
- "继续" / "修吧" → proceed to Phase 4 with recommended approach
- "用方案 2" → proceed with specified approach
- "再看看日志" → run more `env-probe`, loop back to Phase 2
- "取消" → run `dispatch.py release --workspace alfred`

### Phase 4: Coding Development

**When**: User confirms the fix plan.

```bash
_bash("python $SKILL_DIR/scripts/dispatch.py develop --workspace alfred --task 'fix timezone in heartbeat' --plan 'unify to timezone-aware datetime' --branch fix/heartbeat-tz --engine codex")
```

**Parse output**: `data.summary` describes what was changed, `data.files_changed` lists modified files.

**Engine fallback**: Same as Phase 2 — if `develop` returns `ENGINE_ERROR`, retry with the other engine. If both engines fail, you may write code yourself directly, but testing, PR submission, and release **must** go through `dispatch.py`.

**Auto-proceed to Phase 5** — do NOT wait for user confirmation here.

### Phase 5: Test Verification

**When**: Immediately after Phase 4 completes.

```bash
_bash("python $SKILL_DIR/scripts/dispatch.py test --workspace alfred")
```

**Parse output**: Check `data.overall`:

**If "passed"** — report test/lint results and changed files to user. Ask to submit PR.

**If "failed"** — check what failed:
- **Lint failure only**: Auto-fix via `develop --task 'fix lint errors'`, then re-run `test`. No user confirmation needed.
- **Test failure**: Report failures to user with options: (1) auto-fix, (2) manual fix, (3) abandon.

**Auto-fix limit**: Maximum **2 rounds** of develop → test. After 2 failures, must ask user.

**WAIT for user confirmation before Phase 6.**

### Phase 5.5: Self-Review

**When**: Tests pass, before submitting PR. Quick scan of `git diff` (file by file) for unnecessary changes, convention violations, obvious issues, and completeness gaps. Minor issues → auto-fix via `develop`; significant issues → report to user. Include findings in PR body.

### Phase 6: Submit PR

**When**: User confirms test results and wants to submit.

```bash
_bash("python $SKILL_DIR/scripts/dispatch.py submit-pr --workspace alfred --title 'fix: heartbeat timezone handling' --body '## Summary\n- Unified timezone-aware datetime in HeartbeatRunner\n\n## Test\n- All 42 tests passing\n- Ruff clean'")
```

**Parse output**: `data.pr_url` — share with user.

**After PR created**:

- **If task has an associated Env** (i.e., Phase 1 was used) → ask user: "需要部署验证吗？"
  - User says yes → proceed to Phase 7
  - User says no → release workspace
- **If no associated Env** (pure feature development) → release workspace immediately

```bash
_bash("python $SKILL_DIR/scripts/dispatch.py release --workspace alfred")
```

### Phase 7: Env Verification (Optional)

**When**: After Phase 6, user wants to verify fix in deployment env. WAIT for user to deploy, renew lease periodically (`renew-lease --workspace alfred`), then verify:

```bash
_bash("python $SKILL_DIR/scripts/dispatch.py env-verify --workspace alfred --env alfred-staging")
```

**Parse output**: `data.resolved` — if `true`, report and release; if `false`, offer (1) auto-fix (loop to Phase 4), (2) release, (3) rollback (`release --cleanup`). WAIT for user to confirm release.

### Release (mandatory)

**Every task must end with `release`** — whether the task succeeds, is cancelled, or fails at any phase. Unreleased workspaces block future tasks.

```bash
_bash("python $SKILL_DIR/scripts/dispatch.py release --workspace alfred")
```

---

## Feature Management (Task Splitting)

When Phase 2 analysis reveals a task is too large for a single develop cycle, split it into features:

### Commands

```bash
# Create plan (features is JSON array with title, task, optional depends_on)
_bash("python $SKILL_DIR/scripts/dispatch.py feature-plan --workspace alfred --task 'refactor auth system' --features '[{\"title\":\"extract auth middleware\",\"task\":\"move auth logic to middleware\"},{\"title\":\"add JWT\",\"task\":\"integrate PyJWT\",\"depends_on\":[0]}]'")

# Loop: feature-next → develop → test → submit-pr → feature-done
_bash("python $SKILL_DIR/scripts/dispatch.py feature-next --workspace alfred")
_bash("python $SKILL_DIR/scripts/dispatch.py feature-done --workspace alfred --index 0 --branch feat/auth-middleware --pr '#15'")

# Adjust: skip/update features, check progress
_bash("python $SKILL_DIR/scripts/dispatch.py feature-update --workspace alfred --index 1 --status skipped")
_bash("python $SKILL_DIR/scripts/dispatch.py feature-list --workspace alfred")
```

Ask user "Continue with next feature?" before each. When `feature-next` returns `status: all_complete`, call `release`.

---

## Lease Management

Default lease is 2 hours. During long pauses, renew proactively: `_bash("python $SKILL_DIR/scripts/dispatch.py renew-lease --workspace alfred")`

---

## Error Handling

| error_code | Agent Action |
|------------|-------------|
| `NO_SESSION` | Run `workspace-check` first to start a session |
| `PATH_NOT_FOUND` | Ask user to add workspace/env: `config-add ...` |
| `WORKSPACE_LOCKED` | Report current task and phase, suggest waiting |
| `GIT_DIRTY` | Ask user to commit or stash first |
| `LOCK_NOT_FOUND` | Remind to run `workspace-check` first |
| `LEASE_EXPIRED` | Lock was cleaned; re-run `workspace-check` |
| `SSH_UNREACHABLE` | Ask if user wants to skip env probing |
| `ENGINE_TIMEOUT` | Release workspace, suggest retry with simpler task |
| `ENGINE_ERROR` | Release workspace, report error details |
| `COMMAND_DENIED` | Tell user the env command was blocked by security policy |
| `TEST_FAILED` | After 2 auto-fix rounds: present options (manual fix / abandon / keep branch) |

### Cancellation

Use `release --workspace alfred` at any phase. Add `--cleanup` if code changes exist (Phase 4-5) to rollback branch. If PR already created (Phase 6+), tell user to close PR manually first.

---

## Safety Rules

1. **Never push to main/master** — always work on feature/fix branches
2. **Never force push** — all pushes are regular pushes
3. **Never auto-merge PRs** — PRs require human review
4. **Env probing is read-only** — no writes, restarts, or deployments to runtime environments
5. **Confirm before proceeding** — wait at Phase 0, Phase 2, Phase 5, and Phase 6
6. **Respect lock** — if workspace is busy, do not force acquire
7. **Auto-fix limit** — max 2 rounds of test fix, then escalate to user
8. **dispatch.py is the sole workflow entry point** — all workflow operations (workspace-check, test, submit-pr, release, etc.) must go through `dispatch.py`. Do not use `_bash`/`_write_file` to directly modify workspace files or perform git operations, except: when engines are unavailable, you may write code directly for analysis/development, but testing, PR submission, and release must always go through `dispatch.py`
9. **Always release** — every task must end with `dispatch.py release`, whether successful, cancelled, or failed. Forgetting to release blocks the workspace for future tasks

---

