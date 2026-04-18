# Suppress Transient LLM Errors from User Notifications — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop pushing transient LLM infrastructure errors (connection timeouts, incomplete reads, etc.) to users via Telegram — they are noise, not actionable.

**Architecture:** Remove the exception-swallowing in `Inspector.inspect()` so LLM failures propagate as exceptions. `_execute_with_retry` already handles retry. `run_once_with_options` becomes the single decision point: transient errors are logged and silenced; permanent errors are still delivered. Reuse the `_is_transient_llm_error` logic already in `cron.py` by extracting `_TRANSIENT_LLM_ERROR_MARKERS` to a shared location.

**Tech Stack:** Python, pytest, asyncio

---

### Task 1: Extract `_TRANSIENT_LLM_ERROR_MARKERS` to shared module

Both `cron.py` and `heartbeat.py` need transient-error detection. Currently `cron.py` owns the markers and function. Extract to a shared location so both can import.

**Files:**
- Modify: `src/everbot/core/runtime/cron.py:61-90`
- Modify: `src/everbot/core/runtime/heartbeat.py:55-105`

- [ ] **Step 1: Write failing test — `_is_transient_llm_error` importable from heartbeat module**

```python
# tests/unit/test_heartbeat_runner_core.py — add to existing file

def test_is_transient_llm_error_detects_peer_closed():
    """_is_transient_llm_error identifies transport-level failures."""
    from src.everbot.core.runtime.heartbeat import _is_transient_llm_error
    exc = Exception("peer closed connection without sending complete message body (incomplete chunked read)")
    assert _is_transient_llm_error(exc) is True

def test_is_transient_llm_error_rejects_permanent():
    """_is_transient_llm_error does not match permanent errors."""
    from src.everbot.core.runtime.heartbeat import _is_transient_llm_error
    exc = Exception("invalid api key")
    assert _is_transient_llm_error(exc) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_heartbeat_runner_core.py::test_is_transient_llm_error_detects_peer_closed tests/unit/test_heartbeat_runner_core.py::test_is_transient_llm_error_rejects_permanent -v`
Expected: ImportError — `_is_transient_llm_error` not found in heartbeat module.

- [ ] **Step 3: Add `_is_transient_llm_error` and `_TRANSIENT_LLM_ERROR_MARKERS` to `heartbeat.py`**

In `src/everbot/core/runtime/heartbeat.py`, after the existing `_PERMANENT_ERROR_MARKERS` block (after line ~69), add:

```python
_TRANSIENT_LLM_ERROR_MARKERS: tuple[str, ...] = (
    "remoteprotocolerror",
    "apiconnectionerror",
    "apitimeouterror",
    "request timed out",
    "connection error",
    "peer closed connection",
    "incomplete chunked read",
    "server disconnected without sending a response",
)


def _is_transient_llm_error(exc: BaseException) -> bool:
    """Return True for transport-level LLM failures that are not user-actionable."""
    type_name = type(exc).__name__.lower()
    text = str(exc).lower()
    haystack = f"{type_name} {text}"
    return any(marker in haystack for marker in _TRANSIENT_LLM_ERROR_MARKERS)
```

Then update `cron.py` to import from heartbeat instead of defining its own copy:

```python
# In cron.py, replace the local _TRANSIENT_LLM_ERROR_MARKERS and _is_transient_llm_error
# with an import:
from .heartbeat import _TRANSIENT_LLM_ERROR_MARKERS, _is_transient_llm_error
```

Remove the local `_TRANSIENT_LLM_ERROR_MARKERS` (lines 61-70) and `_is_transient_llm_error` (lines 85-90) from `cron.py`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_heartbeat_runner_core.py::test_is_transient_llm_error_detects_peer_closed tests/unit/test_heartbeat_runner_core.py::test_is_transient_llm_error_rejects_permanent -v`
Expected: PASS

Also run existing cron tests to ensure the import doesn't break anything:

Run: `python -m pytest tests/unit/test_cron_executor.py -v`
Expected: All existing tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/everbot/core/runtime/heartbeat.py src/everbot/core/runtime/cron.py tests/unit/test_heartbeat_runner_core.py
git commit -m "refactor: extract _is_transient_llm_error to heartbeat module for shared use"
```

---

### Task 2: Let inspector propagate LLM exceptions instead of swallowing them

The root cause: `Inspector.inspect()` catches all LLM exceptions and converts them to `InspectionResult(output="LLM_ERROR: ...")`. This prevents `_execute_with_retry` from retrying and forces downstream code to parse error strings. Fix: let exceptions propagate.

**Files:**
- Modify: `src/everbot/core/runtime/inspector.py:680-692`
- Modify: `tests/unit/test_inspector.py`

