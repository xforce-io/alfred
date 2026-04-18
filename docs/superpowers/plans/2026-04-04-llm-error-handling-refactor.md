# LLM Error Handling Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move LLM error classification from per-job ad-hoc string matching to framework-level typed exceptions, ensuring watermark never advances when LLM is unavailable.

**Architecture:** Define `LLMTransientError` / `LLMConfigError` in a shared module. `_SkillLLMClient` classifies raw exceptions into these types. `_invoke_job` catches them and returns a degraded result (no raise, no watermark advance). Jobs stop catching LLM errors internally — exceptions bubble up naturally.

**Tech Stack:** Python asyncio, OpenAI SDK exceptions, pytest + AsyncMock

---

### Task 1: Define LLM Exception Types

**Files:**
- Create: `src/everbot/core/jobs/llm_errors.py`
- Test: `tests/unit/test_llm_errors.py`

- [ ] **Step 1: Write the test**

```python
# tests/unit/test_llm_errors.py
"""Tests for LLM error classification."""

from src.everbot.core.jobs.llm_errors import LLMTransientError, LLMConfigError


class TestLLMErrors:
    def test_transient_error_is_exception(self):
        err = LLMTransientError("connection refused")
        assert isinstance(err, Exception)
        assert str(err) == "connection refused"

    def test_config_error_is_exception(self):
        err = LLMConfigError("model not found")
        assert isinstance(err, Exception)
        assert str(err) == "model not found"

    def test_transient_and_config_are_distinct(self):
        """Framework must be able to catch them separately."""
        transient = LLMTransientError("timeout")
        config = LLMConfigError("bad key")
        assert not isinstance(transient, LLMConfigError)
        assert not isinstance(config, LLMTransientError)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_llm_errors.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.everbot.core.jobs.llm_errors'`

- [ ] **Step 3: Write the implementation**

```python
# src/everbot/core/jobs/llm_errors.py
"""Typed exceptions for LLM call failures in skill jobs.

Used by _SkillLLMClient to classify raw exceptions, and by
_invoke_job to decide whether to advance watermark.
"""


class LLMTransientError(Exception):
    """LLM temporarily unavailable: connection failure, timeout, rate limit, 5xx.

    Safe to retry on next scheduled run.
    """


class LLMConfigError(Exception):
    """LLM configuration problem: model not found, auth failure, missing dependency.

    Requires manual intervention to fix.
    """
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_llm_errors.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/everbot/core/jobs/llm_errors.py tests/unit/test_llm_errors.py
git commit -m "feat(jobs): add LLMTransientError and LLMConfigError exception types"
```

---

### Task 2: Add Exception Classification to `_SkillLLMClient`

