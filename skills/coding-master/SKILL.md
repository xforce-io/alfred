---
name: coding-master
description: "Convention-driven code expert for all code-related work"
version: "5.1.0"
tags: [coding, development, review, debug, analysis, pr, automation, parallel]
---

# Coding Master

> **MANDATORY**: All code work MUST go through `_cm_next` and the 7 agent-facing tools below.
> Do NOT use raw bash/grep/read/write to substitute `cm` workflows.
> **Primary entry point**: Call `_cm_next(repo="<name>")` — it drives the entire workflow automatically, stopping only when your input is needed.
> **Session continuity**: If prior `_cm_next` results are visible in conversation history, continue by calling `_cm_next` again (not `_cm_status` or any internal tool).

## Your Role

**You are a dispatcher, not a coder.** Your job:
1. Understand user intent → write PLAN.md (task decomposition)
2. Call `_cm_next` → engine (claude-code) handles all code work automatically
3. Present results to user

**Do NOT** read/grep/edit source code yourself. The engine does that.

## Agent-Facing Tools (v5.1)

| Tool | Purpose |
|------|---------|
| `_cm_next(repo, [intent], [mode], [force])` | **Primary workflow driver** — auto-advances through all steps including engine-delegated code work. Stops only at `write_plan`, `engine_failed`, or `complete`. |
| `_cm_edit(repo, file, old_text, new_text)` | Edit planning files (PLAN.md, feature MDs, report.md). **Not for source code** — engine handles that. |
| `_cm_read(repo, file)` | Read files from the workspace |
| `_cm_find(repo, pattern)` | Find files by glob pattern |
| `_cm_grep(repo, pattern, [path])` | Search file contents by regex |
| `_cm_status([repo])` | Without repo: list configured repos. With repo: show session + feature progress detail |
| `_cm_doctor(repo, [fix])` | Diagnose workspace state; pass `fix=True` to auto-repair |

## Quick Start

### Step 1 — Discover repos

```
_cm_status()                           # list all configured repos
```

### Step 2 — Start working

```
_cm_next(repo="<name>")                # starts or resumes — auto-advances to first breakpoint
```

`_cm_next` returns a **breakpoint** telling you exactly what to do next. Follow the `instruction` field.

### Step 3 — At each breakpoint, provide what's needed, then call `_cm_next` again

```
_cm_edit(repo="<name>", file=".coding-master/PLAN.md", old_text="", new_text="...")
_cm_next(repo="<name>")                # continue after editing
```

### Done — submit

```
_cm_next(repo="<name>", intent="submit", title="feat: ...")
```

## Modes

| Mode | Purpose | Set via |
|------|---------|---------|
| `deliver` | Feature delivery (default) | `_cm_next(mode="deliver")` or default |
| `review` | Code review & feedback | `_cm_next(mode="review")` |
| `debug` | Investigate & diagnose | `_cm_next(mode="debug")` |
| `analyze` | Understand code, produce conclusions | `_cm_next(mode="analyze")` |

## Breakpoints

`_cm_next` stops at **creative breakpoints** that require your input. Each returns:

```json
{
  "ok": true,
  "breakpoint": "<name>",
  "instruction": "<what to do now>",
  "context": { ... }
}
```

### deliver mode breakpoints

| Breakpoint | What it means | What to do |
|-----------|--------------|-----------|
| `write_plan` | PLAN.md is missing or empty | Write `.coding-master/PLAN.md` via `_cm_edit`, then call `_cm_next` |
| `engine_failed` | Engine failed after 3 retries | Review `error` field. Optionally fix manually via `_cm_edit`, then call `_cm_next`. Or report the failure to user. |
| `complete` | All features done, PR submitted | Present PR URL to user |

> **Note**: Analysis, coding, and test-fixing are handled automatically by the engine (claude-code subprocess). The agent never needs to edit source code directly.

### review/debug/analyze mode breakpoints

| Breakpoint | What it means | What to do |
|-----------|--------------|-----------|
| `define_scope` | Scope not yet defined | Call `_cm_next(diff="HEAD~3..HEAD")` or `_cm_next(files="src/foo.py")` — scope+engine run in one step |
| `write_report` | Engine finished; report not written | Write `.coding-master/report.md` via `_cm_edit`, then call `_cm_next` |
| `complete` | Report written | Session complete; present findings to user |

## Intent Parameter

Use `intent` to signal what you just did or want to trigger:

| Intent | When to use |
|--------|------------|
| *(none)* | Continue from current state (most common — just call `_cm_next` after writing PLAN.md) |
| `scope` | Define analysis scope. Can also just pass `diff`/`files` directly without `intent="scope"`. |
| `submit` | Force submit with explicit title. Usually not needed — auto-submits with title from PLAN.md. |

## PLAN.md Format

When `_cm_next` returns `write_plan`, write `.coding-master/PLAN.md` in this exact format:

```markdown
# Plan

## Overview
<brief description>

### Feature 1: <title>

#### Task
<what to implement>

#### Acceptance Criteria
- <criterion 1>
- <criterion 2>

### Feature 2: <title>

#### Task
<what to implement>

#### Acceptance Criteria
- <criterion 1>
```

## Rules

1. **All code changes stay in the target repo**
2. **Never push main/master** — always on feature branches
3. **Never force push**
4. **Don't modify SKILL.md** — immutable convention
5. **Test before done** — `_cm_next(intent="test")` must pass before `complete` breakpoint
6. **Use `_cm_next` as the loop** — call it repeatedly; it drives the entire workflow including engine delegation
7. **You are a dispatcher, not a coder** — do NOT read/grep/edit source code yourself; the engine handles all code work automatically
8. **`_cm_edit` only for planning files** — use `_cm_edit` for PLAN.md, feature MDs, report.md; never for source code
9. **Trust the auto-advance** — when you call `_cm_next`, it may take minutes (engine is running); wait for the result
10. **On `engine_failed`** — review the error, optionally fix with `_cm_edit`, then call `_cm_next` again; or report failure to user
11. **One issue at a time** — handle multiple issues sequentially; fully resolve one before starting the next
12. **Minimal PLAN** — for simple tasks, create a single-feature PLAN; do NOT over-decompose into multi-feature dependency chains
