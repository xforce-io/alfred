# Heartbeat LLM Probe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an LLM connectivity probe at the start of each heartbeat cycle so that when the LLM is unreachable, all jobs are skipped and the user gets one consolidated notification with cooldown instead of fragmented error spam.

**Architecture:** A new `_probe_llm()` method on `HeartbeatRunner` does a `max_tokens=1` completion using `_SkillLLMClient`. The probe runs inside `_execute_once()` after reading HEARTBEAT.md but before agent creation. Probe failure state is tracked via two fields in the heartbeat event log to control notification cooldown (first fail: immediate, repeat: every 2h, recovery: immediate).

**Tech Stack:** Python, asyncio, existing `_SkillLLMClient`, existing `_is_transient_llm_error` / `_is_permanent_error`

---

### Task 1: Add `_probe_llm()` method and gate logic

**Files:**
- Modify: `src/everbot/core/runtime/heartbeat.py` (HeartbeatRunner class)
- Test: `tests/unit/test_heartbeat_runner_core.py`

- [ ] **Step 1: Write failing tests for `_probe_llm()`**

```python
# In tests/unit/test_heartbeat_runner_core.py

@pytest.mark.asyncio
async def test_probe_llm_success(tmp_path):
    """Probe returns True when LLM responds."""
    runner = _make_runner(workspace_path=tmp_path)
    mock_client = AsyncMock()
    mock_client.complete = AsyncMock(return_value="ok")
    runner._create_skill_llm_client = lambda: mock_client
    assert await runner._probe_llm() is True


@pytest.mark.asyncio
async def test_probe_llm_transient_failure(tmp_path):
    """Probe returns False on transient LLM error."""
    runner = _make_runner(workspace_path=tmp_path)
    mock_client = AsyncMock()
    mock_client.complete = AsyncMock(side_effect=ConnectionError("Connection error"))
    runner._create_skill_llm_client = lambda: mock_client
    assert await runner._probe_llm() is False


@pytest.mark.asyncio
async def test_probe_llm_timeout(tmp_path):
    """Probe returns False on timeout."""
    runner = _make_runner(workspace_path=tmp_path)
    mock_client = AsyncMock()
    mock_client.complete = AsyncMock(side_effect=asyncio.TimeoutError())
    runner._create_skill_llm_client = lambda: mock_client
    assert await runner._probe_llm() is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_heartbeat_runner_core.py::test_probe_llm_success tests/unit/test_heartbeat_runner_core.py::test_probe_llm_transient_failure tests/unit/test_heartbeat_runner_core.py::test_probe_llm_timeout -v`
Expected: FAIL with `AttributeError: 'HeartbeatRunner' object has no attribute '_probe_llm'`

- [ ] **Step 3: Implement `_probe_llm()`**

Add this method to `HeartbeatRunner` class in `heartbeat.py`, near `_create_skill_llm_client()` (around line 1140):

```python
async def _probe_llm(self) -> bool:
    """Quick LLM connectivity check. Returns True if LLM is reachable."""
    try:
        client = self._create_skill_llm_client()
        await asyncio.wait_for(
            client.complete("ping", max_tokens=1),
            timeout=15,
        )
        return True
    except Exception as e:
        logger.warning("[%s] LLM probe failed: %s", self.agent_name, e)
        return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_heartbeat_runner_core.py::test_probe_llm_success tests/unit/test_heartbeat_runner_core.py::test_probe_llm_transient_failure tests/unit/test_heartbeat_runner_core.py::test_probe_llm_timeout -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/everbot/core/runtime/heartbeat.py tests/unit/test_heartbeat_runner_core.py
git commit -m "feat(heartbeat): add _probe_llm() for LLM connectivity check"
```

---

### Task 2: Add probe gate in `_execute_once()` with notification cooldown

**Files:**
- Modify: `src/everbot/core/runtime/heartbeat.py`
- Test: `tests/unit/test_heartbeat_runner_core.py`

- [ ] **Step 1: Write failing tests for probe gate behavior**

