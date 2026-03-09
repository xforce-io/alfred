---
name: coding-master
description: "Convention-driven code development with minimal tooling"
version: "3.0.0"
tags: [coding, development, pr, automation, parallel]
---

# Coding Master

`$CM = python $SKILL_DIR/scripts/tools.py`

All commands return JSON `{"ok": true, "data": {...}}` or `{"ok": false, "error": "..."}`.

## Working Directory

All dev state lives in the target repo's `.coding-master/` directory (gitignored):

| File | Format | Purpose | Maintained by |
|------|--------|---------|---------------|
| `lock.json` | JSON | workspace lock | tools |
| `PLAN.md` | MD | feature specs | you create, then read-only |
| `JOURNAL.md` | MD | dev log (append-only) | tools auto + you via cm journal |
| `claims.json` | JSON | feature claim state | tools |
| `features/XX.md` | MD | per-feature workspace | feature owner |
| `evidence/XX-verify.json` | JSON | lint+typecheck+test structured results | tools |
| `evidence/integration-report.json` | JSON | integration merge+test report | tools |

**Principle**: JSON for tool atomics (lock, claim), MD for you to read/write (specs, logs).
**SKILL.md is immutable**: you must not modify it.

## Development Flow

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

### Autonomous Mode
After `cm plan-ready`, you can enter an autonomous loop:
1. `$CM progress` → read `next_action`
2. Execute `next_action` if present
3. If `next_action` is null, inspect `session_next_action` for global status
4. Repeat until `session_phase` = `done`

This is the recommended way to work. `cm progress` always knows the best local next step and the best session-level next step.

## Tools

| Tool | Purpose |
|------|---------|
| `$CM start --repo <name> [--plan-file path]` | One-shot: lock + copy plan + plan-ready |
| `$CM lock --repo <name>` | Lock workspace, create dev branch |
| `$CM unlock --repo <name>` | Release lock |
| `$CM plan-ready` | Validate PLAN.md → session: locked → reviewed |
| `$CM claim --feature <n>` | Claim feature, create branch/worktree/feature-MD |
| `$CM dev --feature <n>` | Check Analysis+Plan → analyzing → developing |
| `$CM test --feature <n>` | Run lint+typecheck+tests → write evidence/N-verify.json + update claims |
| `$CM done --feature <n>` | Check tests passed + no new commits → developing → done |
| `$CM reopen --feature <n>` | Integration fix: done → developing (reset test_status) |
| `$CM integrate` | All done → merge feature branches → full tests → session: integrating |
| `$CM progress` | Show session + feature status + step-by-step action guidance |
| `$CM submit --title "..."` | Idempotent: push → PR → cleanup → unlock |
| `$CM renew` | Renew lock lease (long tasks) |
| `$CM journal --message "..."` | Append to JOURNAL.md (flock protected) |
| `$CM doctor --repo <name>` | Diagnose state, `--fix` to auto-repair |
| `$CM status --repo <name>` | Show lock status |

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