**Files:**
- Modify: `src/everbot/core/runtime/heartbeat.py:1631-1688` (`_SkillLLMClient.complete()`)
- Test: `tests/unit/test_self_reflection.py` (add to `TestSkillLLMClient` class)

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_self_reflection.py` inside `class TestSkillLLMClient`:

```python
    @pytest.mark.asyncio
    async def test_connection_error_raises_transient(self):
        """Connection failures should raise LLMTransientError."""
        from src.everbot.core.runtime.heartbeat import _SkillLLMClient
        from src.everbot.core.jobs.llm_errors import LLMTransientError
        import unittest.mock as um

        client = _SkillLLMClient(model="test-model")

        fake_config = MagicMock()
        fake_model_cfg = MagicMock()
        fake_model_cfg.effective_api = "https://fake.example.com/v1"
        fake_model_cfg.api_key = "fake-key"
        fake_model_cfg.model_name = "test-model"
        fake_model_cfg.max_tokens = 2000
        fake_model_cfg.effective_headers = {}
        fake_config.get_model_config.return_value = fake_model_cfg

        with um.patch(
            "src.everbot.core.runtime.heartbeat._get_skill_global_config",
            return_value=fake_config,
        ), um.patch(
            "src.everbot.core.runtime.heartbeat.AsyncOpenAI",
        ) as mock_openai_cls:
            mock_client = AsyncMock()
            mock_client.chat.completions.create.side_effect = ConnectionError("peer reset")
            mock_openai_cls.return_value = mock_client

            with pytest.raises(LLMTransientError, match="peer reset"):
                await client.complete("hello")

    @pytest.mark.asyncio
    async def test_timeout_error_raises_transient(self):
        """Timeout errors should raise LLMTransientError."""
        from src.everbot.core.runtime.heartbeat import _SkillLLMClient
        from src.everbot.core.jobs.llm_errors import LLMTransientError
        import unittest.mock as um

        client = _SkillLLMClient(model="test-model")

        fake_config = MagicMock()
        fake_model_cfg = MagicMock()
        fake_model_cfg.effective_api = "https://fake.example.com/v1"
        fake_model_cfg.api_key = "fake-key"
        fake_model_cfg.model_name = "test-model"
        fake_model_cfg.max_tokens = 2000
        fake_model_cfg.effective_headers = {}
        fake_config.get_model_config.return_value = fake_model_cfg

        with um.patch(
            "src.everbot.core.runtime.heartbeat._get_skill_global_config",
            return_value=fake_config,
        ), um.patch(
            "src.everbot.core.runtime.heartbeat.AsyncOpenAI",
        ) as mock_openai_cls:
            mock_client = AsyncMock()
            mock_client.chat.completions.create.side_effect = TimeoutError("request timed out")
            mock_openai_cls.return_value = mock_client

            with pytest.raises(LLMTransientError, match="request timed out"):
                await client.complete("hello")

    @pytest.mark.asyncio
    async def test_openai_api_connection_error_raises_transient(self):
        """OpenAI SDK APIConnectionError should raise LLMTransientError."""
        from src.everbot.core.runtime.heartbeat import _SkillLLMClient
        from src.everbot.core.jobs.llm_errors import LLMTransientError
        from openai import APIConnectionError
        import unittest.mock as um

        client = _SkillLLMClient(model="test-model")

        fake_config = MagicMock()
        fake_model_cfg = MagicMock()
        fake_model_cfg.effective_api = "https://fake.example.com/v1"
        fake_model_cfg.api_key = "fake-key"
        fake_model_cfg.model_name = "test-model"
        fake_model_cfg.max_tokens = 2000
        fake_model_cfg.effective_headers = {}
        fake_config.get_model_config.return_value = fake_model_cfg

        with um.patch(
            "src.everbot.core.runtime.heartbeat._get_skill_global_config",
            return_value=fake_config,
        ), um.patch(
            "src.everbot.core.runtime.heartbeat.AsyncOpenAI",
        ) as mock_openai_cls:
            mock_client = AsyncMock()
            mock_client.chat.completions.create.side_effect = APIConnectionError(
                request=MagicMock()
            )
            mock_openai_cls.return_value = mock_client

            with pytest.raises(LLMTransientError):
                await client.complete("hello")

    @pytest.mark.asyncio
    async def test_openai_auth_error_raises_config(self):
        """OpenAI SDK AuthenticationError should raise LLMConfigError."""
        from src.everbot.core.runtime.heartbeat import _SkillLLMClient
        from src.everbot.core.jobs.llm_errors import LLMConfigError
        from openai import AuthenticationError
        import unittest.mock as um

        client = _SkillLLMClient(model="test-model")

        fake_config = MagicMock()
        fake_model_cfg = MagicMock()
        fake_model_cfg.effective_api = "https://fake.example.com/v1"
        fake_model_cfg.api_key = "bad-key"
        fake_model_cfg.model_name = "test-model"
        fake_model_cfg.max_tokens = 2000
        fake_model_cfg.effective_headers = {}
        fake_config.get_model_config.return_value = fake_model_cfg

        with um.patch(
            "src.everbot.core.runtime.heartbeat._get_skill_global_config",
            return_value=fake_config,
        ), um.patch(
            "src.everbot.core.runtime.heartbeat.AsyncOpenAI",
        ) as mock_openai_cls:
            mock_client = AsyncMock()
            mock_client.chat.completions.create.side_effect = AuthenticationError(
                message="invalid api key",
                response=MagicMock(status_code=401),
                body=None,
            )
            mock_openai_cls.return_value = mock_client

            with pytest.raises(LLMConfigError, match="invalid api key"):
                await client.complete("hello")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_self_reflection.py::TestSkillLLMClient -v`
Expected: 4 new tests FAIL (exceptions not wrapped yet)

- [ ] **Step 3: Implement exception classification in `_SkillLLMClient.complete()`**

In `src/everbot/core/runtime/heartbeat.py`, modify the `_SkillLLMClient.complete()` method.

Add import at the top of the `complete` method body (deferred import to avoid circular):
```python
from ..jobs.llm_errors import LLMTransientError, LLMConfigError
```

Replace the bare `response = await client.chat.completions.create(...)` / `return` block (lines 1683-1688) with:

```python
        try:
            response = await client.chat.completions.create(
                model=model_cfg.model_name,
                messages=messages,
                **call_kwargs,
            )
            return response.choices[0].message.content or ""
        except (ConnectionError, TimeoutError, OSError) as e:
            raise LLMTransientError(str(e)) from e
        except Exception as e:
            # Classify OpenAI SDK exceptions by type name to avoid
            # hard import dependency on every OpenAI exception class.
            type_name = type(e).__name__
            if type_name in (
                "APIConnectionError", "APITimeoutError",
                "RateLimitError", "InternalServerError",
            ):
                raise LLMTransientError(str(e)) from e
            if type_name in (
                "AuthenticationError", "NotFoundError",
                "PermissionDeniedError",
            ):
                raise LLMConfigError(str(e)) from e
            # Also check for legacy string markers (model not in config, etc.)
            msg = str(e).lower()
            if any(m in msg for m in (
                "not found in configuration",
                "no module named 'openai'",
            )):
                raise LLMConfigError(str(e)) from e
            # Unknown errors: treat as transient (safer — won't advance watermark)
            raise LLMTransientError(str(e)) from e
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_self_reflection.py::TestSkillLLMClient -v`
Expected: 6 passed (2 existing + 4 new)

- [ ] **Step 5: Run full test suite to check nothing broke**

Run: `python -m pytest tests/unit/test_self_reflection.py tests/unit/test_cron_executor.py tests/unit/test_slm_judge.py -v`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add src/everbot/core/runtime/heartbeat.py tests/unit/test_self_reflection.py
git commit -m "feat(llm): classify exceptions in _SkillLLMClient into LLMTransientError/LLMConfigError"
```

