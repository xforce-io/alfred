---
name: coding-master
description: "Convention-driven code expert for all code-related work"
version: "4.1.0"
tags: [coding, development, review, debug, analysis, pr, automation, parallel]
---

# Coding Master

> **MANDATORY**: All code work MUST go through `cm` commands below.
> Do NOT use raw bash/grep/read to substitute `cm` workflows.
> **Session continuity**: If prior `cm` results are visible in conversation history, you are already initialized — skip `cm lock` / `cm repos` / `cm scope` and continue from where you left off. Only run `cm lock` on the very first message of a session.
> **EXECUTE, DON'T DISPLAY**: Always run `cm` commands via the corresponding `_cm_*` tool calls (e.g. `_cm_lock`, `_cm_scope`, `_cm_engine_run`).
> NEVER output commands as text/code blocks — the user cannot run them.

CLI: `cm <command> [options]`

All commands return JSON `{"ok": true, "data": {...}}` or `{"ok": false, "error": "..."}`.

## Quick Start by Mode

### Discovery — list available repos

```
cm repos                              # list configured repos and workspaces
```

### review / debug / analyze (most common)

```
cm lock --repo <name> --mode review    # or debug / analyze
cm scope --diff HEAD~3..HEAD           # define what to look at
cm engine-run                          # delegate analysis to engine subprocess
cm report --content '...'              # write findings based on engine results
cm unlock                              # done
```

> **You are a dispatcher, not an executor.** Define scope, delegate to engine, consume results, write report.
> `cm engine-run` invokes a subprocess that reads all files and returns structured findings.
> Do NOT read/grep/find code yourself — that is the engine's job.

### deliver (feature development)

```
cm lock --repo <name>                  # default mode = deliver
# Create .coding-master/PLAN.md with features + acceptance criteria
cm plan-ready                          # validate plan
cm claim --feature <n>                 # claim a feature
# Write Analysis + Plan in features/XX.md
cm dev --feature <n>                   # enter dev phase
# Edit code, git commit
cm test --feature <n>                  # run lint+typecheck+tests
cm done --feature <n>                  # mark complete (requires tests passed)
# Repeat claim → dev → test → done for each feature
cm integrate                           # merge all → full tests
cm submit --title "..."                # push + PR + cleanup
```

## Modes

| Mode | Purpose | Required Artifacts | Completion Gate |
|------|---------|-------------------|-----------------|
| `deliver` | Feature delivery (default) | `evidence/N-verify.json` per feature | all features done + evidence pass |
| `review` | Code review & feedback | `scope.json`, `report.md` | report.md exists |
| `debug` | Investigate & diagnose | `scope.json`, `diagnosis.md` | diagnosis.md exists |
| `analyze` | Understand code, produce conclusions | `scope.json`, `report.md` | report.md exists |

**Constraints are hard, paths are soft.** `cm progress` tells you what's missing, not what order to do things.

## Tools

| Tool | Modes | Purpose |
|------|-------|---------|
| `cm start --repo <name> [--mode M] [--plan-file path]` | all | One-shot: lock + plan + plan-ready |
| `cm lock --repo <name> [--mode M]` | all | Lock workspace, create dev branch |
| `cm unlock --repo <name>` | all | Release lock |
| `cm scope [--diff R] [--files F] [--pr N] [--goal G]` | review/debug/analyze | Define analysis scope |
| `cm report [--content C] [--file F]` | review/debug/analyze | Write report or diagnosis |
| `cm engine-run [--goal G] [--engine E] [--timeout T] [--max-turns N]` | all | Delegate analysis to engine subprocess |
| `cm plan-ready` | deliver | Validate PLAN.md → session: locked → reviewed |
| `cm claim --feature <n>` | deliver | Claim feature, create branch/worktree/feature-MD |
| `cm delegate-prepare --feature <n>` | deliver | Write delegation request and mark delegation running |
| `cm delegate-complete --feature <n>` | deliver | Verify delegation artifacts and unlock execute |
| `cm dev --feature <n>` | deliver/debug | Check Analysis+Plan → analyzing → developing |
| `cm test --feature <n>` | deliver/debug | Run lint+typecheck+tests → write evidence |
| `cm done --feature <n>` | deliver | Check tests passed + no new commits → developing → done |
| `cm reopen --feature <n>` | deliver | Integration fix: done → developing |
| `cm integrate` | deliver | All done → merge feature branches → full tests |
| `cm progress` | all | Show status + artifact gaps + action guidance |
| `cm submit --title "..."` | deliver | Idempotent: push → PR → cleanup → unlock |
| `cm renew` | all | Renew lock lease |
| `cm journal --message "..."` | all | Append to JOURNAL.md |
| `cm change-summary [--base-ref R]` | all | Generate change summary with unified diff + worktree path |
| `cm doctor --repo <name>` | all | Diagnose state, `--fix` to auto-repair |
| `cm status --repo <name>` | all | Show lock status |

## Rules

1. **All code changes stay in the target repo**
2. **Never push main/master** — always on feature branches
3. **Never force push**
4. **Don't modify SKILL.md** — immutable convention
5. **Test before done** — `cm done` checks evidence (overall=passed + commit=HEAD)
6. **Release lock when done**
7. **Trust local progress first** — when unsure, run `cm progress` and follow `next_action`
8. **Respect delegation hard gates** — when `must_delegate=true`, wait for delegation completion
9. **Dispatcher, not executor** — use `cm engine-run` for all code analysis. Do not read/grep/find code yourself
10. **Report changes with diff** — after making code changes, always call `cm change-summary` and present the unified diff, worktree path, and review command to the user. Never summarize changes as before/after snippets — show the actual `git diff`
