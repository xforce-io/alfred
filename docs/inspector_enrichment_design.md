# Inspector 反思上下文增强设计 v2

最后更新：2026-03-07

## 1. 背景

当前 `Inspector` 反思阶段只向 LLM 注入 `MEMORY.md` 和 `HEARTBEAT.md`。在这种上下文下，模型几乎只能做“是否发现新 routine”这一件事，而且因为看不到近期对话、任务执行情况和异常事件，绝大多数时候只会返回 `HEARTBEAT_OK`。

虽然 `InspectionContext` 已经预留了 `session_summary` 和 `task_execution_stats` 字段，但当前实现并未填充它们。

## 2. 现状问题

### 2.1 上下文不足

- 反思 prompt 只包含 `MEMORY.md` 和 `HEARTBEAT.md`
- `Inspector` 无法感知用户最近在做什么
- `Inspector` 无法感知任务是否持续失败、堆积或异常

### 2.2 Skip 逻辑过早、过窄

当前反思会先在 `heartbeat.py` 外层被短路，只有 `MEMORY.md` 和 `HEARTBEAT.md` 变化才会继续执行。

这导致以下问题：

- 用户主会话持续活跃，但两个 md 文件未变时，反思不会触发
- 任务状态和事件流发生明显变化时，反思不会触发
- 即使后续给 `Inspector` 增加更多上下文，也会被外层 skip 逻辑绕过

### 2.3 投递职责混乱

当前 `Inspector` 在 `auto_register_routines=False` 时会直接调用 `deposit_mailbox_event` 投递 routine proposal。

如果未来再支持 `push_message`，继续沿用“Inspector 直接投递 + heartbeat 再做统一投递”的方式，会造成：

- 双重通知
- 去重逻辑分散
- `Inspector` 同时承担“决策”和“投递”两类职责

### 2.4 输出协议不统一

当前 `ReflectionManager.extract_routine_proposals()` 只关心 `{"routines":[...]}`。

如果扩展为支持：

- routine proposal
- push message
- HEARTBEAT_OK

就需要一个统一 schema，否则 prompt、parser、投递逻辑会持续分叉。

## 3. 设计目标

1. 增强 `Inspector` 的反思上下文，补齐会话摘要、任务统计、近期事件。
2. 将反思范围从“仅发现 routine”扩展到“routine 发现 + 异常告警 + 主动汇报”。
3. 修复现有 skip 逻辑，让完整上下文变化都能触发反思。
4. 明确边界：`Inspector` 负责思考，`HeartbeatRunner` 负责统一投递。
5. 在保持兼容的前提下，引入统一输出协议。

## 4. 设计方案

### 4.1 Context 收集

在 `Inspector` 中新增 `_gather_context()`，集中构建反思所需上下文。

```python
@dataclass
class InspectionContext:
    """Context used by the reflection prompt."""

    memory_content: Optional[str] = None
    heartbeat_content: Optional[str] = None
    session_summary: Optional[str] = None
    task_execution_stats: Dict[str, Any] = field(default_factory=dict)
    recent_events: Optional[str] = None
    existing_routines: List[Any] = field(default_factory=list)


async def _gather_context(self, heartbeat_content: str) -> InspectionContext:
    """Collect reflection context from runtime and local files."""
    return InspectionContext(
        memory_content=self._read_memory_md(),
        heartbeat_content=heartbeat_content,
        session_summary=self._gather_session_summary(),
        task_execution_stats=self._gather_task_stats(),
        recent_events=self._gather_recent_events(),
        existing_routines=self._gather_existing_routines(),
    )
```

### 4.2 Session 摘要

不把“主会话识别”设计成新的动态协议。当前代码里已经存在稳定的主会话定义：`session_manager.get_primary_session_id(agent_name)`。

因此这里采用更贴近现状的策略：

1. 优先摘要 canonical primary session
2. 再补充最近 1 到 2 个 reviewable session
3. 不依赖 `active_session_id`、环境变量或新的 session 协议

```python
def _gather_session_summary(self) -> str:
    """Summarize the primary session and a few recent reviewable sessions."""
    primary_id = self.session_manager.get_primary_session_id(self.agent_name)
    sections: List[str] = []

    primary_path = self._session_path(primary_id)
    if primary_path.exists():
        digest = self._extract_session_digest(primary_path, max_messages=50, max_chars=3000)
        if digest:
            sections.append(f"### Primary Session ({primary_id})\n{digest}")

    scanner = SessionScanner(self._sessions_dir)
    recent = scanner.get_reviewable_sessions(
        watermark=self._last_session_watermark,
        agent_name=self.agent_name,
        max_sessions=2,
        max_age_days=3,
    )
    for item in recent:
        if item.id == primary_id:
            continue
        digest = scanner.extract_digest(item.path, max_messages=10, max_chars=800)
        if digest:
            sections.append(f"### Recent Session ({item.id})\n{digest}")

    return "\n\n".join(sections) if sections else "[No recent session summary]"
```