---

### Task 3: Framework-Level LLM Error Handling in `_invoke_job`

**Files:**
- Modify: `src/everbot/core/runtime/cron.py:675-713` (`_invoke_job`)
- Test: `tests/unit/test_cron_executor.py` (add new test class)

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_cron_executor.py`:

```python
from src.everbot.core.jobs.llm_errors import LLMTransientError, LLMConfigError


class TestInvokeJobLLMErrorHandling:
    """_invoke_job should catch LLM errors and return degraded result, not raise."""

    @pytest.mark.asyncio
    async def test_transient_error_returns_degraded_not_raises(self, tmp_path):
        """LLMTransientError from job module should not propagate as exception."""
        mgr = _seed_task(
            tmp_path,
            title="Memory Review",
            job="memory-review",
        )
        executor = _make_executor(tmp_path, routine_manager=mgr)
        task_list = mgr.load_task_list()
        task = task_list.tasks[0]

        with patch(
            "importlib.import_module",
        ) as mock_import:
            mock_module = MagicMock()
            mock_module.run = AsyncMock(side_effect=LLMTransientError("Connection error"))
            mock_import.return_value = mock_module

            # Should NOT raise — returns degraded string
            result = await executor._invoke_job(task, None, "test_run")

        assert "LLM unavailable" in result
        assert "Connection error" in result

    @pytest.mark.asyncio
    async def test_config_error_returns_degraded_not_raises(self, tmp_path):
        """LLMConfigError from job module should not propagate as exception."""
        mgr = _seed_task(
            tmp_path,
            title="Memory Review",
            job="memory-review",
        )
        executor = _make_executor(tmp_path, routine_manager=mgr)
        task_list = mgr.load_task_list()
        task = task_list.tasks[0]

        with patch(
            "importlib.import_module",
        ) as mock_import:
            mock_module = MagicMock()
            mock_module.run = AsyncMock(side_effect=LLMConfigError("model not found"))
            mock_import.return_value = mock_module

            result = await executor._invoke_job(task, None, "test_run")

        assert "LLM unavailable" in result

    @pytest.mark.asyncio
    async def test_non_llm_error_still_raises(self, tmp_path):
        """Non-LLM exceptions should still propagate normally."""
        mgr = _seed_task(
            tmp_path,
            title="Memory Review",
            job="memory-review",
        )
        executor = _make_executor(tmp_path, routine_manager=mgr)
        task_list = mgr.load_task_list()
        task = task_list.tasks[0]

        with patch(
            "importlib.import_module",
        ) as mock_import:
            mock_module = MagicMock()
            mock_module.run = AsyncMock(side_effect=ValueError("bad data"))
            mock_import.return_value = mock_module

            with pytest.raises(ValueError, match="bad data"):
                await executor._invoke_job(task, None, "test_run")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_cron_executor.py::TestInvokeJobLLMErrorHandling -v`
Expected: 2 FAIL (LLM errors still raise), 1 PASS (non-LLM error)

- [ ] **Step 3: Implement LLM error catch in `_invoke_job`**

In `src/everbot/core/runtime/cron.py`, modify `_invoke_job` (around line 675). Add the new except clause between the `result = await job_module.run(context)` try block and the existing `except Exception` handler:

```python
    async def _invoke_job(self, task: Task, scan_result: Any, run_id: str) -> str:
        """Execute a cron job task."""
        from ..jobs.llm_errors import LLMTransientError, LLMConfigError

        job_name = task.job
        start_ms = int(_time.time() * 1000)

        self._write_event(
            "job_started", skill=job_name,
            scan_summary=scan_result.change_summary if scan_result else "",
        )

        try:
            context = self._build_job_context(scan_result)
            module_name = job_name.replace("-", "_")
            if module_name not in ALLOWED_JOBS:
                raise ValueError(f"Job {job_name!r} is not in the allowed jobs whitelist")
            _pkg = __name__.rsplit(".", 2)[0]
            try:
                job_module = importlib.import_module(f"{_pkg}.jobs.{module_name}")
            except ModuleNotFoundError as e:
                raise RuntimeError(
                    f"Cannot import job module '{module_name}': {e}. "
                    f"Ensure daemon runs from project root or package is installed."
                ) from e
            result = await job_module.run(context)

            duration_ms = int(_time.time() * 1000) - start_ms
            self._write_event(
                "job_completed", skill=job_name,
                duration_ms=duration_ms, result=str(result)[:200],
            )
            return str(result)
        except (LLMTransientError, LLMConfigError) as exc:
            duration_ms = int(_time.time() * 1000) - start_ms
            self._write_event(
                "job_degraded", skill=job_name,
                duration_ms=duration_ms, error=str(exc)[:200],
                retriable=isinstance(exc, LLMTransientError),
            )
            logger.warning("Job %s skipped (LLM unavailable): %s", job_name, exc)
            return f"LLM unavailable: {exc}"
        except Exception as exc:
            duration_ms = int(_time.time() * 1000) - start_ms
            self._write_event(
                "job_failed", skill=job_name,
                duration_ms=duration_ms, error=str(exc)[:200],
            )
            logger.error("Job %s failed: %s", job_name, exc, exc_info=True)
            raise
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_cron_executor.py::TestInvokeJobLLMErrorHandling -v`
Expected: 3 passed

- [ ] **Step 5: Suppress Telegram push for degraded results**

In `src/everbot/core/runtime/cron.py`, update the push suppression in `_run_isolated_job` (around line 569). Change:

```python
            if not result.startswith("Evaluated 0/"):
                await self.delivery._emit_realtime(result, run_id)
