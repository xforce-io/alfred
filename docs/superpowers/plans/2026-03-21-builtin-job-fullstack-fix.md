# Built-in Job Full-Stack Fix

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix three architectural gaps preventing built-in cron jobs (memory-review, task-discover) from working end-to-end.

**Architecture:** Three independent changes: (1) Pass job/scanner/min_execution_interval through normalize_routine, (2) Add inspector self-healing to detect and register missing built-in jobs, (3) Add defensive import error in cron.py. Plus cleanup of a stale plan file.

**Tech Stack:** Python, pytest, no new dependencies.

**Spec:** `docs/superpowers/specs/2026-03-21-builtin-job-fullstack-fix-design.md`

---

## File Structure

| Action | Path | Purpose |
|--------|------|---------|
| Modify | `src/everbot/core/runtime/reflection.py:241-250` | Pass through job/scanner/min_execution_interval |
| Modify | `src/everbot/core/runtime/inspector.py:588-605` | Add `_ensure_builtin_jobs()` call + method |
| Modify | `src/everbot/core/runtime/cron.py:558-559` | Defensive import error message |
| Modify | `tests/unit/test_inspector.py` | Tests for normalize_routine fields and _ensure_builtin_jobs |
| Modify | `tests/unit/test_cron_executor.py` | Test for import error message |
| Delete | `docs/superpowers/plans/2026-03-17-skill-consolidation.md` | Stale completed plan |

---

### Task 1: normalize_routine field passthrough

**Files:**
- Modify: `src/everbot/core/runtime/reflection.py:241-250`
- Test: `tests/unit/test_inspector.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_inspector.py`:

```python
class TestNormalizeRoutine:
    def test_passes_through_job_fields(self):
        item = {
            "title": "Memory Review",
            "schedule": "2h",
            "job": "memory-review",
            "scanner": "session",
            "min_execution_interval": "2h",
        }
        result = ReflectionManager.normalize_routine(item)
        assert result["job"] == "memory-review"
        assert result["scanner"] == "session"
        assert result["min_execution_interval"] == "2h"

    def test_omits_job_fields_when_absent(self):
        item = {"title": "Normal task", "schedule": "1d"}
        result = ReflectionManager.normalize_routine(item)
        assert "job" not in result
        assert "scanner" not in result
        assert "min_execution_interval" not in result

    def test_strips_whitespace_on_job_fields(self):
        item = {
            "title": "Test",
            "schedule": "1h",
            "job": " memory-review ",
            "scanner": " session ",
            "min_execution_interval": " 2h ",
        }
        result = ReflectionManager.normalize_routine(item)
        assert result["job"] == "memory-review"
        assert result["scanner"] == "session"
        assert result["min_execution_interval"] == "2h"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/unit/test_inspector.py::TestNormalizeRoutine -v
```

Expected: FAIL — `"job" not in result`

- [ ] **Step 3: Implement field passthrough**

In `src/everbot/core/runtime/reflection.py`, replace lines 241-250:

```python
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
        job = item.get("job")
        if job:
            result["job"] = str(job).strip()
        scanner = item.get("scanner")
        if scanner:
            result["scanner"] = str(scanner).strip()
        min_exec = item.get("min_execution_interval")
        if min_exec:
            result["min_execution_interval"] = str(min_exec).strip()
        return result
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/unit/test_inspector.py::TestNormalizeRoutine -v
```

Expected: 3 passed

- [ ] **Step 5: Run full test suite to check no regressions**

```bash
pytest tests/unit/ -q --tb=line
```

Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add src/everbot/core/runtime/reflection.py tests/unit/test_inspector.py
git commit -m "fix(reflection): pass job/scanner/min_execution_interval through normalize_routine"
```

---

### Task 2: Inspector self-healing for built-in jobs

**Files:**
- Modify: `src/everbot/core/runtime/inspector.py`
- Test: `tests/unit/test_inspector.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_inspector.py`:

```python
class TestEnsureBuiltinJobs:
    def test_registers_missing_jobs(self, tmp_path):
        routine_mgr = RoutineManager(tmp_path)
        inspector = _make_inspector(tmp_path, routine_manager=routine_mgr)

        registered = inspector._ensure_builtin_jobs()

        assert registered >= 2  # memory-review + task-discover
        task_list = routine_mgr.load_task_list()
        job_names = {t.job for t in task_list.tasks if t.job}
        assert "memory-review" in job_names
        assert "task-discover" in job_names

    def test_skips_already_registered_jobs(self, tmp_path):
        routine_mgr = RoutineManager(tmp_path)
        inspector = _make_inspector(tmp_path, routine_manager=routine_mgr)

        # First call registers
        first = inspector._ensure_builtin_jobs()
        # Second call should register nothing
        second = inspector._ensure_builtin_jobs()

        assert first >= 2
        assert second == 0

    def test_re_registers_disabled_jobs(self, tmp_path):
        routine_mgr = RoutineManager(tmp_path)
        inspector = _make_inspector(tmp_path, routine_manager=routine_mgr)

        # Register, then disable
        inspector._ensure_builtin_jobs()
        task_list = routine_mgr.load_task_list()
        for t in task_list.tasks:
            if t.job == "memory-review":
                t.enabled = False
        routine_mgr.flush(task_list)

        # Should re-register the disabled one
        registered = inspector._ensure_builtin_jobs()
        assert registered == 1

    def test_matches_by_job_field_not_title(self, tmp_path):
        routine_mgr = RoutineManager(tmp_path)
        # Manually add a task with the right job but different title
        routine_mgr.add_routine(
            title="Custom Memory Job Name",
            schedule="4h",
            job="memory-review",
            scanner="session",
        )
        inspector = _make_inspector(tmp_path, routine_manager=routine_mgr)

        registered = inspector._ensure_builtin_jobs()

        # memory-review already exists (different title), only task-discover should be added
        assert registered == 1
        task_list = routine_mgr.load_task_list()
        job_names = [t.job for t in task_list.tasks if t.job]
        assert job_names.count("memory-review") == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/unit/test_inspector.py::TestEnsureBuiltinJobs -v
