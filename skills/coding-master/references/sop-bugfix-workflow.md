# SOP: Bugfix Workflow

Use when user reports a bug, error, or wants to fix an issue. Follows Phase 0–7.

> `workspace-check` creates `.coding-master/session.json`. All subsequent commands require this session (`NO_SESSION` error otherwise).

---

## Phase 0: Workspace Check

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

## Phase 1: Env Probing (optional — skip for pure code bugs)

```bash
_bash("$D env-probe --workspace <ws> --env alfred-prod")
_bash("$D env-probe --workspace <ws> --env alfred-prod --commands \"journalctl -u alfred --since '2 hours ago'\"")
```
Output: `data.modules` (process status, errors, logs).

## Phase 2: Analysis

```bash
_bash("$D analyze --workspace <ws> --task '<bug description>' --engine codex")
```
Output: `data.summary`, `data.complexity` (trivial|standard|complex), `data.feature_plan_created`, `data.feature_count`.

## Phase 3: Plan Confirmation

- **trivial** → auto-proceed to Phase 4
- **standard** → **WAIT**: "继续"→Phase 4, "用方案2"→Phase 4 with alt, "再看看日志"→back to Phase 1/2, "取消"→release
- **complex** → Feature Plan auto-generated; load **Feature Dev** SOP for Feature Loop

## Phase 4: Development

```bash
_bash("$D develop --workspace <ws> --task 'fix: <description>' --plan '<confirmed plan>' --branch fix/<name> --engine codex")
```
Output: `data.summary`, `data.files_changed`. Auto-proceed to Phase 5.

## Phase 5: Test

```bash
_bash("$D test --workspace <ws>")
```
Passed → report + ask to submit PR. Lint-only fail → auto-fix. Test fail → report to user. Max **2 auto-fix rounds**, then escalate. **WAIT before Phase 6.**

### Phase 5.5: Self-Review

Scan `git diff` file-by-file for unnecessary changes, convention violations. Minor → auto-fix; significant → report. Include in PR body.

## Phase 6: Submit PR

```bash
_bash("$D submit-pr --workspace <ws> --title 'fix: <description>' --body '...'")
```
Output: `data.pr_url`. If env was probed → ask "需要部署验证吗？"; otherwise release immediately.

## Phase 7: Env Verification (optional)

```bash
_bash("$D env-verify --workspace <ws> --env alfred-staging")
```
`data.resolved` → true: release; false: offer auto-fix / release / rollback (`release --cleanup`).

## Release (mandatory — every task must end here)

```bash
_bash("$D release --workspace <ws>")              # normal
_bash("$D release --workspace <ws> --cleanup")     # rollback branch
```
Lease: 2h default. Renew with `$D renew-lease --workspace <ws>`.

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

## Safety Rules

1. Never push to main/master — always feature/fix branches
2. Never force push; never auto-merge PRs
3. Env probing is read-only — no writes, restarts, or deployments
4. **WAIT for user** at Phase 0, Phase 3 (standard/complex), Phase 5, Phase 6
5. Respect locks — never force acquire; `WORKSPACE_LOCKED` → use `--repos` for read-only fallback
6. Max 2 auto-fix rounds, then escalate
7. `dispatch.py` is the sole workflow entry point — no direct `_bash`/`_write_file` on workspace files (except code authoring when engines unavailable)
8. Always release — forgetting blocks future tasks