```

to:

```python
            if not result.startswith(("Evaluated 0/", "LLM unavailable:")):
                await self.delivery._emit_realtime(result, run_id)
```

- [ ] **Step 6: Run full cron executor tests**

Run: `python -m pytest tests/unit/test_cron_executor.py -v`
Expected: All pass

- [ ] **Step 7: Commit**

```bash
git add src/everbot/core/runtime/cron.py tests/unit/test_cron_executor.py
git commit -m "feat(cron): catch LLM errors in _invoke_job, return degraded result without raising"
```

---

### Task 4: Simplify `memory_review.py` — Remove Ad-Hoc Error Handling

**Files:**
- Modify: `src/everbot/core/jobs/memory_review.py:18-218`
- Test: `tests/unit/test_self_reflection.py` (update existing tests)

- [ ] **Step 1: Write new test for connection error + watermark protection**

Add to `tests/unit/test_self_reflection.py` inside `class TestSkillWithoutScanner`:

```python
    @pytest.mark.asyncio
    async def test_memory_review_connection_error_raises_not_swallowed(self, tmp_path, sessions_dir):
        """Connection errors should propagate (not be swallowed as empty result).
        
        The framework (_invoke_job) catches LLMTransientError and prevents
        watermark advance. The job itself should NOT catch LLM errors.
        """
        from src.everbot.core.jobs.memory_review import run
        from src.everbot.core.runtime.skill_context import SkillContext
        from src.everbot.core.memory.manager import MemoryManager
        from src.everbot.core.memory.models import MemoryEntry
        from src.everbot.core.jobs.llm_errors import LLMTransientError

        self._write_session(sessions_dir, "web_session_test-agent_001", _1D_AGO)

        mm = MemoryManager(tmp_path / "MEMORY.md")
        mm.store.save([
            MemoryEntry(
                id="mem001",
                category="workflow",
                content="User prefers concise answers",
                score=0.8,
                created_at="2026-03-01T00:00:00+00:00",
                last_activated="2026-03-01T00:00:00+00:00",
                activation_count=1,
                source_session="web_session_test-agent_001",
            )
        ])
        mock_llm = AsyncMock()
        mock_llm.complete.side_effect = LLMTransientError("Connection error")

        ctx = SkillContext(
            sessions_dir=sessions_dir,
            workspace_path=tmp_path,
            agent_name="test-agent",
            memory_manager=mm,
            mailbox=AsyncMock(),
            llm=mock_llm,
            scan_result=None,
        )

        with pytest.raises(LLMTransientError, match="Connection error"):
            await run(ctx)

        # Watermark must NOT be advanced (run() never reached watermark code)
        from src.everbot.core.scanners.reflection_state import ReflectionState
        state_after = ReflectionState.load(tmp_path)
        assert not state_after.get_watermark("memory-review")
