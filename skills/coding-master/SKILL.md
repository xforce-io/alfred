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
# Register a repo (URL as value; repos get cloned into workspaces on demand)
_bash("python $SKILL_DIR/scripts/dispatch.py config-add repo dolphin git@github.com:user/dolphin.git")

# Set optional fields on repo (e.g., default_branch)
_bash("python $SKILL_DIR/scripts/dispatch.py config-set repo dolphin default_branch develop")

# Register a workspace slot (empty directory — repos are cloned into it)
_bash("python $SKILL_DIR/scripts/dispatch.py config-add workspace env0 ~/.alfred/workspaces/env0")

# Register a direct workspace (existing git repo)
_bash("python $SKILL_DIR/scripts/dispatch.py config-add workspace my-app ~/dev/my-app")

# Register an env
_bash("python $SKILL_DIR/scripts/dispatch.py config-add env my-app-prod deploy@server:/opt/my-app")
```

### Set extended fields (auto-upgrades minimal → extended)

```bash
_bash("python $SKILL_DIR/scripts/dispatch.py config-set workspace alfred test_command 'pytest -x'")
_bash("python $SKILL_DIR/scripts/dispatch.py config-set env alfred-prod log /opt/alfred/logs/daemon.log")
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

**IMPORTANT**: Never run bare `git diff` on the entire repo — large diffs (tens of KB) will be truncated by the output retention strategy and you'll miss most changes.

**Correct pattern**:

1. Get overview with `quick-status`, then run `git diff --stat` for file list:
```bash
_bash("cd <workspace_path> && git diff --stat")
```

2. Review each file individually:
```bash
_bash("cd <workspace_path> && git diff -- <file_path>")
```

3. For large single-file diffs that still get truncated, use `_get_cached_result_detail(reference_id, scope='skill', limit=20000)` to retrieve the full content.

4. Summarize findings by priority (high / medium / low).

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

Use when the user wants to check a runtime environment (logs, process status) without starting a coding workflow. No workspace lock or session required.

```bash
# Basic probe
_bash("python $SKILL_DIR/scripts/dispatch.py quick-env --env alfred-prod")

# With extra commands
_bash("python $SKILL_DIR/scripts/dispatch.py quick-env --env alfred-prod --commands \"tail -50 /var/log/app.log\" \"journalctl -u alfred --since '1 hour ago'\"")
```

**Parse output**: Same as `env-probe` — `data.modules` with process status, recent errors, log tail. The difference is that no artifacts are saved (results are ephemeral).

**Escalation**: If the user wants to fix an issue found in the env, transition to the full workflow (`workspace-check` → `env-probe` → `analyze` → ...).

---

## Workflow — 8 Phases (Phase 7 optional)

> **Session context**: `workspace-check` creates a session file (`.coding-master/session.json`) that locks in the workspace path. All subsequent workflow commands (`analyze`, `develop`, `test`, `submit-pr`, etc.) **require** this session — calling them without running `workspace-check` first will return `error_code: NO_SESSION`.

### Phase 0: Workspace Confirmation

**When**: User mentions a coding task. There are two modes:

#### Mode A: Repo-based (recommended for isolation)

Use `--repos` to clone/update repos into a workspace slot. The workspace is auto-allocated if `--workspace` is omitted.

```bash
# Auto-allocate workspace, clone/update dolphin repo
_bash("python $SKILL_DIR/scripts/dispatch.py workspace-check --repos dolphin --task 'fix: heartbeat bug' --engine codex")

# Or specify a workspace explicitly
_bash("python $SKILL_DIR/scripts/dispatch.py workspace-check --repos dolphin --workspace env0 --task 'fix: heartbeat bug' --engine codex")

# Multiple repos (comma-separated) — first is primary
_bash("python $SKILL_DIR/scripts/dispatch.py workspace-check --repos dolphin,shared-lib --task 'cross-repo refactor' --engine claude")
```

**Parse output**: `data.snapshot` contains `repos` array (each with git/runtime/project info) and `primary_repo`.

#### Mode B: Direct workspace (legacy)

Use `--workspace` alone for an existing git repo registered as a workspace.

```bash
_bash("python $SKILL_DIR/scripts/dispatch.py workspace-check --workspace alfred --task 'fix: heartbeat bug' --engine codex")
```