说明：

- 这样可以直接覆盖当前 canonical primary session
- 仍然复用现有 `SessionScanner`，避免引入新的扫描框架
- 如果未来需要纳入更多 channel session，再单独扩展 scanner 或 session resolver，而不是在本设计里先假设新接口存在

### 4.3 任务执行统计

从当前解析后的 task list 聚合近 24 小时状态：

```python
def _gather_task_stats(self) -> dict:
    """Aggregate task execution stats for the last 24 hours."""
    tasks = self._load_task_list()
    recent_cutoff = datetime.now() - timedelta(hours=24)

    stats = {
        "total": len(tasks),
        "pending": 0,
        "running": 0,
        "failed_24h": 0,
        "completed_24h": 0,
        "failures": [],
    }

    for task in tasks:
        if task.state == "PENDING":
            stats["pending"] += 1
        elif task.state == "RUNNING":
            stats["running"] += 1
        elif task.state == "FAILED" and task.updated_at and task.updated_at > recent_cutoff:
            stats["failed_24h"] += 1
            if len(stats["failures"]) < 3:
                stats["failures"].append(
                    {
                        "title": task.title,
                        "error": (task.error_message or "Unknown")[:200],
                        "time": task.updated_at.isoformat(),
                    }
                )
        elif task.state == "COMPLETED" and task.completed_at and task.completed_at > recent_cutoff:
            stats["completed_24h"] += 1

    return stats
```

### 4.4 近期系统事件

读取 `heartbeat_events.jsonl` 最近一小段窗口，只保留非 `ok` 事件和关键状态变化：

```python
def _gather_recent_events(self) -> str:
    """Collect recent notable heartbeat events."""
    lines = _tail_lines(self._heartbeat_events_file, limit=50)
    notable: List[str] = []

    for line in lines:
        try:
            event = json.loads(line)
        except Exception:
            continue

        result = str(event.get("result") or "").lower()
        name = str(event.get("event") or event.get("event_type") or "unknown")
        if result == "ok" and name not in {"task_failed", "routine_proposal"}:
            continue

        notable.append(
            f"- [{event.get('timestamp', '?')}] {name}: "
            f"{str(event.get('summary') or event.get('reason') or '')[:100]}"
        )

    return "\n".join(notable[-10:]) if notable else "[No notable heartbeat events]"
```

说明：

- 当前代码里 event key 既有 `event`，也有 `result`，设计应兼容这两类字段
- 不要求引入新的 event schema，只在读取侧做兼容格式化

### 4.5 完整上下文变更检测

#### 核心原则

- 反思是否执行，应由完整上下文决定，而不是只看两个 md 文件
- skip 判断应下沉到 `Inspector` 内部
- `heartbeat.py` 外层的提前短路逻辑应删除

```python
@dataclass
class InspectionState:
    """Persisted state for inspector change detection."""

    memory_hash: str = ""
    heartbeat_hash: str = ""
    session_summary_hash: str = ""
    task_stats_hash: str = ""
    recent_events_hash: str = ""
    updated_at: str = ""


def _build_state(self, ctx: InspectionContext) -> InspectionState:
    """Build a hash snapshot for the current inspection context."""
    return InspectionState(
        memory_hash=_hash_text(ctx.memory_content),
        heartbeat_hash=_hash_text(ctx.heartbeat_content),
        session_summary_hash=_hash_text(ctx.session_summary),
        task_stats_hash=_hash_text(json.dumps(ctx.task_execution_stats, sort_keys=True)),
        recent_events_hash=_hash_text(ctx.recent_events),
        updated_at=datetime.now().isoformat(),
    )


def _should_inspect(self, ctx: InspectionContext) -> tuple[bool, str]:
    """Decide whether reflection should run for the current context."""
    current = self._build_state(ctx)

    if self._last_state is None:
        self._last_state = current
        self._save_state(current)
        return True, "first_run"

    if self._force_interval_elapsed():
        self._last_state = current
        self._save_state(current)
        return True, "force_interval_elapsed"

    changed = []
    if current.memory_hash != self._last_state.memory_hash:
        changed.append("memory")
    if current.heartbeat_hash != self._last_state.heartbeat_hash:
        changed.append("heartbeat")
    if current.session_summary_hash != self._last_state.session_summary_hash:
        changed.append("session")
    if current.task_stats_hash != self._last_state.task_stats_hash:
        changed.append("tasks")
    if current.recent_events_hash != self._last_state.recent_events_hash:
        changed.append("events")

    if not changed:
        return False, "context_unchanged"

    self._last_state = current
    self._save_state(current)
    return True, f"context_changed:{','.join(changed)}"
```

