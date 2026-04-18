# LLM Error Handling Architecture Refactor

## Problem

Skill job 的 LLM 错误处理分散在各 job 内部，采用字符串白名单匹配 (`_is_missing_skill_llm`) + 私有 sentinel (`_SkipResult`) 模式。这导致：

1. **白名单追不上新错误类型** — `_is_missing_skill_llm` 只覆盖 3 种字符串，Connection error / timeout / rate limit 等全部漏掉
2. **watermark 被错误推进** — LLM 失败时 job 返回降级结果（空 dict / 默认分数），watermark 推进代码正常执行，session 数据永久丢失
3. **保护机制不可复用** — `_SkipResult` 是 memory_review 的私有方案，task_discover 完全没有保护，新 job 作者不知道需要实现这套机制
4. **无效数据静默写入** — skill_evaluate 的 judge 在 LLM 失败时返回 0.5 默认分数并保存为正式报告

## Design

### Exception Hierarchy

新增 `src/everbot/core/jobs/llm_errors.py`，定义两个异常类：

```python
class LLMTransientError(Exception):
    """LLM 暂时不可用：连接失败、超时、限流、5xx。可重试。"""

class LLMConfigError(Exception):
    """LLM 配置问题：模型不存在、API key 无效、依赖缺失。需人工介入。"""
```

区别：`LLMTransientError` 下次调度自动重试可恢复；`LLMConfigError` 需要修改配置。两者共同点：都不应推进 watermark。

### LLM Client Layer (`_SkillLLMClient`)

在 `heartbeat.py` 的 `_SkillLLMClient.complete()` 中统一分类异常：

- 基于 OpenAI SDK 的异常类型分类（`APIConnectionError` / `APITimeoutError` / `RateLimitError` / `InternalServerError` → `LLMTransientError`；`AuthenticationError` / `NotFoundError` → `LLMConfigError`）
- 原生 Python 异常：`ConnectionError` / `TimeoutError` / `OSError` → `LLMTransientError`
- 现有 `_is_missing_skill_llm` 覆盖的场景（module not found、config not found）→ `LLMConfigError`

调用方拿到的永远是 `LLMTransientError` 或 `LLMConfigError`，不再需要自己猜异常含义。

### Framework Layer (`_invoke_job`)

在 `cron.py` 的 `_invoke_job` 中新增 catch：

```python
except (LLMTransientError, LLMConfigError) as exc:
    self._write_event("job_degraded", skill=job_name,
                      error=str(exc)[:200],
                      retriable=isinstance(exc, LLMTransientError))
    logger.warning("Job %s skipped (LLM unavailable): %s", job_name, exc)
    return f"LLM unavailable: {exc}"
```

行为：
- 不 raise — caller 走正常 rearm 流程，task 回到 PENDING 等下次调度
- watermark 不推进 — job 内部的 `set_watermark()` 代码因异常冒泡而未执行
- 抑制 Telegram 推送 — 瞬时问题下次自动恢复，持续性问题由 health_check 报告

### Job Layer Changes

**memory_review.py:**
- 删除 `_is_missing_skill_llm()` 函数
- 删除 `_SkipResult` 类
- `_analyze_memory_consolidation`: 删除 try/except，LLM 异常直接冒泡
- `_compress_to_user_profile`: 删除 try/except，LLM 异常直接冒泡
- `run()`: 删除 `isinstance(compress_result, _SkipResult)` 守卫。走到 watermark 代码说明两个 LLM 调用都成功了

**task_discover.py:**
- `_discover_tasks`: 删除 try/except，LLM 异常直接冒泡
- watermark 推进代码自然被跳过

**skill_evaluate.py:**
- 循环内 catch `LLMTransientError` → break（不继续尝试，后续都会失败）
- 循环结束后如果 `llm_failed` 则 re-raise `LLMTransientError`
- 已成功评估的 report 是有效的（LLM 真正返回了结果），不需要回滚

**judge.py:**
- `judge_segments`: 只 catch 非 LLM 异常（如 JSON parse error 返回默认分数）
- LLM 异常冒泡到 skill_evaluate 循环层

**health_check.py:**
- `_check_llm` 的 catch 改为 `(LLMTransientError, LLMConfigError)`
- 行为不变：报告 LLM 状态，不管 watermark

### Files Changed

| File | Change |
|------|--------|
| `core/jobs/llm_errors.py` | New: two exception classes |
| `core/runtime/heartbeat.py` | `_SkillLLMClient.complete()`: add exception classification |
| `core/runtime/cron.py` | `_invoke_job`: add LLM exception catch + push suppression |
| `core/jobs/memory_review.py` | Delete `_SkipResult`/`_is_missing_skill_llm`, remove try/except around LLM calls |
| `core/jobs/task_discover.py` | `_discover_tasks`: remove try/except |
| `core/jobs/skill_evaluate.py` | Loop: break on LLMTransientError, re-raise after loop |
| `core/slm/judge.py` | `judge_segments`: only catch non-LLM exceptions |
| `core/jobs/health_check.py` | Update catch to new exception types |

### Files NOT Changed

- `TaskExecutionGate` — scheduling-level watermark, unrelated
- `ReflectionState` API — no change needed
- Rearm/scheduling mechanism — works as-is

### Test Strategy

- `_SkillLLMClient` unit tests: verify raw exceptions are correctly classified into `LLMTransientError` / `LLMConfigError`
- `_invoke_job` unit tests: verify LLM exceptions return degraded string (not raise), and event is written
- memory_review unit tests: verify watermark does not advance when LLM fails
- task_discover unit tests: same watermark protection verification
- skill_evaluate unit tests: verify partial success (some evaluated) + break + re-raise on LLM failure
- judge unit tests: verify LLM exceptions propagate, only parse errors return defaults