**Parse output**: If `ok: true`, extract `data.snapshot` — it contains git status, runtime info, and project commands. If `ok: false`, report the error to user (e.g., "workspace has uncommitted changes").

**User interaction**: Present workspace summary and ask to proceed:

```
📁 Workspace: alfred (~/dev/github/alfred)
   Branch: main, clean, Python 3.12.4
   Test: pytest | Lint: ruff check .
   Proceed with analysis?
```

**WAIT for user confirmation before proceeding.**

### Phase 1: Environment Probing

**When**: User reports a runtime issue (bug, error, crash). Skip for pure feature development.

**Identify env**: Use workspace name to find matching envs. If multiple envs match (e.g., alfred-local, alfred-prod), ask user which one based on context ("线上" → prod, "本地" → local).

```bash
_bash("python $SKILL_DIR/scripts/dispatch.py env-probe --workspace alfred --env alfred-prod")
```

**Parse output**: Extract `data.modules` — each has process status, recent errors, and log tail. Summarize for user:

```
🖥️ Env: alfred-prod (ssh → prod-server)
   daemon: running (pid 5678)
   Recent errors:
     10:15 ERROR heartbeat: Task 'daily-report' skipped
     09:45 ERROR heartbeat: Task 'paper-digest' skipped
```

**Directed probing**: If you need specific info:

```bash
_bash("python $SKILL_DIR/scripts/dispatch.py env-probe --workspace alfred --env alfred-prod --commands \"journalctl -u alfred --since '2 hours ago'\"")
```

### Pre-Analysis: Project Context Discovery

**When**: After Phase 0 (and optionally Phase 1), before calling `analyze`.

Scan the workspace for project documentation to understand architectural constraints and coding conventions. This context significantly improves analysis and development quality.

1. **Check for docs** (use Glob/Read — NOT dispatch.py):
   - Architecture: `ARCHITECTURE.md`, `DESIGN.md`, `docs/architecture*`
   - Agent instructions: `AGENTS.md`, `CLAUDE.md`, `.cursor/rules/`, `.github/copilot-instructions.md`
   - Contributing guide: `CONTRIBUTING.md`, `docs/contributing*`
   - API / module docs: `docs/**/*.md`

2. **Extract key constraints** (if found):
   - Dependency layers / module boundaries (e.g., "types → config → service → runtime")
   - Coding conventions (naming, patterns, forbidden practices)
   - Project-specific rules (e.g., "all dates must be UTC", "no direct DB queries outside repo layer")

3. **Carry forward**: Include discovered constraints in the `--task` for `analyze` and in the `--plan` for `develop`. Example:

   ```
   --task 'fix heartbeat timezone bug. Project constraints: all datetime must be timezone-aware (see ARCHITECTURE.md); no direct imports from runtime layer in core.'
   ```

If no project docs are found, proceed normally — this step is best-effort.

### Phase 2: Problem Analysis

**When**: After Phase 0 (and optionally Phase 1) are confirmed.

```bash
_bash("python $SKILL_DIR/scripts/dispatch.py analyze --workspace alfred --task 'heartbeat 定时任务没触发' --engine codex")
```

**Parse output**: `data.summary` is the analysis report. Present to user:

```
📍 heartbeat.py:142 HeartbeatRunner._should_run_task()
🔍 naive datetime vs UTC comparison causes timezone offset
💡 Proposed fix: unify to timezone-aware datetime
   Risk: low | Impact: heartbeat scheduling only
```

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

**Include constraints in `--plan`**: If Project Context Discovery found architectural rules, append them to the plan so the engine respects them:

