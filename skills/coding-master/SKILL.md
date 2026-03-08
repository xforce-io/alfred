---
name: coding-master
description: "Code review, development, and ops for registered repos"
version: "0.2.0"
tags: [coding, review, development, bugfix, pr, automation]
---

# Coding Master Skill

`$D = python $SKILL_DIR/scripts/dispatch.py`

All commands return JSON `{"ok": true, "data": {...}}` or `{"ok": false, "error_code": "...", "hint": "..."}`.
On failure, follow `hint` to recover.

## Core Commands

| Command | Description | timeout |
|---------|-------------|---------|
| `$D status --repos <name>` | git status / diff overview | default |
| `$D find --repos <name> --query <pattern>` | Search code | default |
| `$D analyze --repos <name> --task "<desc>"` | Engine deep analysis | 600 |
| `$D auto-dev --repos <name> --task "<desc>"` | Develop + test (one step) | 600 |
| `$D submit --repos <name> --title "<title>"` | Commit + push + create PR (auto-releases workspace) | default |
| `$D release --workspace <ws>` | Release workspace lock | default |

> Commands without explicit timeout use 120s default.

### auto-dev Options

- `--branch <name>` — specify branch name (auto-generated if omitted)
- `--engine <claude|codex>` — specify engine (default from config)
- `--feature next` — develop next task from feature plan
- `--workspace <name>` — use existing workspace (required for feature mode)
- `--repo <name>` — target repo in multi-repo workspace
- `--plan "<desc>"` — specify implementation plan
- `--allow-complex` — skip complexity check, force single auto-dev
- `--reset-worktree` — clean uncommitted changes before developing

### auto-dev Behavior

- Execution unit is a **single target repo**
- `--repos <name>` only supports single repo; multiple repos → `TASK_TOO_COMPLEX`
- Multi-repo workspace requires explicit `--repo <name>`; otherwise → `NEED_EXPLICIT_REPO`
- Engine develops code, runs tests, and fixes until tests pass (internal loop)
- Dispatch runs final verification independently after engine completes

### Submitting PRs

auto-dev does NOT auto-submit PRs. After tests pass:

- repo mode: `$D submit --repos <name> --title "<title>"`
- workspace / feature mode: `$D submit --workspace <ws> --title "<title>"`

`submit` auto-releases workspace on success. Add `--keep-lock` to keep working.

## Rules

- **All code operations must go through $D commands** — never use bare `_bash` for repo operations
- Engine commands (analyze, auto-dev) need `timeout=600`
- On failure, check `error_code` + `hint` and follow instructions
- After `auto-dev` / `submit`, release workspace if not continuing development
- Never push to main/master. Never force push. Never auto-merge PRs.

> More commands (workspace management, feature splitting, env probing, etc.):
> `_load_skill_resource("coding-master", "references/full-command-reference.md")`
> or `$D --help`
