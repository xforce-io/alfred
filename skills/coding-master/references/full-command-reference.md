# Coding Master — Full Command Reference

`$D = python $SKILL_DIR/scripts/dispatch.py`

## Read-Only Operations (no workspace lock needed)

| Command | Description |
|---------|-------------|
| `$D status --repos <name>` | git status/diff (alias: `quick-status`) |
| `$D find --repos <name> --query <pattern> [--glob GLOB]` | Search code (alias: `quick-find`) |
| `$D quick-test --repos <name> [--path PATH] [--lint]` | Run tests |
| `$D quick-env --env <name> [--commands CMD...]` | Probe remote env |
| `$D analyze --repos <name> --task "<desc>" [--engine ENGINE]` | Deep analysis (timeout=600) |

All read-only commands also accept `--workspace <ws>` to operate on a locked workspace.

## One-Step Development

| Command | Description |
|---------|-------------|
| `$D auto-dev --repos <name> --task "<desc>"` | Develop + test in one step (timeout=600) |
| `$D submit --repos <name> --title "<title>"` | Commit + push + PR + auto-release |

### auto-dev Options

- `--branch <name>` — branch name
- `--engine <claude|codex>` — engine override
- `--feature next` — next feature from plan (requires `--workspace`)
- `--workspace <name>` — use existing workspace
- `--repo <name>` — target repo in multi-repo workspace
- `--plan "<desc>"` — implementation plan override
- `--allow-complex` — skip complexity heuristic
- `--reset-worktree` — git reset + clean before developing

### submit Options

- `--workspace <ws>` — explicit workspace
- `--repos <name>` — find active workspace by repo name
- `--repo <name>` — repo within workspace
- `--body "<body>"` — PR body
- `--keep-lock` — don't auto-release after submit

## Write Operations (require workspace lock)

### Workspace Lifecycle

| Command | Description |
|---------|-------------|
| `$D workspace-check --repos <name> --task "<desc>" [--engine ENGINE]` | Acquire workspace lock |
| `$D workspace-check --workspace <ws> --task "<desc>" [--engine ENGINE]` | Direct workspace mode |
| `$D release --workspace <ws>` | Release workspace lock |
| `$D release --all` | Release all workspace locks |
| `$D renew-lease --workspace <ws>` | Extend workspace lease |

### Development Commands (require active workspace)

| Command | Description |
|---------|-------------|
| `$D develop --workspace <ws> --task "<desc>" [--engine ENGINE] [--plan PLAN] [--branch BRANCH]` | Engine writes code (timeout=600) |
| `$D test --workspace <ws>` | Run lint + tests in workspace |
| `$D submit-pr --workspace <ws> [--repo <name>] --title "<title>" [--body "<body>"]` | Commit + push + create PR |

### Environment Probing (require active workspace)

| Command | Description |
|---------|-------------|
| `$D env-probe --workspace <ws> --env <name> [--commands CMD...]` | Probe runtime environment |
| `$D env-verify --workspace <ws> --env <name>` | Verify fix in deployment env |

### Feature Management (require active workspace)

| Command | Description |
|---------|-------------|
| `$D feature-plan --workspace <ws> --task "<task>" --features '<json>'` | Create feature split plan |
| `$D feature-next --workspace <ws>` | Get next executable feature |
| `$D feature-done --workspace <ws> --index N [--branch B] [--pr URL] [--force]` | Mark feature done |
| `$D feature-list --workspace <ws>` | List all features and status |
| `$D feature-update --workspace <ws> --index N [--status S] [--title T] [--task-desc D]` | Update feature |
| `$D feature-criteria --workspace <ws> --index N --action view\|append [--criteria JSON]` | Manage acceptance criteria |
| `$D feature-verify --workspace <ws> --index N [--engine E]` | Run acceptance criteria verification |

## Configuration

| Command | Description |
|---------|-------------|
| `$D config-list` | List all config |
| `$D config-add <kind> <name> <value>` | Add repo/workspace/env |
| `$D config-set <kind> <name> <key> <value>` | Set field on repo/workspace/env |
| `$D config-remove <kind> <name>` | Remove repo/workspace/env |

## Error Codes

| Code | Meaning | Recovery |
|------|---------|----------|
| `TASK_TOO_COMPLEX` | Task needs splitting | Use `analyze` first, then feature plan |
| `NEED_EXPLICIT_REPO` | Multi-repo workspace, repo ambiguous | Add `--repo <name>` |
| `FINAL_TEST_FAILED` | Engine completed but tests failed | Retry `auto-dev` or fix manually |
| `ENGINE_ERROR` | Engine failed | Try other engine: `--engine codex` / `--engine claude` |
| `WORKSPACE_LOCKED` | Workspace busy | Use `release` or wait |
| `LEASE_EXPIRED` | Lock expired | Run `workspace-check` again |
| `PATH_NOT_FOUND` | Config name not found | Run `config-list` |
| `NO_SESSION` | No workspace-check done | Run `workspace-check` first |
