# SOP: Deep Review

Use when user asks to "review", "审查", or "分析" a project for issues/improvements.

**Core principle**: Always use engine-powered analysis (`analyze` command). Quick commands alone are too shallow for a real review.

---

## Flow

### Step 1: Gather Context (quick commands, no lock)

```bash
_bash("$D quick-status --repos <repo_name>")
_bash("$D quick-test --repos <repo_name> --lint")
```

Also run in the repo directory:
- `git log --oneline -20` — recent commit history
- `git diff --stat` — if dirty, see what's changed

### Step 2: Acquire Workspace & Run Engine Analysis

```bash
_bash("$D workspace-check --repos <repo_name> --task 'review: <用户的review目标>' --engine codex")
```

Note the allocated workspace name from output (e.g., `env0`), then:

```bash
_bash("$D analyze --workspace <allocated_ws> --task 'Full project review: identify high-priority bugs, code quality issues, architecture improvements, and security concerns. Check test coverage, error handling, and documentation gaps.' --engine codex")
```

If `ENGINE_ERROR` → retry with `--engine claude`. If both fail → fall back to manual analysis.

### Step 3: Report & Release

1. Present `data.summary` to user with structured findings:
   - High priority issues (bugs, security)
   - Medium priority (code quality, architecture)
   - Low priority (style, documentation)
2. Release workspace:
   ```bash
   _bash("$D release --workspace <allocated_ws>")
   ```
3. If review identifies actionable fixes → ask user whether to proceed to development (load **Bugfix Workflow** or **Feature Dev** SOP).

---

## Key Rules

- **Always use engine** — `analyze` reads the full codebase and produces deep analysis. Scanning with grep/find is not a substitute.
- **Always release** workspace when done, even if analysis fails.
- **WAIT for user** before proceeding to any fix/development.