### 4.6 Prompt 构建

反思 prompt 改为由 `Inspector` 基于完整上下文生成，而不是继续复用 `heartbeat.py` 里只带两份 md 的模板。

```python
def _build_reflect_prompt(self, ctx: InspectionContext) -> str:
    """Build the enriched reflection prompt."""
    return f"""[系统心跳 - {datetime.now().isoformat()}]

请综合以下信息进行反思，判断是否有值得告知用户的内容。

## 用户近期对话摘要
{ctx.session_summary or "[No recent session summary]"}

## 任务执行状况（近24小时）
- 总任务数: {ctx.task_execution_stats.get("total", 0)}
- 待执行: {ctx.task_execution_stats.get("pending", 0)}
- 执行中: {ctx.task_execution_stats.get("running", 0)}
- 近期完成: {ctx.task_execution_stats.get("completed_24h", 0)}
- 近期失败: {ctx.task_execution_stats.get("failed_24h", 0)}

{self._format_failures(ctx.task_execution_stats.get("failures", []))}

## 近期系统事件
{ctx.recent_events or "[No notable heartbeat events]"}

## 当前 MEMORY.md
{ctx.memory_content or "[Empty]"}

## 当前 HEARTBEAT.md
{ctx.heartbeat_content or "[Empty]"}

---

请从以下维度进行反思：
1. 是否有周期性意图尚未注册为 routine？
2. 是否存在需要提醒用户的异常、失败或堆积？
3. 是否有值得主动汇报的进展、发现或建议？

## 输出格式
仅输出 JSON，不要附加解释性文字。

```json
{{
  "heartbeat_ok": true,
  "push_message": "",
  "routines": []
}}
```

规则：
- 若无需推送且无需 proposal，返回 `{{"heartbeat_ok": true}}`
- 若有任一可执行内容，返回 `heartbeat_ok=false`
- `push_message` 和 `routines` 可以同时存在
"""
```

### 4.7 统一输出协议

定义单一返回协议：

```json
{
  "heartbeat_ok": false,
  "push_message": "optional",
  "routines": []
}
```

对应的结果结构：

```python
@dataclass
class InspectionResult:
    """Structured output of one inspection cycle."""

    heartbeat_ok: bool = True
    push_message: Optional[str] = None
    proposals: List[dict] = field(default_factory=list)
    skipped: bool = False
    skip_reason: Optional[str] = None
    applied: int = 0
    raw_output: str = "HEARTBEAT_OK"
    output: str = "HEARTBEAT_OK"
```

解析逻辑需要同时兼容：

- 新 JSON 协议
- 旧格式 `{"routines":[...]}` JSON
- 旧格式纯文本 `HEARTBEAT_OK`

```python
def _parse_response(self, response: str) -> InspectionResult:
    """Parse reflection response with backward compatibility."""
    result = InspectionResult(raw_output=response)
    payload = self._extract_json(response)

    if isinstance(payload, dict):
        proposals = payload.get("routines", [])
        if payload.get("heartbeat_ok") is True and not proposals and not payload.get("push_message"):
            result.heartbeat_ok = True
            result.output = "HEARTBEAT_OK"
            return result

        result.heartbeat_ok = False
        result.push_message = _normalize_text(payload.get("push_message"))
        result.proposals = [item for item in proposals if isinstance(item, dict)]
        result.output = self._compose_output_text(result.push_message, result.proposals)
        return result

    if "HEARTBEAT_OK" in response.upper().replace(" ", ""):
        result.heartbeat_ok = True
        result.output = "HEARTBEAT_OK"
        return result

    result.heartbeat_ok = False
    result.push_message = response.strip() or None
    result.output = result.push_message or "HEARTBEAT_OK"
    return result
```

注意：

- 旧格式 `{"routines":[...]}` 应视为 `heartbeat_ok=false`
- `output` 仍保留字符串形式，便于复用现有 `_should_deliver(response: str)` 机制

### 4.8 投递职责收敛

`Inspector` 不再直接调用 `deposit_mailbox_event`。统一原则如下：

- `Inspector` 只负责“采集上下文 -> 调用 LLM -> 解析结果”
- `HeartbeatRunner` 负责“自动注册 / 提案投递 / push 投递 / suppress 判定”

这样可以避免当前和未来的双重投递问题。

```python
async def inspect(...) -> InspectionResult:
    """Return a structured inspection result without side effects."""
    ctx = await self._gather_context(heartbeat_content)
    should_run, reason = self._should_inspect(ctx)
    if not should_run:
        return InspectionResult(skipped=True, skip_reason=reason)

    prompt = self._build_reflect_prompt(ctx)
    response = await run_agent(agent, prompt)
    self._update_state_after_success(ctx)
    return self._parse_response(response)
```

