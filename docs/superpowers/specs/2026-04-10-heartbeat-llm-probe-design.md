# Heartbeat LLM Probe

## Problem

When the LLM API is unreachable, all heartbeat jobs fail individually and spam fragmented error messages to Telegram. The user gets no single clear signal that the agent is non-functional.

## Design

Add an LLM connectivity probe at the top of each heartbeat cycle. If it fails, skip all jobs and send one consolidated notification with cooldown.

### Implementation

1. **Probe**: New `_probe_llm()` in `HeartbeatRunner`. Creates a minimal completion request (`max_tokens=1`) using the agent's configured model. Reuses `_is_transient_llm_error()` for error classification.

2. **Gate**: In `_execute_once()`, after reading HEARTBEAT.md but before agent creation/job dispatch, call `_probe_llm()`. If it fails, skip all jobs and return a notification string.

3. **Notification cooldown**: Persist `llm_unavailable_last_notified_at` in heartbeat state. First failure notifies immediately. Subsequent failures only notify if >= 2h since last notification. Recovery always notifies.

### Notification messages

- First failure: `"LLM 不可用 ({error}), 心跳任务已暂停"`
- Repeated (>= 2h): `"LLM 持续不可用 (已 {hours}h), 心跳任务仍暂停"`
- Recovery: `"LLM 已恢复, 心跳任务恢复正常"`

### What doesn't change

- Individual job error handling
- `_is_transient_llm_error()` classification
- Heartbeat interval, active_hours, lock mechanism
- CronDelivery / event emission path (reused as-is)
