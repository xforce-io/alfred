# SOP: Deep Review

Use when user asks to "review", "审查", or "分析" a project for issues/improvements.

**Core principle**: Always use engine-powered analysis (`analyze --repos`). Quick commands alone are too shallow for a real review. Review is read-only — no workspace lock needed.

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

### Step 2: Engine Analysis (no lock, direct on repo)

```bash
_bash("$D analyze --repos <repo_name> --task 'Full project review: identify high-priority bugs, code quality issues, architecture improvements, and security concerns. Check test coverage, error handling, and documentation gaps.' --engine codex")
```

If `ENGINE_ERROR` → retry with `--engine claude`. If both fail → fall back to manual analysis.

Present `data.summary` to user with structured findings:
- High priority issues (bugs, security)
- Medium priority (code quality, architecture)
- Low priority (style, documentation)

If review identifies actionable fixes → ask user whether to proceed to development (load **Bugfix Workflow** or **Feature Dev** SOP).

---

## Key Rules

- **Always use engine** — `analyze` reads the full codebase and produces deep analysis. Scanning with grep/find is not a substitute.
- **No workspace needed** — review uses `--repos` mode (read-only, lock-free). Only bugfix/feature-dev flows need `workspace-check`.
- **WAIT for user** before proceeding to any fix/development.
