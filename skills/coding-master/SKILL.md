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
| `review_changes` | Integration done; awaiting diff review | Present `diff_summary` to user. Then call `_cm_next(intent='confirm')`, `_cm_next(intent='fix', feedback='...')`, or `_cm_next(intent='abort')` |
| `complete` | All features done, PR submitted (or aborted) | **STOP — do NOT call `_cm_next` again.** Present `pr_url` to user (empty string if aborted). Session is finished. |

> **Note**: Analysis, coding, and test-fixing are handled automatically by the engine (claude-code subprocess). The agent never needs to edit source code directly.

### review/debug/analyze mode breakpoints

| Breakpoint | What it means | What to do |
|-----------|--------------|-----------|
| `define_scope` | Scope not yet defined | Call `_cm_next(diff="HEAD~3..HEAD")` or `_cm_next(files="src/foo.py")` — scope+engine run in one step |
| `write_report` | Engine finished; report not written | Write `.coding-master/report.md` via `_cm_edit`, then call `_cm_next` |
| `complete` | Report written | **STOP — do NOT call `_cm_next` again.** Session complete; present findings to user. |

## Intent Parameter

Use `intent` to signal what you just did or want to trigger:

| Intent | When to use |
|--------|------------|
| *(none)* | Continue from current state (most common — just call `_cm_next` after writing PLAN.md) |
| `confirm` | At `review_changes`: approve the diff and submit |
| `fix` | At `review_changes`: request an inline fix. Pass `feedback="what to change"` |
| `abort` | At `review_changes`: discard PR, preserve work on branch, unlock session |
| `skip_feature` | At `working`: skip a feature. Pass `feature=N`. Marks feature as skipped, removes its worktree, continues to next feature or integrate. |
| `scope` | Define analysis scope. Can also just pass `diff`/`files` directly without `intent="scope"`. |
| `submit` | Force submit with explicit title. Usually not needed — auto-submits with title from PLAN.md. |

## PLAN.md Format

When `_cm_next` returns `write_plan`, write `.coding-master/PLAN.md` in this exact format:

```markdown
# Feature Plan

## Origin Task
<describe what needs to be done>

## Features

### Feature 1: <title>
**Depends on**: —

#### Task
<what to implement>

#### Acceptance Criteria
- [ ] <criterion 1>
- [ ] <criterion 2>
```

**Default: 1 feature.** Most tasks need only one feature. Do NOT split analysis/scanning into a separate feature — that is the engine's analyze phase.

If you genuinely need multiple features (independent, non-mergeable work streams), add `## Max Features: N` with a justification line after `## Origin Task`:

```markdown
## Max Features: 2
Refactor and new logic are independent changes requiring separate interface validation.
```

Without this declaration, `plan-ready` will reject plans with more than 1 feature.

## Breakpoint Discipline

**The `instruction` field is mandatory.** When `_cm_next` returns a breakpoint with `feature=N`, you MUST work on Feature N — you cannot choose a different feature or bypass dependencies by passing `feature=M` to the next call. The system enforces dependency order; attempting to skip it will return the same breakpoint repeatedly.

**Do not retry a breakpoint without doing the required work.** If you call `_cm_next` and get the same breakpoint 3+ times without completing the `instruction`, the system will **hard-block** with `ok: false`. This is not a warning — the tool will refuse to proceed. Stop and do the work first.

**`complete` breakpoint = STOP.** Do NOT call `_cm_next` after receiving `complete`. The session is finished. Present the result to the user and wait for a new request.

## Common Scenarios

### User wants to review code before submitting

The `review_changes` breakpoint shows the diff automatically after integration. To reach it:

1. If features are still in progress and user wants to skip them:
   ```
   _cm_next(repo="<name>", intent="skip_feature", feature=N)
   ```
2. Then call `_cm_next(repo="<name>")` — system integrates and returns `review_changes` with `diff_summary`
3. Present the diff to the user, then:
   - User approves → `_cm_next(intent="confirm")`
   - User wants changes → `_cm_next(intent="fix", feedback="...")`
   - User cancels → `_cm_next(intent="abort")`

**Do NOT** call `_cm_next(mode="review")` to show code diff — that starts a separate review session, not a diff of the current delivery.

### User wants to skip a feature

```
_cm_next(repo="<name>", intent="skip_feature", feature=N)
```

Marks Feature N as skipped, cleans up its worktree, and auto-advances.

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
12. **Default 1 feature** — plan-ready enforces max_features=1 by default. Only add `## Max Features: N` (with justification) when genuinely needed. Never split analysis/scan into a separate feature.
