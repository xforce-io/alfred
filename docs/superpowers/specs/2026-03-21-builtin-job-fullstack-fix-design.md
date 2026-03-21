# Built-in Job Full-Stack Fix

## Problem

Built-in cron jobs (memory-review, task-discover, health-check) are defined in `src/everbot/core/jobs/` and whitelisted in `cron.py:ALLOWED_JOBS`, but the system has three architectural gaps that prevent them from working end-to-end:

1. **normalize_routine()** in `reflection.py` drops `job`, `scanner`, `min_execution_interval` fields — even if the inspector LLM correctly proposes a job-bound routine, the fields are silently discarded before reaching `add_routine()`.

2. **Inspector has no self-healing** — when built-in job tasks are missing from HEARTBEAT.md (e.g., after a refactor or manual deletion), nothing detects or restores them. The inspector prompt doesn't mention jobs, and the LLM has no guidance to propose them.

3. **cron.py import failure gives no actionable error** — when `importlib.import_module` fails (e.g., daemon started from wrong working directory), the error is a bare `"No module named 'everbot'"` with no diagnostic context.

**Evidence**: `memory-review` has **never successfully executed** in production. heartbeat_events.jsonl shows continuous `skill_failed` with import errors, and the current HEARTBEAT.md contains zero job tasks.

## Design

### Change 1: normalize_routine field passthrough

**File**: `src/everbot/core/runtime/reflection.py`, function `normalize_routine` (line 219)

Add `job`, `scanner`, `min_execution_interval` to the returned dict. Strip None values so `add_routine()` uses its defaults for fields not present in the proposal.

```python
# After existing field extraction (line 240):
job = item.get("job")
scanner = item.get("scanner")
min_exec = item.get("min_execution_interval")

result = {
    "title": title,
    "description": description,
    "schedule": schedule,
    "execution_mode": execution_mode,
    "timezone_name": timezone_name,
    "timeout_seconds": timeout_seconds,
    "source": "heartbeat_reflect",
    "allow_duplicate": False,
}
# Conditionally add job fields (avoid passing None to add_routine)
if job:
    result["job"] = str(job).strip()
if scanner:
    result["scanner"] = str(scanner).strip()
if min_exec:
    result["min_execution_interval"] = str(min_exec).strip()

return result
```

### Change 2: Inspector self-healing for built-in jobs

**File**: `src/everbot/core/runtime/inspector.py`

Add a `_ensure_builtin_jobs()` method that runs during inspection. It compares registered job tasks in HEARTBEAT.md against a hardcoded defaults table, and registers any missing ones.

```python
_BUILTIN_JOB_DEFAULTS = {
    "memory-review": {
        "title": "Memory Review",
        "schedule": "2h",
        "scanner": "session",
        "min_execution_interval": "2h",
        "execution_mode": "inline",
        "timeout_seconds": 120,
    },
    "task-discover": {
        "title": "Task Discover",
        "schedule": "2h",
        "scanner": "session",
        "min_execution_interval": "2h",
        "execution_mode": "inline",
        "timeout_seconds": 120,
    },
}
```

**Why not health-check**: It's stateless, no scanner gate needed, low value as a recurring job.

**Why hardcoded, not a registry**: Only 2 jobs. A registry file would be over-engineering at this scale.

**Why Python logic, not prompt**: Zero token overhead. Deterministic. The inspector prompt stays unchanged.

Call site: invoke `_ensure_builtin_jobs()` early in the `inspect()` method, before reflection prompt construction. This ensures the jobs exist before the LLM even runs.

### Change 3: Defensive import error in cron.py

**File**: `src/everbot/core/runtime/cron.py`, function `_invoke_job` (line ~558)

Wrap the `importlib.import_module` call with a `ModuleNotFoundError` catch that provides actionable context:

```python
try:
    job_module = importlib.import_module(f"{_pkg}.jobs.{module_name}")
except ModuleNotFoundError as e:
    raise RuntimeError(
        f"Cannot import job module '{module_name}': {e}. "
        f"Ensure daemon runs from project root or package is installed."
    ) from e
```

### Change 4: Delete stale plan file

**File**: `docs/superpowers/plans/2026-03-17-skill-consolidation.md`

This plan has been fully executed (all tasks completed, verified by code inspection). Delete to avoid confusion.

## Testing

1. **normalize_routine**: Add test in `test_self_reflection.py` verifying job/scanner/min_execution_interval are passed through when present, and omitted when absent.

2. **_ensure_builtin_jobs**: Add test verifying:
   - Missing jobs are registered on empty HEARTBEAT.md
   - Already-registered jobs are not duplicated
   - Disabled jobs are re-registered

3. **cron.py import error**: Add test verifying the RuntimeError message when module import fails.

## Scope exclusions

- No changes to inspector prompt (zero token inflation)
- No new registry abstraction
- No changes to daemon startup mechanism or PYTHONPATH
- No changes to routine_cli.py (already correct)
- No changes to AGENTS.md (agent knowledge gap is addressed by self-healing, not documentation)