```

- [ ] **Step 2: Run new test to verify it fails**

Run: `python -m pytest tests/unit/test_self_reflection.py::TestSkillWithoutScanner::test_memory_review_connection_error_raises_not_swallowed -v`
Expected: FAIL — connection error is currently swallowed, returns string instead of raising

- [ ] **Step 3: Simplify `memory_review.py`**

In `src/everbot/core/jobs/memory_review.py`:

**Delete** `_is_missing_skill_llm` function (lines 18-25) and `_SkipResult` class (lines 28-38).

**Simplify** `_analyze_memory_consolidation` (around line 112). Remove the try/except around the LLM call. Keep the `if not existing_entries: return {}` early return. The function becomes:

```python
async def _analyze_memory_consolidation(llm, digests: List[str], existing_entries) -> dict:
    """Analyze memory entries for consolidation opportunities.

    Returns dict with: merge_pairs, deprecate_ids, reinforce_ids, refined_entries.
    LLM errors propagate to caller (framework handles them).
    """
    if not existing_entries:
        return {}

    existing_text = "\n".join(
        f"- [{e.id}] [{e.category}] (score={e.score:.2f}, count={e.activation_count}) {e.content}"
        for e in existing_entries
    )
    context_text = "\n".join(d[:500] for d in digests[:3])

    from pathlib import Path

    dph_path = Path(__file__).parent / "system_dphs" / "memory_review_consolidation.dph"
    dph_data = parse_system_dph(str(dph_path), {
        "existing_text": existing_text,
        "context_text": context_text,
    })
    sys_prompt = dph_data["config"].pop("system_prompt", "")
    model_override = dph_data["config"].pop("model", "")

    response = await llm.complete(
        dph_data["prompt"],
        system=sys_prompt,
        model_override=model_override,
        **dph_data["config"]
    )
    result = parse_json_response(response)

    # Validate entropy constraint
    merge_count = len(result.get("merge_pairs", []))
    deprecate_count = len(result.get("deprecate_ids", []))
    reinforce_count = len(result.get("reinforce_ids", []))
    if merge_count + deprecate_count < reinforce_count:
        logger.warning(
            "Entropy constraint violated: merge=%d + deprecate=%d < reinforce=%d, trimming reinforcements",
            merge_count, deprecate_count, reinforce_count,
        )
        allowed = merge_count + deprecate_count
        result["reinforce_ids"] = result.get("reinforce_ids", [])[:allowed]

    return result
```

**Simplify** `_compress_to_user_profile` (around line 167). Remove try/except. The function becomes:

```python
async def _compress_to_user_profile(context: SkillContext) -> str:
    """Compress all memory entries into structured tags and write to USER.md.

    LLM errors propagate to caller (framework handles them).
    """
    entries = context.memory_manager.load_entries()
    if not entries:
        return "no entries"

    active = [e for e in entries if e.score >= 0.5]
    if not active:
        return "no active entries"

    entries_text = "\n".join(
        f"- [{e.category}] {e.content}" for e in active
    )

    from pathlib import Path

    dph_path = Path(__file__).parent / "system_dphs" / "memory_review_compression.dph"
    dph_data = parse_system_dph(str(dph_path), {
        "entries_text": entries_text,
    })
    sys_prompt = dph_data["config"].pop("system_prompt", "")
    model_override = dph_data["config"].pop("model", "")

    response = await context.llm.complete(
        dph_data["prompt"],
        system=sys_prompt,
        model_override=model_override,
        **dph_data["config"]
    )
    profile_content = response.strip()

    user_md_path = context.workspace_path / "USER.md"
    user_md_path.write_text(
        f"# 用户画像\n\n{profile_content}\n",
        encoding="utf-8",
    )
    logger.info("Compressed %d memory entries to USER.md", len(active))
    return f"compressed {len(active)} entries"
```

**Simplify** `run()` watermark section (around line 100). Remove the `_SkipResult` check and the `isinstance` guard. Replace:

```python
    # If LLM/deps were unavailable, do not apply changes or advance the watermark.
    # The sessions remain eligible for re-review once the environment recovers.
    if isinstance(review, _SkipResult):
        return f"Memory review skipped: {review}, profile: skipped"
```

with nothing (delete these lines). And replace:

```python
    # 6. Advance watermark only when both steps completed successfully.
    # A _SkipResult means the LLM/deps were unavailable; keep sessions eligible
    # for re-review so they are not permanently lost.
    llm_unavailable = isinstance(compress_result, _SkipResult)
    if last_successful_session and not llm_unavailable:
        state.set_watermark("memory-review", last_successful_session.updated_at)
        state.save(context.workspace_path)

    compress_str = str(compress_result)
    return f"Memory review: {review_stats}, profile: {compress_str}"
```

with:

```python
    # 6. Advance watermark — if we got here, both LLM calls succeeded.
    # LLM errors propagate as exceptions, so this code only runs on success.
    if last_successful_session:
        state.set_watermark("memory-review", last_successful_session.updated_at)
        state.save(context.workspace_path)

    return f"Memory review: {review_stats}, profile: {compress_result}"