```python
# In tests/unit/test_heartbeat_runner_core.py

@pytest.mark.asyncio
async def test_execute_once_skips_on_probe_failure(tmp_path):
    """When LLM probe fails, _execute_once skips all jobs and returns notification."""
    runner = _make_runner(workspace_path=tmp_path)
    # Make probe fail
    runner._probe_llm = AsyncMock(return_value=False)
    # Provide valid HEARTBEAT.md so we get past the idle check
    runner._read_heartbeat_md = lambda: _build_structured_md([{
        "id": "t1", "title": "Test", "description": "", "schedule": "1h",
        "state": "pending", "enabled": True, "execution_mode": "inline",
    }])
    # Mock session/lock
    runner.session_manager = MagicMock()
    runner.session_manager.acquire_session = AsyncMock(return_value=True)
    runner.session_manager.release_session = MagicMock()
    runner.session_manager.file_lock = MagicMock(return_value=_always_acquired_lock())
    runner.session_manager.migrate_legacy_sessions_for_agent = AsyncMock()
    result = await runner._execute_once()
    assert "LLM 不可用" in result


@pytest.mark.asyncio
async def test_execute_once_cooldown_suppresses_repeated_notification(tmp_path):
    """Second probe failure within 2h should be suppressed (HEARTBEAT_OK)."""
    runner = _make_runner(workspace_path=tmp_path)
    runner._probe_llm = AsyncMock(return_value=False)
    runner._read_heartbeat_md = lambda: _build_structured_md([{
        "id": "t1", "title": "Test", "description": "", "schedule": "1h",
        "state": "pending", "enabled": True, "execution_mode": "inline",
    }])
    runner.session_manager = MagicMock()
    runner.session_manager.acquire_session = AsyncMock(return_value=True)
    runner.session_manager.release_session = MagicMock()
    runner.session_manager.file_lock = MagicMock(return_value=_always_acquired_lock())
    runner.session_manager.migrate_legacy_sessions_for_agent = AsyncMock()
    # First call — should notify
    result1 = await runner._execute_once()
    assert "LLM 不可用" in result1
    # Second call — within cooldown, should suppress
    result2 = await runner._execute_once()
    assert result2 == "HEARTBEAT_OK"


@pytest.mark.asyncio
async def test_execute_once_recovery_notification(tmp_path):
    """When LLM recovers after being unavailable, notify recovery."""
    runner = _make_runner(workspace_path=tmp_path)
    runner._read_heartbeat_md = lambda: _build_structured_md([{
        "id": "t1", "title": "Test", "description": "", "schedule": "1h",
        "state": "pending", "enabled": True, "execution_mode": "inline",
    }])
    runner.session_manager = MagicMock()
    runner.session_manager.acquire_session = AsyncMock(return_value=True)
    runner.session_manager.release_session = MagicMock()
    runner.session_manager.file_lock = MagicMock(return_value=_always_acquired_lock())
    runner.session_manager.migrate_legacy_sessions_for_agent = AsyncMock()
    # First: probe fails
    runner._probe_llm = AsyncMock(return_value=False)
    await runner._execute_once()
    # Then: probe succeeds — should emit recovery and continue normal flow
    runner._probe_llm = AsyncMock(return_value=True)
    # Mock the rest of normal execution to avoid agent creation
    runner._get_or_create_agent = AsyncMock()
    runner._execute_structured_tasks = AsyncMock(return_value="HEARTBEAT_OK task done")
    runner._should_deliver = lambda r: False
    runner._save_session_atomic = AsyncMock()
    runner._file_mgr = MagicMock()
    runner._file_mgr.heartbeat_mode = "structured_due"
    runner._file_mgr.task_list = []
    result = await runner._execute_once()
    # Recovery notification should have been emitted via delivery
    assert runner._llm_unavailable_since is None  # state cleared


# Helper for lock mock
@contextmanager
def _always_acquired_lock():
    yield True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_heartbeat_runner_core.py::test_execute_once_skips_on_probe_failure tests/unit/test_heartbeat_runner_core.py::test_execute_once_cooldown_suppresses_repeated_notification tests/unit/test_heartbeat_runner_core.py::test_execute_once_recovery_notification -v`
Expected: FAIL

- [ ] **Step 3: Implement probe gate in `_execute_once()`**

Add two instance variables in `__init__()` (after `self._pending_delivery_details`):

```python
self._llm_unavailable_since: Optional[datetime] = None
self._llm_unavailable_last_notified_at: Optional[datetime] = None
```

Add the probe gate inside `_run_locked_body()` in `_execute_once()`, right after the HEARTBEAT.md idle check (after line 755 `return "HEARTBEAT_IDLE"`) but before the rest of the logic. Insert before the comment `# Recover tasks stuck in 'running'` (line 757):

```python
                # LLM probe: skip all jobs if LLM is unreachable
                if not await self._probe_llm():
                    now = datetime.now()
                    if self._llm_unavailable_since is None:
                        # First failure — notify immediately
                        self._llm_unavailable_since = now
                        self._llm_unavailable_last_notified_at = now
                        self._record_timeline_event("turn_end", run_id, status="completed", result="LLM_UNAVAILABLE")
                        return "LLM 不可用, 心跳任务已暂停"
                    else:
                        elapsed = now - self._llm_unavailable_last_notified_at
                        if elapsed >= timedelta(hours=2):
                            # Repeat notification after cooldown
                            hours = (now - self._llm_unavailable_since).total_seconds() / 3600
                            self._llm_unavailable_last_notified_at = now
                            self._record_timeline_event("turn_end", run_id, status="completed", result="LLM_UNAVAILABLE")
                            return f"LLM 持续不可用 (已 {hours:.0f}h), 心跳任务仍暂停"
                        else:
                            # Within cooldown — suppress
                            self._record_timeline_event("turn_end", run_id, status="completed", result="LLM_UNAVAILABLE_SUPPRESSED")
                            return "HEARTBEAT_OK"

                # LLM recovery check
                if self._llm_unavailable_since is not None:
                    self._llm_unavailable_since = None
                    self._llm_unavailable_last_notified_at = None
                    logger.info("[%s] LLM recovered", self.agent_name)
                    # Emit recovery notification via delivery
                    await self._delivery.deliver_result("LLM 已恢复, 心跳任务恢复正常", run_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_heartbeat_runner_core.py::test_execute_once_skips_on_probe_failure tests/unit/test_heartbeat_runner_core.py::test_execute_once_cooldown_suppresses_repeated_notification tests/unit/test_heartbeat_runner_core.py::test_execute_once_recovery_notification -v`
Expected: PASS

- [ ] **Step 5: Run full test suite to check for regressions**

Run: `python -m pytest tests/unit/test_heartbeat_runner_core.py -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/everbot/core/runtime/heartbeat.py tests/unit/test_heartbeat_runner_core.py
git commit -m "feat(heartbeat): gate job execution on LLM probe with notification cooldown"
```