```
--plan 'Unify to timezone-aware datetime. Constraints: all datetime must use timezone.utc; no naive datetime in core/ or runtime/ layers.'
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

**If "passed"** — report to user:

```
✅ Tests passed (42 passed, ruff clean)
📝 Changes: heartbeat.py (+3, -2)
Submit PR?
```

**If "failed"** — check what failed:

- **Lint failure only**: Auto-fix by calling `develop` with lint fix task, then re-run `test`. No user confirmation needed.

  ```bash
  _bash("python $SKILL_DIR/scripts/dispatch.py develop --workspace alfred --task 'fix lint errors' --engine codex")
  _bash("python $SKILL_DIR/scripts/dispatch.py test --workspace alfred")
  ```

- **Test failure**: Report to user with options:

  ```
  ❌ Tests failed (3 failed / 42 total):
     • test_heartbeat_timezone: AssertionError ...
     • test_heartbeat_skip: ...

  Options:
  1. Let me fix it (auto-fix round)
  2. I'll look manually (pause, keep branch)
  3. Abandon changes (rollback)
  ```

**Auto-fix limit**: Maximum **2 rounds** of develop → test. After 2 failures, must ask user.

**WAIT for user confirmation before Phase 6.**

### Phase 5.5: Self-Review

**When**: Tests pass and before submitting PR. This step catches issues that tests don't cover.

1. Run `git diff` in the workspace (file by file for large diffs) and review the changes for:
   - **Unnecessary changes**: files modified that aren't related to the task
   - **Convention violations**: naming, patterns, or architectural boundaries discovered in Project Context Discovery
   - **Obvious issues**: hardcoded values, missing error handling at system boundaries, leftover debug code
   - **Completeness**: does the diff fully address the task, or are there gaps

2. If issues are found:
   - Minor (formatting, naming): fix directly via `develop` with a targeted task, then re-run `test`
   - Significant (wrong approach, missing pieces): report to user before proceeding

3. Include review findings in the PR body (Phase 6) so the human reviewer has context.

This step is lightweight — a quick scan, not a full code review. If the diff is small and clean, move on.

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

**When**: After Phase 6, when the task involved an Env and user wants to verify the fix in a deployment environment.

**Flow**:

1. Tell user the workspace is held while waiting for deployment
2. **WAIT** for user to deploy (via CI/CD or manually) and confirm deployment is done
3. During the wait, renew lease periodically:
```bash
_bash("python $SKILL_DIR/scripts/dispatch.py renew-lease --workspace alfred")
```
4. Once user confirms deployment, run verification:
```bash
_bash("python $SKILL_DIR/scripts/dispatch.py env-verify --workspace alfred --env alfred-staging")
```

**Parse output**: Check `data.resolved`:

**If `true`** — report to user:
```
✅ Env verification passed:
   Resolved: 2 heartbeat-related error(s)
   No new errors detected
   Task complete — release workspace?
```

**If `false`** — report comparison and offer options:
```
❌ Env verification failed:
   Still present: ERROR heartbeat: Task 'daily-report' skipped
   New errors: ERROR heartbeat: Task 'daily-report' timeout

   Options:
   1. Let me fix it (loop back to Phase 4 with env verify report as context)
   2. I'll handle it manually (release workspace)
   3. Rollback changes (release --cleanup)
```

**If user chooses "fix it"**: Loop back to Phase 4 → 5 → 6 → 7. The env verification report is available as context for the next develop cycle.

**WAIT for user to confirm release after verification.**

```bash
_bash("python $SKILL_DIR/scripts/dispatch.py release --workspace alfred")
```

### Release (mandatory)

**Every task must end with `release`** — whether the task succeeds, is cancelled, or fails at any phase. Unreleased workspaces block future tasks.

```bash
_bash("python $SKILL_DIR/scripts/dispatch.py release --workspace alfred")
```

---

## Feature Management (Task Splitting)

When Phase 2 analysis reveals a task is too large for a single develop cycle, split it into features:

### Create a feature plan

```bash
_bash("python $SKILL_DIR/scripts/dispatch.py feature-plan --workspace alfred --task 'refactor auth system' --features '[{\"title\":\"extract auth middleware\",\"task\":\"move auth logic to middleware\"},{\"title\":\"add JWT\",\"task\":\"integrate PyJWT\",\"depends_on\":[0]}]'")
```

### Feature loop

After creating the plan, loop through features:

1. Get next feature:
```bash
_bash("python $SKILL_DIR/scripts/dispatch.py feature-next --workspace alfred")
```

2. For each feature, run: `develop` → `test` → `submit-pr` → then mark done:
```bash
_bash("python $SKILL_DIR/scripts/dispatch.py feature-done --workspace alfred --index 0 --branch feat/auth-middleware --pr '#15'")
```

3. Ask user "Continue with next feature?" before proceeding.

4. When `feature-next` returns `status: all_complete`, call `release`.

### Adjust plan mid-flight

```bash
# Skip a feature
_bash("python $SKILL_DIR/scripts/dispatch.py feature-update --workspace alfred --index 1 --status skipped")