```

- [ ] **Step 4: Update existing test for the new behavior**

In `tests/unit/test_self_reflection.py`, update `test_memory_review_skips_when_skill_llm_unavailable`. The old test expects a string return with "skipped". Now LLM errors propagate as exceptions. The `_SkillLLMClient` wraps `RuntimeError("litellm is required ...")` as `LLMConfigError`. But in the test, the mock LLM raises `RuntimeError` directly (not going through `_SkillLLMClient`). Update the test to raise `LLMConfigError` directly:

```python
    @pytest.mark.asyncio
    async def test_memory_review_skips_when_skill_llm_unavailable(self, tmp_path, sessions_dir):
        """memory_review should raise LLMConfigError when skill LLM is unavailable."""
        from src.everbot.core.jobs.memory_review import run
        from src.everbot.core.runtime.skill_context import SkillContext
        from src.everbot.core.memory.manager import MemoryManager
        from src.everbot.core.memory.models import MemoryEntry
        from src.everbot.core.jobs.llm_errors import LLMConfigError

        self._write_session(sessions_dir, "web_session_test-agent_001", _1D_AGO)

        mm = MemoryManager(tmp_path / "MEMORY.md")
        mm.store.save([
            MemoryEntry(
                id="mem001",
                category="workflow",
                content="User prefers concise answers",
                score=0.8,
                created_at="2026-03-01T00:00:00+00:00",
                last_activated="2026-03-01T00:00:00+00:00",
                activation_count=1,
                source_session="web_session_test-agent_001",
            )
        ])
        mock_llm = AsyncMock()
        mock_llm.complete.side_effect = LLMConfigError(
            "litellm is required for skill LLM calls"
        )

        ctx = SkillContext(
            sessions_dir=sessions_dir,
            workspace_path=tmp_path,
            agent_name="test-agent",
            memory_manager=mm,
            mailbox=AsyncMock(),
            llm=mock_llm,
            scan_result=None,
        )

        with pytest.raises(LLMConfigError):
            await run(ctx)

        # Watermark must NOT be advanced
        from src.everbot.core.scanners.reflection_state import ReflectionState
        state_after = ReflectionState.load(tmp_path)
        assert not state_after.get_watermark("memory-review"), (
            "watermark must not advance when skill LLM is unavailable"
        )
```

Also update `test_memory_review_dph_file_missing_does_not_advance_watermark` — `FileNotFoundError` from `parse_system_dph` now propagates directly (no longer caught by the job). Update to expect the exception:

```python
    @pytest.mark.asyncio
    async def test_memory_review_dph_file_missing_does_not_advance_watermark(
        self, tmp_path, sessions_dir
    ):
        """dph 文件缺失时 FileNotFoundError 应向上冒泡，不推进 watermark。"""
        from unittest.mock import patch
        from src.everbot.core.jobs.memory_review import run
        from src.everbot.core.runtime.skill_context import SkillContext
        from src.everbot.core.memory.manager import MemoryManager
        from src.everbot.core.memory.models import MemoryEntry
        from src.everbot.core.scanners.reflection_state import ReflectionState

        self._write_session(sessions_dir, "web_session_test-agent_001", _1D_AGO)

        mm = MemoryManager(tmp_path / "MEMORY.md")
        mm.store.save([
            MemoryEntry(
                id="mem002",
                category="workflow",
                content="User likes TDD",
                score=0.9,
                created_at="2026-03-01T00:00:00+00:00",
                last_activated="2026-03-01T00:00:00+00:00",
                activation_count=1,
                source_session="web_session_test-agent_001",
            )
        ])
        mock_llm = AsyncMock()

        ctx = SkillContext(
            sessions_dir=sessions_dir,
            workspace_path=tmp_path,
            agent_name="test-agent",
            memory_manager=mm,
            mailbox=AsyncMock(),
            llm=mock_llm,
            scan_result=None,
        )

        with patch(
            "src.everbot.core.jobs.memory_review.parse_system_dph",
            side_effect=FileNotFoundError("no such file"),
        ):
            with pytest.raises(FileNotFoundError):
                await run(ctx)

        # Watermark must NOT advance
        state_after = ReflectionState.load(tmp_path)
        assert not state_after.get_watermark("memory-review"), (
            "watermark must not advance when dph file is missing"
        )
```

- [ ] **Step 5: Run all memory review tests**

Run: `python -m pytest tests/unit/test_self_reflection.py::TestSkillWithoutScanner -v`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add src/everbot/core/jobs/memory_review.py tests/unit/test_self_reflection.py
git commit -m "refactor(memory-review): remove _SkipResult/_is_missing_skill_llm, let LLM errors propagate"
```

---

### Task 5: Simplify `task_discover.py` — Remove Error Swallowing

