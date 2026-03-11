---
name: coding-master
description: "Convention-driven code expert for all code-related work"
version: "4.1.0"
tags: [coding, development, review, debug, analysis, pr, automation, parallel]
---

# Coding Master

> **MANDATORY**: All code work MUST go through `$CM` commands below.
> Do NOT use raw bash/grep/read to substitute `$CM` workflows.
> **Session continuity**: If prior `$CM` results are visible in conversation history, you are already initialized — skip `$CM lock` / `$CM repos` / `$CM scope` and continue from where you left off. Only run `$CM lock` on the very first message of a session.
> **EXECUTE, DON'T DISPLAY**: Always run `$CM` commands via the `_bash` tool call.
> NEVER output commands as text/code blocks — the user cannot run them.

`$CM = python $SKILL_DIR/scripts/tools.py`

All commands return JSON `{"ok": true, "data": {...}}` or `{"ok": false, "error": "..."}`.

## Quick Start by Mode

### Discovery — list available repos

```
$CM repos                              # list configured repos and workspaces
```

### review / debug / analyze (most common)

```
$CM lock --repo <name> --mode review    # or debug / analyze
$CM scope --diff HEAD~3..HEAD           # define what to look at
# ... read code, analyze ...
$CM report --content '...'              # write findings
$CM progress                            # check what's missing
$CM unlock                              # done
```

### deliver (feature development)

```
$CM lock --repo <name>                  # default mode = deliver
# Create .coding-master/PLAN.md with features + acceptance criteria
$CM plan-ready                          # validate plan
$CM claim --feature <n>                 # claim a feature
# Write Analysis + Plan in features/XX.md
$CM dev --feature <n>                   # enter dev phase
# Edit code, git commit
$CM test --feature <n>                  # run lint+typecheck+tests
$CM done --feature <n>                  # mark complete (requires tests passed)
# Repeat claim → dev → test → done for each feature
$CM integrate                           # merge all → full tests
$CM submit --title "..."                # push + PR + cleanup
```

## Modes

| Mode | Purpose | Required Artifacts | Completion Gate |
|------|---------|-------------------|-----------------|
| `deliver` | Feature delivery (default) | `evidence/N-verify.json` per feature | all features done + evidence pass |
| `review` | Code review & feedback | `scope.json`, `report.md` | report.md exists |
| `debug` | Investigate & diagnose | `scope.json`, `diagnosis.md` | diagnosis.md exists |
| `analyze` | Understand code, produce conclusions | `scope.json`, `report.md` | report.md exists |

**Constraints are hard, paths are soft.** `$CM progress` tells you what's missing, not what order to do things.

## Tools

| Tool | Modes | Purpose |
|------|-------|---------|
| `$CM start --repo <name> [--mode M] [--plan-file path]` | all | One-shot: lock + plan + plan-ready |
| `$CM lock --repo <name> [--mode M]` | all | Lock workspace, create dev branch |
| `$CM unlock --repo <name>` | all | Release lock |
| `$CM scope [--diff R] [--files F] [--pr N] [--goal G]` | review/debug/analyze | Define analysis scope |
| `$CM report [--content C] [--file F]` | review/debug/analyze | Write report or diagnosis |
| `$CM plan-ready` | deliver | Validate PLAN.md → session: locked → reviewed |
| `$CM claim --feature <n>` | deliver | Claim feature, create branch/worktree/feature-MD |
| `$CM delegate-prepare --feature <n>` | deliver | Write delegation request and mark delegation running |
| `$CM delegate-complete --feature <n>` | deliver | Verify delegation artifacts and unlock execute |
| `$CM dev --feature <n>` | deliver/debug | Check Analysis+Plan → analyzing → developing |
| `$CM test --feature <n>` | deliver/debug | Run lint+typecheck+tests → write evidence |
| `$CM done --feature <n>` | deliver | Check tests passed + no new commits → developing → done |
| `$CM reopen --feature <n>` | deliver | Integration fix: done → developing |
| `$CM integrate` | deliver | All done → merge feature branches → full tests |
| `$CM progress` | all | Show status + artifact gaps + action guidance |
| `$CM submit --title "..."` | deliver | Idempotent: push → PR → cleanup → unlock |
| `$CM renew` | all | Renew lock lease |
| `$CM journal --message "..."` | all | Append to JOURNAL.md |
| `$CM doctor --repo <name>` | all | Diagnose state, `--fix` to auto-repair |
| `$CM status --repo <name>` | all | Show lock status |

## Rules

1. **All code changes stay in the target repo**
2. **Never push main/master** — always on feature branches
3. **Never force push**
4. **Don't modify SKILL.md** — immutable convention
5. **Test before done** — `$CM done` checks evidence (overall=passed + commit=HEAD)
6. **Release lock when done**
7. **Trust local progress first** — when unsure, run `$CM progress` and follow `next_action`
8. **Respect delegation hard gates** — when `must_delegate=true`, wait for delegation completion
