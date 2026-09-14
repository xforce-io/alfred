# S3 Drive Plan: deposit/history inject failure must fail task

## Requirement
Per design §3, when `deposit_job_event` or `inject_to_history` returns False:
- Task must be marked FAILED (not DONE)
- Task will be retried on next cron cycle
- No silent data loss

## Automated Tests

### Location
`tests/integration/test_s3_deposit_history_fault_injection.py`

### Run Command
```bash
cd $PROJECT_ROOT
PYTHONPATH=src python3 -m pytest tests/integration/test_s3_deposit_history_fault_injection.py -v
```

Or via unified runner:
```bash
./tests/run_tests.sh integration -f test_s3_deposit_history_fault_injection -v
```

### Test Cases

1. **test_deposit_job_event_false_fails_task**
   - Seeds an isolated job task (`execution_mode="isolated", job="skill-evaluate"`)
   - Patches `CronDelivery.deposit_job_event` to return False
   - Runs `CronExecutor.tick()`
   - Asserts task status is "failed" (not "done")
   - Asserts error contains "Failed to deposit job completion event to mailbox"
   - Verifies `inject_to_history` is NOT called (task failed before that)

2. **test_inject_to_history_false_fails_task**
   - Seeds an isolated job task
   - Patches `deposit_job_event` to return True (succeeds)
   - Patches `inject_to_history` to return False (fails)
   - Runs `CronExecutor.tick()`
   - Asserts task status is "failed"
   - Asserts error contains "Failed to inject job result to history"
   - Verifies both deposit and inject were called

3. **test_staged_history_step_false_fails_task**
   - Uses `RoutineCheckpointStore.run_delivery_step` directly
   - Provides a lambda that returns False for "history" step
   - Asserts RuntimeError is raised with "Delivery step history failed: operation returned False"
   - Verifies manifest does NOT mark step as "delivered" (will retry on next run)

## Expected Outcomes

- All 3 tests pass
- Task runner correctly raises RuntimeError on deposit/history False
- Staged delivery correctly raises RuntimeError on False from operation
- Manifest state is NOT advanced on failure (task will retry)

## Acceptance

```bash
PYTHONPATH=src python3 -m pytest tests/integration/test_s3_deposit_history_fault_injection.py -v
# Exit code: 0
# 3 passed
```

## Live Daemon Drive

**Status**: Skipped with rationale

Automated tests fully cover the fault-injection logic:
- `CronExecutor` error handling and task status marking
- `RoutineCheckpointStore` delivery step failure recovery
- Both deposit and history inject False paths

Live daemon drive would require:
1. Manually forcing mailbox/history storage to reject writes (disk full, permission errors)
2. Observing task retry behavior over multiple cron cycles
3. No additional code paths tested vs. unit/integration mocks

**Conclusion**: Automated tests provide sufficient evidence. Live drive adds no new coverage.