- [ ] **Step 1: Write failing test — inspector propagates LLM exceptions**

```python
# tests/unit/test_inspector.py — add to existing file

@pytest.mark.asyncio
async def test_inspect_propagates_llm_exception(tmp_path: Path):
    """LLM failures during reflection should propagate, not be swallowed."""
    inspector = _make_inspector(
        tmp_path,
        agent_factory=AsyncMock(),
    )
    inspector._run_llm = AsyncMock(
        side_effect=ConnectionError("peer closed connection")
    )
    # Provide minimal context so inspect() reaches the LLM call
    inspector._build_reflect_prompt = MagicMock(return_value="test prompt")
    inspector._gather_context = AsyncMock(return_value=InspectionContext(
        heartbeat_content="test",
        session_summary=None,
        recent_events=[],
    ))

    with pytest.raises(ConnectionError, match="peer closed connection"):
        await inspector.inspect(
            heartbeat_content="test",
            run_id="test_run",
            session_manager=MagicMock(),
            primary_session_id="session_1",
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_inspector.py::test_inspect_propagates_llm_exception -v`
Expected: FAIL — no exception raised, returns `InspectionResult` with `LLM_ERROR:` instead.

- [ ] **Step 3: Remove the exception-swallowing in `Inspector.inspect()`**

In `src/everbot/core/runtime/inspector.py`, replace lines 680-692:

```python
        except Exception as exc:
            logger.warning("LLM reflection failed: %s", exc)
            return InspectionResult(
                heartbeat_ok=False,
                output=f"LLM_ERROR: {exc}",
            )

        if not isinstance(response, str) or not response.strip():
            logger.warning("LLM reflection returned empty response")
            return InspectionResult(
                heartbeat_ok=False,
                output="LLM_ERROR: empty response",
            )
```

With:

```python
        except Exception as exc:
            logger.warning("LLM reflection failed: %s", exc)
            raise

        if not isinstance(response, str) or not response.strip():
            logger.warning("LLM reflection returned empty response")
            raise RuntimeError("LLM reflection returned empty response")
```

- [ ] **Step 4: Run tests to verify the new test passes**

Run: `python -m pytest tests/unit/test_inspector.py::test_inspect_propagates_llm_exception -v`
Expected: PASS

- [ ] **Step 5: Run all inspector tests to check for regressions**

Run: `python -m pytest tests/unit/test_inspector.py -v`
Expected: All pass. Some existing tests may mock at a level that's unaffected. If any test expected `LLM_ERROR` in `output`, update it to expect the exception instead.

- [ ] **Step 6: Commit**

```bash
git add src/everbot/core/runtime/inspector.py tests/unit/test_inspector.py
git commit -m "refactor(inspector): propagate LLM exceptions instead of swallowing into result strings"
```

---

### Task 3: Clean up `_execute_once` structured_reflect path

With inspector now raising exceptions, the `LLM_ERROR` string-matching in `_execute_once` is dead code. The `elif inspection.output in ("HEARTBEAT_ERROR", "HEARTBEAT_OK")` check at line 826 can be simplified since `inspection.output` will never be `LLM_ERROR: ...` anymore — it'll be either a valid parsed output or an exception.

**Files:**
- Modify: `src/everbot/core/runtime/heartbeat.py:824-829`

- [ ] **Step 1: Verify the dead code path**

Read lines 824-829 of heartbeat.py. The `else: result = inspection.output` branch previously caught `LLM_ERROR: ...` strings. With inspector now raising, this branch only receives valid LLM output or `HEARTBEAT_OK`/`HEARTBEAT_ERROR`. No change needed to the conditional logic itself — it already handles those cases correctly. Just verify this is true by reading the code.

- [ ] **Step 2: Run the full heartbeat test suite to confirm nothing broke**

Run: `python -m pytest tests/unit/test_heartbeat_runner_core.py tests/integration/test_heartbeat_runner_flow.py -v`
Expected: All pass.

- [ ] **Step 3: Commit (only if any cleanup was done)**

If no code changes were needed, skip this commit.

---

### Task 4: Suppress transient errors in `run_once_with_options`

This is the key change. `run_once_with_options` is the single gateway to user notifications. When `_execute_with_retry` raises after all retries are exhausted, check if the exception is transient — if so, log it and return silently instead of calling `_emit_result`.

**Files:**
- Modify: `src/everbot/core/runtime/heartbeat.py:1598-1607`
- Modify: `tests/integration/test_heartbeat_runner_flow.py`

- [ ] **Step 1: Write failing test — transient errors are not emitted**