**Files:**
- Modify: `src/everbot/core/jobs/task_discover.py:150-155,192-211`
- Test: `tests/unit/test_self_reflection.py` (add new test)

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_self_reflection.py` inside `class TestSkillWithoutScanner`:

```python
    @pytest.mark.asyncio
    async def test_task_discover_llm_error_raises_not_swallowed(self, tmp_path, sessions_dir):
        """task_discover should let LLM errors propagate, not swallow them."""
        from src.everbot.core.jobs.task_discover import run
        from src.everbot.core.runtime.skill_context import SkillContext
        from src.everbot.core.memory.manager import MemoryManager
        from src.everbot.core.jobs.llm_errors import LLMTransientError

        self._write_session(sessions_dir, "web_session_test-agent_001", _1D_AGO)

        mm = MemoryManager(tmp_path / "MEMORY.md")
        mock_llm = AsyncMock()
        mock_llm.complete.side_effect = LLMTransientError("Connection refused")

        ctx = SkillContext(
            sessions_dir=sessions_dir,
            workspace_path=tmp_path,
            agent_name="test-agent",
            memory_manager=mm,
            mailbox=AsyncMock(),
            llm=mock_llm,
            scan_result=None,
        )

        with pytest.raises(LLMTransientError, match="Connection refused"):
            await run(ctx)

        # Watermark must NOT be advanced
        from src.everbot.core.scanners.reflection_state import ReflectionState
        state_after = ReflectionState.load(tmp_path)
        assert not state_after.get_watermark("task-discover")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_self_reflection.py::TestSkillWithoutScanner::test_task_discover_llm_error_raises_not_swallowed -v`
Expected: FAIL — currently swallows exception and returns "Discovered 0 tasks"

- [ ] **Step 3: Simplify `task_discover.py`**

In `src/everbot/core/jobs/task_discover.py`, modify `_discover_tasks` (around line 158). Remove the try/except around the LLM call:

```python
async def _discover_tasks(llm, digests: List[str], existing_titles: List[str]) -> List[DiscoveredTask]:
    """Use LLM to discover actionable tasks from session digests.

    LLM errors propagate to caller (framework handles them).
    """
    context_text = "\n".join(d[:800] for d in digests[:5])
    existing_text = "\n".join(f"- {t}" for t in existing_titles) if existing_titles else "(none)"

    prompt = f"""Analyze these recent conversations and identify actionable tasks that the user mentioned but hasn't completed.

## Recent Conversations
{context_text}

## Already Tracked Tasks
{existing_text}

## Rules
- Only identify tasks the user explicitly mentioned wanting to do
- Skip tasks that seem already completed in the conversations
- Skip vague wishes — only include actionable, specific tasks
- Do NOT duplicate already tracked tasks
- Maximum 3 new tasks

Output format:
```json
{{
  "tasks": [
    {{
      "title": "Short task title",
      "description": "What needs to be done",
      "urgency": "high|medium|low",
      "source_hint": "Brief reference to the conversation"
    }}
  ]
}}
```"""

    response = await llm.complete(prompt, system="You are a task discovery engine. Output valid JSON only.")
    result = parse_json_response(response)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=_EXPIRE_DAYS)

    tasks = []
    for item in result.get("tasks", [])[:3]:
        tasks.append(DiscoveredTask(
            title=item.get("title", ""),
            description=item.get("description", ""),
            urgency=item.get("urgency", "medium"),
            source_session_id="",
            discovered_at=now.isoformat(),
            expires_at=expires.isoformat(),
        ))
    return tasks
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_self_reflection.py::TestSkillWithoutScanner::test_task_discover_llm_error_raises_not_swallowed -v`
Expected: PASS

- [ ] **Step 5: Run all self_reflection tests**

Run: `python -m pytest tests/unit/test_self_reflection.py -v`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add src/everbot/core/jobs/task_discover.py tests/unit/test_self_reflection.py
git commit -m "refactor(task-discover): let LLM errors propagate instead of swallowing"
```

---

### Task 6: Fix `judge.py` and `skill_evaluate.py` — LLM Errors Propagate

**Files:**
- Modify: `src/everbot/core/slm/judge.py:58-95` (`judge_segments`)
- Modify: `src/everbot/core/jobs/skill_evaluate.py:36-54` (evaluation loop)
- Test: `tests/unit/test_slm_judge.py` (update `test_error_handling`)

- [ ] **Step 1: Update judge test for new behavior**

In `tests/unit/test_slm_judge.py`, update `test_error_handling` and add a new test. Replace the existing test:

```python
    @pytest.mark.asyncio
    async def test_llm_error_propagates(self):
        """LLM errors should propagate, not be swallowed with default scores."""
        from src.everbot.core.jobs.llm_errors import LLMTransientError

        class FailingLLM:
            async def complete(self, prompt: str, system: str = "") -> str:
                raise LLMTransientError("Connection refused")

        with pytest.raises(LLMTransientError, match="Connection refused"):
            await judge_segments(FailingLLM(), [_make_segment()])

    @pytest.mark.asyncio
    async def test_parse_error_returns_neutral_scores(self):
        """Non-LLM errors (like JSON parse failures) should return neutral scores."""
        llm = MockLLM("this is not valid json at all")
        results = await judge_segments(llm, [_make_segment()])
        assert len(results) == 1
        assert results[0].satisfaction == 0.5
        assert "error" in results[0].reason.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_slm_judge.py -v`
Expected: `test_llm_error_propagates` FAIL (currently catches all exceptions)

- [ ] **Step 3: Update `judge_segments` to only catch non-LLM exceptions**

In `src/everbot/core/slm/judge.py`, modify `judge_segments` (around line 58):

