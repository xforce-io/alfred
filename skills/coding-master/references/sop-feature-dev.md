# SOP: Feature Development

Use when user wants to add new functionality, refactor, or develop features. Follows Phase 0–7, with Feature Management for complex tasks.

> `workspace-check` creates `.coding-master/session.json`. All subsequent commands require this session (`NO_SESSION` error otherwise).

---

## Phase 0: Workspace Check

```bash
# Repo-based (recommended) — auto-allocates workspace slot
_bash("$D workspace-check --repos dolphin --task 'feat: add user auth' --engine codex")
# Multi-repo
_bash("$D workspace-check --repos dolphin,shared-lib --workspace env0 --auto-clean --task 'cross-repo refactor' --engine claude")
```
Output: `data.snapshot` (repos array with git/runtime/project info, `base_commit`, `primary_repo`).

**WAIT for user confirmation** before proceeding.

## Phase 2: Analysis

```bash
_bash("$D analyze --workspace <ws> --task '<feature description>' --engine codex")
```
Output: `data.summary`, `data.complexity` (trivial|standard|complex), `data.feature_plan_created`, `data.feature_count`.

## Phase 3: Plan Confirmation & Complexity Branching

- **trivial** → auto-proceed to Phase 4
- **standard** → **WAIT**: "继续"→Phase 4, "用方案2"→Phase 4 with alt, "取消"→release
- **complex** → Feature Plan auto-generated; enter **Feature Loop** (see below)

## Phase 4: Development

```bash
_bash("$D develop --workspace <ws> --task '<description>' --plan '<confirmed plan>' --branch feat/<name> --engine codex")
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
_bash("$D submit-pr --workspace <ws> --title 'feat: <description>' --body '...'")
```
Output: `data.pr_url`. Release immediately after PR is created.

## Release (mandatory — every task must end here)

```bash
_bash("$D release --workspace <ws>")              # normal
_bash("$D release --workspace <ws> --cleanup")     # rollback branch
```
Lease: 2h default. Renew with `$D renew-lease --workspace <ws>`.

---

## Feature Management (for complex tasks)

When `complexity: complex`, the engine auto-generates a Feature Plan in Phase 2. You can also create one manually.

### Commands

```bash
_bash("$D feature-plan --workspace <ws> --task 'refactor auth' --features '[{\"title\":\"extract middleware\",\"task\":\"...\",\"acceptance_criteria\":[{\"type\":\"test\",\"target\":\"pytest tests/auth/\",\"description\":\"pass\"}]},{\"title\":\"add JWT\",\"task\":\"...\",\"depends_on\":[0]}]'")
_bash("$D feature-list --workspace <ws>")
_bash("$D feature-criteria --workspace <ws> --index 0 --action view")
_bash("$D feature-criteria --workspace <ws> --index 0 --action append --criteria '{\"type\":\"test\",\"target\":\"pytest tests/auth/test_jwt.py\",\"description\":\"pass\"}'")
_bash("$D feature-verify --workspace <ws> --index 0 --engine codex")
_bash("$D feature-done --workspace <ws> --index 0 --branch feat/auth --pr '#15'")
_bash("$D feature-update --workspace <ws> --index 1 --status skipped")
```

Criteria types: `test` (subprocess, blocks done), `assert` (engine verify, blocks done), `manual` (reminder only).

### Feature Loop

`feature-next` → `develop` → `test` → `feature-verify` → `feature-done` (max 3 attempts per feature). Ask user before starting each feature. When `all_complete` → `submit-pr` → `release`.

---

## Error Handling

| error_code | Action |
|------------|--------|
| `NO_SESSION` / `LOCK_NOT_FOUND` / `LEASE_EXPIRED` | Run `workspace-check` to start/restart session |
| `PATH_NOT_FOUND` / `INVALID_ARGS` | Check `hint` field; run `config-list` for correct names; try `--repos <name>` |
| `WORKSPACE_LOCKED` | Use `quick-find --repos` for read-only search; or ask user to release workspace |
| `GIT_DIRTY` | Ask user to commit or stash |
| `WORKSPACE_GIT_DIRTY` | Add `--auto-clean` to `workspace-check`, or `release --cleanup` |
| `ENGINE_TIMEOUT` / `ENGINE_ERROR` | Release workspace; retry with other engine or simpler task |
| `TEST_FAILED` | Max 2 auto-fix rounds, then ask user (manual fix / abandon / keep branch) |

**Cancellation**: `release --workspace <name>` at any phase. Add `--cleanup` to rollback branch (Phase 4-5). If PR exists (Phase 6+), tell user to close PR first.

## Safety Rules

1. Never push to main/master — always feature/fix branches
2. Never force push; never auto-merge PRs
3. **WAIT for user** at Phase 0, Phase 3 (standard/complex), Phase 5, Phase 6
4. Respect locks — never force acquire; `WORKSPACE_LOCKED` → use `--repos` for read-only fallback
5. Max 2 auto-fix rounds, then escalate
6. `dispatch.py` is the sole workflow entry point — no direct `_bash`/`_write_file` on workspace files (except code authoring when engines unavailable)
7. Always release — forgetting blocks future tasks
