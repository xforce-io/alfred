---
name: coding-master
description: "Convention-driven code expert for all code-related work"
version: "4.0.0"
tags: [coding, development, review, debug, analysis, pr, automation, parallel]
---

# Coding Master

`$CM = python $SKILL_DIR/scripts/tools.py`

All commands return JSON `{"ok": true, "data": {...}}` or `{"ok": false, "error": "..."}`.

## Modes

Coding Master handles all code-related work. Each session runs in a **mode** that defines constraints, not a fixed pipeline. You choose your own path; the system only enforces what artifacts must exist before completion.

| Mode | Purpose | Required Artifacts | Completion Gate |
|------|---------|-------------------|-----------------|
| `deliver` | Feature delivery (default) | `evidence/N-verify.json` per feature | all features done + evidence pass |
| `review` | Code review & feedback | `scope.json`, `report.md` | report.md exists |
| `debug` | Investigate & diagnose | `scope.json`, `diagnosis.md` | diagnosis.md exists |
| `analyze` | Understand code, produce conclusions | `scope.json`, `report.md` | report.md exists |

Lock with mode: `$CM lock --repo <name> --mode review`

**Constraints are hard, paths are soft.** The system tells you what's missing (`cm progress`), not what order to do things. You decide.

## Working Directory

All dev state lives in the target repo's `.coding-master/` directory (gitignored):

| File | Format | Purpose | Maintained by |
|------|--------|---------|---------------|
| `lock.json` | JSON | workspace lock (includes mode) | tools |
| `scope.json` | JSON | analysis/review/debug scope | tools via `cm scope` |
| `report.md` | MD | review/analysis report | tools via `cm report` |
| `diagnosis.md` | MD | debug diagnosis | tools via `cm report` (debug mode) |
| `PLAN.md` | MD | feature specs (deliver mode) | you create, then read-only |
| `JOURNAL.md` | MD | dev log (append-only) | tools auto + you via cm journal |
| `claims.json` | JSON | feature claim state (deliver mode) | tools |
| `features/XX.md` | MD | per-feature workspace (deliver mode) | feature owner |
| `evidence/XX-verify.json` | JSON | lint+typecheck+test structured results | tools |
| `evidence/integration-report.json` | JSON | integration merge+test report | tools |
| `delegation/<feature>/request.json` | JSON | engine delegation request | tools |
| `delegation/<feature>/result.json` | JSON | engine delegation result | external engine + tools |

**Principle**: JSON for tool atomics (lock, claim), MD for you to read/write (specs, logs).
**SKILL.md is immutable**: you must not modify it.

## Flow: deliver mode (default)

### Session Level
1. **Lock** — `$CM lock --repo <name>` (session: locked)
2. **Plan** — Create `.coding-master/PLAN.md` defining features + acceptance criteria
3. **Review** — `$CM plan-ready` validates PLAN.md (session: locked → reviewed)

### Feature Level (repeat per feature)
4. **Claim** — `$CM claim --feature <n>` (feature: pending → analyzing, session: working)
5. **Analyze** — Write Analysis + Plan in `features/XX.md`
6. **Enter dev** — `$CM dev --feature <n>` (feature: analyzing → developing)
7. **Code** — Edit code in worktree, git commit
8. **Test** — `$CM test --feature <n>` (updates test_status)
9. **Fix loop** — If failed: read test_output → fix → commit → `$CM test` → until passed
10. **Done** — `$CM done --feature <n>` (feature: developing → done, requires test_status=passed && test_commit=HEAD)
11. **Next** — Claim next available feature, repeat 4-10

### Wrap-up
12. **Progress** — `$CM progress` shows status + action guidance at any time
13. **Integrate** — All done → `$CM integrate` (merge branches → full tests → session: integrating)
14. **Fix integration** — If failed: `$CM reopen --feature <n>` → fix → test → done → retry integrate
15. **Submit** — `$CM submit --title "..."` (push + PR + cleanup → session: done)

### Parallel Development
- Multiple agents claim different features simultaneously (`$CM claim`)
- Each agent edits only their own `features/XX.md` — no conflicts
- `$CM claim` auto-checks dependencies; blocked features cannot be claimed
- `$CM done` reports newly unblocked features

## Flow: review / debug / analyze modes

No fixed sequence. Work freely, check progress anytime:

1. `$CM lock --repo <name> --mode review` — start session
2. `$CM scope --diff HEAD~3..HEAD` — define what to look at
3. Read code, analyze, delegate to engine if needed
4. `$CM report --content '...'` — write structured findings
5. `$CM progress` — check artifact gaps, see what's still missing
6. `$CM unlock` — done

`cm progress` in these modes shows **artifact status** (what exists, what's missing) instead of feature phases.

### Autonomous Mode
1. `$CM progress` → read `next_action` and `artifact_status`
2. If artifacts are missing, work to produce them
3. If `completion_ready` = true, finish up
4. Repeat until done

This is the recommended way to work. `cm progress` always knows what's missing.

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
5. **Keep feature MD updated** — `features/XX.md` is your work record
6. **JOURNAL.md is append-only** — use `$CM journal` to add entries
7. **Test before done** — `$CM done` checks evidence/N-verify.json (overall=passed + commit=HEAD); code changes require re-test
8. **Release lock when done**
9. **Only edit your own feature MD** — don't touch others' files
10. **Only work in your own worktree** — don't enter others' worktrees
11. **Evidence is mandatory after first v4 verify** — once a feature has `evidence/N-verify.json`, `cm done` uses that file
12. **Trust local progress first** — when unsure, run `$CM progress` and follow `next_action`
13. **Do not steal others' work** — `session_next_action` may describe a global need; only act on it if owner-safe
14. **Respect delegation hard gates** — when `must_delegate=true`, do not run execute commands until delegation is completed

## Templates

### PLAN.md

    # Feature Plan

    ## Origin Task
    <!-- original task description -->

    ## Features

    ### Feature 1: ...
    **Depends on**: —

    #### Task
    <!-- description -->

    #### Acceptance Criteria
    - [ ] ...

    ---

    ### Feature 2: ...
    **Depends on**: Feature 1

    #### Task
    #### Acceptance Criteria

### features/XX.md

    # Feature N: <title>

    ## Spec
    > Copied from PLAN.md

    **Acceptance Criteria**:
    - [ ] ...

    ## Analysis
    ## Plan
    ## Test Results
    ## Dev Log