```

Expected: FAIL — `Inspector has no attribute '_ensure_builtin_jobs'`

- [ ] **Step 3: Implement _ensure_builtin_jobs**

In `src/everbot/core/runtime/inspector.py`, add the defaults dict and method. Place the dict at module level (after imports), and the method inside the `Inspector` class:

```python
# Module level, after existing constants
_BUILTIN_JOB_DEFAULTS: dict[str, dict] = {
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

Method inside `Inspector` class:

```python
    def _ensure_builtin_jobs(self) -> int:
        """Register any missing built-in jobs in HEARTBEAT.md."""
        task_list = self.routine_manager.load_task_list()
        if task_list is None:
            return 0
        existing_jobs = {t.job for t in task_list.tasks if t.job and t.enabled is not False}
        registered = 0
        for job_name, defaults in _BUILTIN_JOB_DEFAULTS.items():
            if job_name in existing_jobs:
                continue
            try:
                self.routine_manager.add_routine(job=job_name, **defaults)
                registered += 1
                logger.info("Auto-registered built-in job: %s", job_name)
            except ValueError as exc:
                logger.debug("Skipping built-in job %s: %s", job_name, exc)
        return registered
```

- [ ] **Step 4: Add call site in inspect()**

In `inspector.py`, at line 604 (before `ctx = self._gather_context(...)`), add:

```python
        # Ensure built-in jobs are registered before gathering context
        self._ensure_builtin_jobs()
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/unit/test_inspector.py::TestEnsureBuiltinJobs -v
```

Expected: 4 passed

- [ ] **Step 6: Run full test suite**

```bash
pytest tests/unit/ -q --tb=line
```

Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add src/everbot/core/runtime/inspector.py tests/unit/test_inspector.py
git commit -m "feat(inspector): auto-register missing built-in jobs on inspection"
```

---

### Task 3: Defensive import error in cron.py

**Files:**
- Modify: `src/everbot/core/runtime/cron.py:558-559`
- Test: `tests/unit/test_cron_executor.py`

- [ ] **Step 1: Add imports and write failing test**

First, ensure these imports exist at the top of `tests/unit/test_cron_executor.py`:

```python
from unittest.mock import patch
from src.everbot.core.tasks.execution_gate import GateVerdict
```

Then add the test:

```python
class TestJobImportError:
    @pytest.mark.asyncio
    async def test_import_failure_gives_actionable_error(self, tmp_path):
        mgr = _seed_task(
            tmp_path,
            title="Bad import job",
            job="health-check",  # valid job name, but we'll mock the import to fail
            scanner="session",
        )
        executor = _make_executor(tmp_path, routine_manager=mgr)
        task_list = mgr.load_task_list()

        with patch(
            "src.everbot.core.runtime.cron.TaskExecutionGate.check",
            return_value=GateVerdict(allowed=True),
        ), patch(
            "importlib.import_module",
            side_effect=ModuleNotFoundError("No module named 'everbot'"),
        ):
            result = await executor.tick(
                task_list,
                run_agent=AsyncMock(),
                inject_context=AsyncMock(),
                include_isolated=False,
            )

        assert result.failed == 1
        assert "Cannot import job module" in result.results[0].error
        assert "project root" in result.results[0].error
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/unit/test_cron_executor.py::TestJobImportError -v
```

Expected: FAIL — error message doesn't contain "Cannot import job module"

- [ ] **Step 3: Implement defensive import**

In `src/everbot/core/runtime/cron.py`, replace lines 558-559:

```python
            _pkg = __name__.rsplit(".", 2)[0]
            try:
                job_module = importlib.import_module(f"{_pkg}.jobs.{module_name}")
            except ModuleNotFoundError as e:
                raise RuntimeError(
                    f"Cannot import job module '{module_name}': {e}. "
                    f"Ensure daemon runs from project root or package is installed."
                ) from e
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/unit/test_cron_executor.py::TestJobImportError -v
```

Expected: PASS

- [ ] **Step 6: Run full test suite**

```bash
pytest tests/unit/ -q --tb=line
```

Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add src/everbot/core/runtime/cron.py tests/unit/test_cron_executor.py
git commit -m "fix(cron): add actionable error message when job module import fails"
```

---

### Task 4: Delete stale plan file

**Files:**
- Delete: `docs/superpowers/plans/2026-03-17-skill-consolidation.md`

- [ ] **Step 1: Delete the file**

```bash
git rm docs/superpowers/plans/2026-03-17-skill-consolidation.md
```

- [ ] **Step 2: Commit**

```bash
git commit -m "chore: remove completed skill-consolidation plan"
```

---

### Task 5: Integration verification

- [ ] **Step 1: Run full unit test suite**

```bash
pytest tests/unit/ -q --tb=short
```

Expected: all pass, 0 failures

- [ ] **Step 2: Verify HEARTBEAT.md gets populated after daemon restart**

```bash
python -m src.everbot.cli stop && python -m src.everbot.cli start
```

Wait for one inspection cycle (~10 min), then check:

```bash
grep "memory-review\|task-discover" ~/.alfred/agents/demo_agent/HEARTBEAT.md
```

Expected: both job tasks present in HEARTBEAT.md