```python
# tests/integration/test_heartbeat_runner_flow.py — add to existing file

@pytest.mark.asyncio
async def test_run_once_suppresses_transient_llm_error(tmp_path: Path):
    """Transient LLM errors (e.g. connection reset) should NOT be pushed to users."""
    callback_args: list[tuple] = []

    async def _on_result(agent_name: str, result: str):
        callback_args.append((agent_name, result))

    runner = _make_runner(
        workspace_path=tmp_path,
        on_result=_on_result,
        max_retries=1,
    )

    # Simulate a transient LLM error
    runner._execute_with_retry = AsyncMock(
        side_effect=ConnectionError("peer closed connection without sending complete message body")
    )

    result = await runner.run_once_with_options(force=True)
    assert result == "HEARTBEAT_FAILED"
    # Key assertion: on_result callback was NOT called
    assert len(callback_args) == 0
```

- [ ] **Step 2: Write failing test — permanent errors are still emitted**

```python
# tests/integration/test_heartbeat_runner_flow.py — add to existing file

@pytest.mark.asyncio
async def test_run_once_emits_permanent_error(tmp_path: Path):
    """Permanent errors (e.g. invalid API key) should still be pushed to users."""
    callback_args: list[tuple] = []

    async def _on_result(agent_name: str, result: str):
        callback_args.append((agent_name, result))

    runner = _make_runner(
        workspace_path=tmp_path,
        on_result=_on_result,
        max_retries=1,
    )

    exc = Exception("invalid api key")
    exc.status_code = 401
    runner._execute_with_retry = AsyncMock(side_effect=exc)

    result = await runner.run_once_with_options(force=True)
    assert result == "HEARTBEAT_FAILED"
    # Key assertion: on_result WAS called for permanent error
    assert len(callback_args) == 1
    assert "invalid api key" in callback_args[0][1]
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/integration/test_heartbeat_runner_flow.py::test_run_once_suppresses_transient_llm_error tests/integration/test_heartbeat_runner_flow.py::test_run_once_emits_permanent_error -v`
Expected: `test_run_once_suppresses_transient_llm_error` FAILS (callback is currently called for all errors). `test_run_once_emits_permanent_error` PASSES (current behavior already emits all errors).

- [ ] **Step 4: Modify `run_once_with_options` to suppress transient errors**

In `src/everbot/core/runtime/heartbeat.py`, replace the except block in `run_once_with_options` (lines 1598-1607):

```python
        except Exception as e:
            failure_summary = f"HEARTBEAT_FAILED: {type(e).__name__}: {e}"
            logger.error("Heartbeat run failed: %s", failure_summary)
            if self.on_result:
                try:
                    await self._emit_result(failure_summary)
                except Exception as callback_error:
                    logger.error("Heartbeat failure callback failed: %s", callback_error)
            logger.error("[%s] 心跳失败: %s", self.agent_name, e)
            return "HEARTBEAT_FAILED"
```

With:

```python
        except Exception as e:
            failure_summary = f"HEARTBEAT_FAILED: {type(e).__name__}: {e}"
            if _is_transient_llm_error(e):
                logger.info("[%s] Transient LLM error, suppressing user notification: %s", self.agent_name, e)
            else:
                logger.error("Heartbeat run failed: %s", failure_summary)
                if self.on_result:
                    try:
                        await self._emit_result(failure_summary)
                    except Exception as callback_error:
                        logger.error("Heartbeat failure callback failed: %s", callback_error)
                logger.error("[%s] 心跳失败: %s", self.agent_name, e)
            return "HEARTBEAT_FAILED"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/integration/test_heartbeat_runner_flow.py::test_run_once_suppresses_transient_llm_error tests/integration/test_heartbeat_runner_flow.py::test_run_once_emits_permanent_error -v`
Expected: Both PASS.

- [ ] **Step 6: Run existing failure callback test to ensure no regression**

Run: `python -m pytest tests/integration/test_heartbeat_runner_flow.py::test_run_once_with_options_reports_failure_via_callback -v`
Expected: PASS — `RuntimeError("boom")` is not transient, so it's still emitted.

- [ ] **Step 7: Commit**

```bash
git add src/everbot/core/runtime/heartbeat.py tests/integration/test_heartbeat_runner_flow.py
git commit -m "fix(heartbeat): suppress transient LLM errors from user notifications"
```

---

### Task 5: Full regression test

- [ ] **Step 1: Run the complete test suite**

Run: `python -m pytest tests/unit/test_heartbeat_runner_core.py tests/unit/test_inspector.py tests/unit/test_cron_executor.py tests/integration/test_heartbeat_runner_flow.py -v`
Expected: All tests pass.

- [ ] **Step 2: Run broader tests to catch import issues**

Run: `python -m pytest tests/ -x --timeout=60 -q`
Expected: No failures.

- [ ] **Step 3: Final commit (if any fixups needed)**

```bash
git add -u
git commit -m "test: fix regressions from transient error suppression"
```
