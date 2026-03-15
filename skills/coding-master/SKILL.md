---
name: coding-master
description: "Convention-driven code expert for all code-related work"
version: "5.0.0"
tags: [coding, development, review, debug, analysis, pr, automation, parallel]
---

# Coding Master

> **MANDATORY**: All code work MUST go through `_cm_next` and the 7 agent-facing tools below.
> Do NOT use raw bash/grep/read/write to substitute `cm` workflows.
> **Primary entry point**: Call `_cm_next(repo="<name>")` — it drives the entire workflow automatically, stopping only when your input is needed.
> **Session continuity**: If prior `_cm_next` results are visible in conversation history, continue by calling `_cm_next` again (not `_cm_status` or any internal tool).

## Agent-Facing Tools (v5.0)

| Tool | Purpose |
|------|---------|
| `_cm_next(repo, [intent], [mode], [force])` | **Primary workflow driver** — auto-advances through all mechanical steps, stops at creative breakpoints requiring your input |
| `_cm_edit(repo, file, old_text, new_text)` | Edit files in the workspace (PLAN.md, feature MDs, report.md, source code) |
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
| `write_analysis` | Feature analysis+plan section needed | Write the Analysis and Plan sections in `features/NN.md` via `_cm_edit`, then call `_cm_next` |
| `write_code` | Feature code changes needed | Edit source files via `_cm_edit`, then call `_cm_next(intent="test")` |
| `fix_code` | Tests failed | Fix the failing code via `_cm_edit`, then call `_cm_next(intent="test")` |
| `complete` | All features done, PR submitted | Session finished — present PR URL to user. (Title auto-generated from PLAN.md; only shown as `need_title` if auto-gen fails) |

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
| *(none)* | Continue from current state |
| `test` | After editing code — triggers lint+typecheck+tests |
| `scope` | Trigger scope definition (use with `diff`, `files`, `pr`, `goal`). Can also just pass `diff`/`files` directly to `_cm_next` without `intent="scope"`. |
| `submit` | After all features done — triggers push+PR+cleanup. `title` optional (auto-generated from PLAN.md if omitted) |

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
6. **Use `_cm_next` as the loop** — call it repeatedly; it decides what step comes next
7. **Trust the breakpoint** — when `_cm_next` says `write_code`, write code; don't run extra diagnostics first
8. **`_cm_edit` for all writes** — never use bash/write tools to modify workspace files
9. **Dispatcher for analysis** — review/analyze/debug modes use engine subprocess; do not read/grep code yourself
10. **Report changes with diff** — after code changes, call `_cm_next` and present the unified diff from `context.diff` to the user
11. **One issue at a time** — handle multiple issues sequentially; fully resolve one before starting the next
