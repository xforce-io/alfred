# S4 Drive Plan: Telegram send failure must attempt mailbox deposit

## Requirement
Per design §4, when Telegram send fails after retries:
- Mailbox deposit must be attempted (mailbox as anchor)
- If mailbox deposit succeeds, task succeeds (no RuntimeError)
- If mailbox deposit also fails, RuntimeError is raised → task FAILED → retry

## Automated Tests

### Location
`tests/integration/test_s4_telegram_mailbox_fallback.py`

### Run Command
```bash
cd $PROJECT_ROOT
PYTHONPATH=src python3 -m pytest tests/integration/test_s4_telegram_mailbox_fallback.py -v
```

Or via unified runner:
```bash
./tests/run_tests.sh integration -f test_s4_telegram_mailbox_fallback -v
```

### Test Cases

1. **test_telegram_send_failure_attempts_mailbox**
   - Constructs `TelegramChannel` with real API (`bot_token`, `session_manager`)
   - Patches `_send_split_with_entities` to return `(0, 1)` → Telegram send failed
   - Patches `session_manager.deposit_mailbox_event` to return True
   - Calls `_on_background_event` with realistic heartbeat data
   - Asserts no exception is raised (mailbox anchor saves the message)
   - Verifies both Telegram send and mailbox deposit were attempted

2. **test_both_telegram_and_mailbox_fail_raises**
   - Patches Telegram send to fail (0, 1)
   - Patches mailbox deposit to return False
   - Calls `_on_background_event`
   - Asserts RuntimeError is raised with "Both Telegram delivery and mailbox deposit failed for heartbeat_delivery"
   - Verifies both were attempted

3. **test_telegram_send_success_mailbox_still_attempted**
   - Patches Telegram send to succeed (1, 1)
   - Patches projection to return False (not projected)
   - Verifies mailbox deposit is STILL called (S4: always attempt unless projected)

4. **test_telegram_send_success_projected_skips_mailbox**
   - Patches Telegram send to succeed (1, 1)
   - Patches projection to return True (projected)
   - Verifies mailbox deposit is NOT called (projection replaced mailbox mirror)

## Expected Outcomes

- All 4 tests pass
- Telegram failure triggers mailbox fallback
- Both-failed raises RuntimeError (task will retry)
- Mailbox deposit always attempted unless projected (S4 design)

## Acceptance

```bash
PYTHONPATH=src python3 -m pytest tests/integration/test_s4_telegram_mailbox_fallback.py -v
# Exit code: 0
# 4 passed
```

## Live Telegram Drive

**Status**: Skipped with rationale

Live Telegram drive is NOT included because:
- Telegram bot requires real API token and user interaction
- Automated tests fully cover the logic branches:
  - Telegram fail (ok_count < total) → mailbox attempt → mailbox succeed (no raise)
  - Telegram fail → mailbox attempt → mailbox fail (raise)
  - Telegram succeed + not projected → mailbox attempt
  - Telegram succeed + projected → no mailbox attempt

Live drive would require:
1. Configuring a test Telegram bot with valid token
2. Temporarily injecting a fault in Telegram send (e.g., invalid chat_id, rate limit)
3. Verifying mailbox event is deposited
4. Checking cron task does NOT fail (mailbox anchor saved it)

**Conclusion**: Automated tests mock all relevant branches. Live Telegram drive adds no new code path coverage. The mocks accurately reflect the real `_on_background_event` flow as of commit 6e32411.