Heartbeat 侧统一处理：

```python
async def _handle_inspection_result(self, result: InspectionResult, run_id: str) -> str:
    """Handle delivery and routine side effects for one inspection result."""
    if result.skipped or result.heartbeat_ok:
        return "HEARTBEAT_OK"

    if self.auto_register_routines:
        applied_text = self._apply_inspection_proposals(result.proposals, run_id)
    else:
        applied_text = await self._deposit_routine_proposals_to_mailbox(result.proposals, run_id)

    final_output = self._merge_inspection_outputs(result.push_message, applied_text)
    if self._should_deliver(final_output):
        await self._inject_result_to_primary_history(final_output, run_id)
        await self._deposit_deliver_event_to_primary_session(final_output, run_id)
    return final_output
```

说明：

- `_should_deliver()` 当前签名是 `response: str`，因此这里继续围绕字符串输出设计
- 不引入 event object 版本的 `_should_deliver()`
- routine proposal mailbox 投递可以继续复用现有逻辑，只是从 `Inspector` 挪到 `HeartbeatRunner`

### 4.9 调用链调整

反思阶段调用链调整为：

1. `heartbeat.py` 不再在外层调用 `self._inspector.should_skip()`
2. 进入反思阶段后直接调用 `self._inspector.inspect(...)`
3. 由 `Inspector` 内部完成上下文收集和变更检测
4. `HeartbeatRunner` 统一处理 `InspectionResult`

这样可以确保“完整上下文变化”真正生效。

## 5. 代码改动范围

| 文件 | 改动 |
|------|------|
| `src/everbot/core/runtime/inspector.py` | 增加上下文收集、完整状态快照、prompt 构建、统一结果解析；删除直接 mailbox deposit 逻辑 |
| `src/everbot/core/runtime/heartbeat.py` | 删除外层 `self._inspector.should_skip()` 短路；新增 `InspectionResult` 统一处理路径 |
| `src/everbot/core/runtime/reflection.py` | 保留 routine normalize 能力；`extract_routine_proposals()` 可以逐步收敛为 Inspector 内部使用 |
| `tests/unit/test_inspector.py` | 补充完整上下文变更检测、JSON 协议解析、skip 判定、无副作用 inspect 测试 |
| `tests/unit/test_heartbeat.py` | 补充反思结果统一投递与 suppress 行为测试 |

## 6. 上下文预算

| 上下文段 | 预算 |
|----------|------|
| Primary session 摘要 | ~3000 chars |
| 其他近期 session | 2 x 800 = ~1600 chars |
| 任务统计与失败详情 | ~800 chars |
| 近期事件 | ~1000 chars |
| MEMORY.md | ~2000 chars |
| HEARTBEAT.md | ~1500 chars |
| Prompt 指令 | ~800 chars |
| 总计 | ~10,700 chars |

预算目标：单次调用控制在约 3k token 以内。

## 7. 向后兼容

| 场景 | 行为 |
|------|------|
| 纯文本 `HEARTBEAT_OK` | 继续识别为无输出 |
| 旧格式 `{"routines":[...]}` | 兼容解析，视为 `heartbeat_ok=false` |
| `auto_register_routines=True` | 仍由 runtime 侧做 routine apply |
| `auto_register_routines=False` | 仍由 runtime 侧投递 routine proposal，但不再由 Inspector 直接投递 |
| 无 push_message | 仅处理 routines 或返回 `HEARTBEAT_OK` |

## 8. 风险与缓解

| 风险 | 缓解措施 |
|------|----------|
| session 摘要引入噪音 | 先固定为 primary session + 最多 2 个 recent session |
| 变更检测过于敏感 | 保留 force interval，并只对摘要后的结果做哈希 |
| 新 schema 不稳定 | parser 保留旧协议兼容 |
| 推送内容过长 | `output` 仍走现有 suppress 和 summary 截断逻辑 |
| 事件格式不一致 | 读取侧兼容 `event` / `event_type` / `result` 字段 |

## 9. 验证清单

- [ ] `MEMORY.md` 未变但主会话摘要变化时，反思会触发
- [ ] 任务失败或堆积时，即使两个 md 文件未变，反思也会触发
- [ ] `inspect()` 本身不再直接调用 `deposit_mailbox_event`
- [ ] `HeartbeatRunner` 能统一处理 proposal 与 push_message
- [ ] 旧格式 routines JSON 仍能被正确处理
- [ ] 纯文本 `HEARTBEAT_OK` 仍会被 suppress