```python
async def judge_segments(
    llm: LLMClient,
    segments: List[EvaluationSegment],
) -> List[JudgeResult]:
    """Score all segments in a single LLM call.

    Returns results in the same order as input segments, with segment_index set.
    LLM errors (LLMTransientError, LLMConfigError) propagate to caller.
    Parse errors return neutral 0.5 scores.
    """
    if not segments:
        return []

    from ..jobs.llm_errors import LLMTransientError, LLMConfigError

    prompt = _BATCH_JUDGE_PROMPT.format(
        segments_block=_build_segments_block(segments),
    )
    try:
        response = await llm.complete(prompt, system=_JUDGE_SYSTEM)
    except (LLMTransientError, LLMConfigError):
        raise  # Let framework handle LLM unavailability
    except Exception as e:
        logger.warning("Batch judge failed: %s", e)
        return [
            JudgeResult(
                segment_index=i,
                has_critical_issue=False,
                satisfaction=0.5,
                reason=f"Judge error: {e}",
            )
            for i in range(len(segments))
        ]

    try:
        items = _parse_batch_response(response, len(segments))
    except Exception as e:
        logger.warning("Batch judge parse failed: %s", e)
        return [
            JudgeResult(
                segment_index=i,
                has_critical_issue=False,
                satisfaction=0.5,
                reason=f"Parse error: {e}",
            )
            for i in range(len(segments))
        ]

    results: List[JudgeResult] = []
    for i, data in enumerate(items):
        results.append(JudgeResult(
            segment_index=i,
            has_critical_issue=bool(data.get("has_critical_issue", False)),
            satisfaction=max(0.0, min(1.0, float(data.get("satisfaction", 0.0)))),
            reason=str(data.get("reason", "")),
        ))
    return results
```

- [ ] **Step 4: Update `skill_evaluate.py` evaluation loop**

In `src/everbot/core/jobs/skill_evaluate.py`, modify the loop in `run()` (around line 36):

```python
    evaluated = 0
    for skill_id in skill_ids:
        try:
            result = await _evaluate_one(
                context, seg_logger, ver_mgr, skill_id, udm.sessions_dir,
            )
            if result:
                evaluated += 1
                logger.info("Evaluated %s: %s", skill_id, result)
        except (LLMTransientError, LLMConfigError):
            logger.warning("LLM unavailable during %s evaluation, aborting remaining", skill_id)
            raise  # Propagate to framework — all remaining would fail too
        except Exception as e:
            logger.warning("Failed to evaluate %s: %s", skill_id, e)
```

Add the import at the top of `run()`:

```python
    from .llm_errors import LLMTransientError, LLMConfigError
```

- [ ] **Step 5: Run judge tests**

Run: `python -m pytest tests/unit/test_slm_judge.py -v`
Expected: All pass

- [ ] **Step 6: Run full test suite**

Run: `python -m pytest tests/unit/test_cron_executor.py tests/unit/test_slm_judge.py tests/unit/test_self_reflection.py -v`
Expected: All pass

- [ ] **Step 7: Commit**

```bash
git add src/everbot/core/slm/judge.py src/everbot/core/jobs/skill_evaluate.py tests/unit/test_slm_judge.py
git commit -m "refactor(judge,skill-evaluate): let LLM errors propagate, only catch parse errors"
```

---

### Task 7: Update `health_check.py` Exception Types

**Files:**
- Modify: `src/everbot/core/jobs/health_check.py:182-200`

- [ ] **Step 1: Update the catch clause**

In `src/everbot/core/jobs/health_check.py`, modify `_check_llm` (around line 182). health_check intentionally catches LLM errors to report status — it should catch the new typed exceptions:

```python
async def _check_llm(context: SkillContext) -> CheckResult:
    """Check LLM API availability with a minimal request."""
    from .llm_errors import LLMTransientError, LLMConfigError
    try:
        response = await context.llm.complete("Reply with OK", system="Reply with exactly 'OK'")
        if response and len(response.strip()) > 0:
            return CheckResult(name="llm", ok=True, message="responsive")
        return CheckResult(
            name="llm",
            ok=False,
            message="empty response",
            critical=True,
        )
    except LLMTransientError as e:
        return CheckResult(
            name="llm",
            ok=False,
            message=f"transient: {e}",
            critical=True,
        )
    except LLMConfigError as e:
        return CheckResult(
            name="llm",
            ok=False,
            message=f"config: {e}",
            critical=True,
        )
    except Exception as e:
        return CheckResult(
            name="llm",
            ok=False,
            message=f"unexpected: {e}",
            critical=True,
        )
```

- [ ] **Step 2: Run full test suite**

Run: `python -m pytest tests/unit/test_cron_executor.py tests/unit/test_slm_judge.py tests/unit/test_self_reflection.py tests/unit/test_llm_errors.py -v`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add src/everbot/core/jobs/health_check.py
git commit -m "refactor(health-check): use typed LLM exceptions for status reporting"
```

---

### Task 8: Final Verification

- [ ] **Step 1: Run entire unit test suite**

Run: `python -m pytest tests/unit/ -v --tb=short`
Expected: All pass, no regressions

- [ ] **Step 2: Verify deleted code is gone**

Run: `grep -rn "_SkipResult\|_is_missing_skill_llm" src/everbot/`
Expected: No matches (these patterns should be completely removed)

- [ ] **Step 3: Verify new patterns are in place**

Run: `grep -rn "LLMTransientError\|LLMConfigError" src/everbot/`
Expected: Matches in `llm_errors.py` (definition), `heartbeat.py` (classification), `cron.py` (framework catch), `judge.py` (re-raise), `skill_evaluate.py` (re-raise), `health_check.py` (status report)

- [ ] **Step 4: Final commit if any fixups needed**

```bash
git status
# If clean: done. If fixups needed: stage and commit.
```