# Check overall progress
_bash("python $SKILL_DIR/scripts/dispatch.py feature-list --workspace alfred")
```

---

## Lease Management

During long user-interaction pauses (Phase 3 confirmation, feature intervals), renew the workspace lease to prevent expiration:

```bash
_bash("python $SKILL_DIR/scripts/dispatch.py renew-lease --workspace alfred")
```

The default lease is 2 hours. Renew proactively if the user hasn't responded for a while.

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

### Cancellation at any phase

- **Phase 0-2** (no code changes): `dispatch.py release --workspace alfred`
- **Phase 4-5** (has code changes): `dispatch.py release --workspace alfred --cleanup` (rolls back branch + remote if pushed)
- **Phase 6** (PR created): Tell user to close PR manually, then `dispatch.py release --workspace alfred`
- **Phase 7** (waiting for deployment / verification): `dispatch.py release --workspace alfred` — PR already created, user can verify independently

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

## Interaction Examples

### Bug Fix (with remote env)

```
User: alfred 线上的 heartbeat 有 bug，定时任务没触发

Agent: 📁 Workspace: alfred (~/dev/github/alfred)
       🖥️ Env: alfred-prod (ssh → prod-server)
       Checking environment...

Agent: [Workspace] main branch, clean, Python 3.12.4
       [Env: alfred-prod]
         daemon: running (pid 5678, 3d uptime)
         Recent errors:
           10:15 ERROR heartbeat: Task 'daily-report' skipped
           09:45 ERROR heartbeat: Task 'paper-digest' skipped
       Start analysis?

User: 开始

Agent: Analysis complete:
       📍 heartbeat.py:142 HeartbeatRunner._should_run_task()
       🔍 naive datetime vs UTC comparison → timezone offset
       💡 Unify to timezone-aware datetime, Risk: low
       Proceed with fix?

User: 修吧

Agent: Coding complete, running tests...

Agent: ✅ Tests passed (42 passed, ruff clean)
       📝 heartbeat.py (+3, -2)
       Submit PR?

User: 提交

Agent: PR created: https://github.com/.../pull/15
       Need to verify the fix in staging?

User: 等我部署看看

       ... (user deploys via CI/CD) ...

User: 部署好了

Agent: Verifying env alfred-prod...

Agent: ✅ Env verification passed:
         Resolved: 2 heartbeat-related errors
         No new errors in last 30 minutes
       Release workspace?

User: 好的

Agent: Workspace alfred released.
```

### Feature Development (no env)

```
User: 帮我在 alfred 加个 workspace list 命令

Agent: 📁 Workspace: alfred, main branch ✓
       Feature development — skipping env probe.
       Start analysis?

User: 开始

Agent: Analysis complete:
       📍 dispatch.py — add "workspace-list" subcommand
       💡 Add list_workspaces() to WorkspaceManager, wire in dispatch.py
       Proceed?

User: 继续
...
```

### Feature Split (large task)

```
User: 帮我重构 alfred 的认证系统

Agent: 📁 Workspace: alfred, main branch ✓
       Feature development — skipping env probe.
       Start analysis?

User: 开始

Agent: Analysis complete. This task needs splitting:
       1. Extract auth middleware
       2. Add JWT verification (depends on 1)
       3. Migrate session logic (depends on 2)
       Proceed with this plan?

User: 可以

Agent: Starting Feature 1/3: Extract auth middleware
       Coding complete, running tests...

Agent: ✅ Tests passed. 📝 auth_middleware.py (+45), app.py (-30)
       Submit PR?

User: 提交

Agent: PR #15 created.
       ✅ Feature 1/3 complete.
       Next: Feature 2/3 "Add JWT verification". Continue?

User: JWT 不做了，直接做 session 迁移

Agent: OK, skipping Feature 2.
       Starting Feature 3/3: Migrate session logic...
```

### Config Management

```
User: 添加 workspace my-app ~/dev/my-app

Agent: ✅ workspace added:
       my-app → ~/dev/my-app

User: 添加 env my-app-prod root@server:/opt/my-app

Agent: ✅ env added:
       my-app-prod → root@server:/opt/my-app

User: 列出环境

Agent: Workspaces:
         alfred     ~/dev/github/alfred     [idle]
         my-app     ~/dev/my-app            [idle]
       Envs:
         alfred-local   ~/dev/github/alfred           [local]
         alfred-prod    deploy@prod-server:/opt/alfred [ssh]
         my-app-prod    root@server:/opt/my-app       [ssh]
```
