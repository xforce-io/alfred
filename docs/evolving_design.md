# Self-Reflection: Agent 持续进化机制

## 1. Background

Alfred 当前缺乏从历史经验中主动学习的能力。Memory 只在会话结束时提取，没有跨会话的整合和修剪；用户提到但未解决的问题会被遗忘。

本设计引入 **Self-Reflection** 机制：一套基于 **Heartbeat + Skill** 的可扩展框架，让 agent 定期扫描历史数据、调用分析 skill、产出可执行的进化动作。

### 设计原则

1. **复用而非新建**：不引入新框架概念，完全复用现有的 heartbeat（调度）+ skill（处理逻辑）
2. **Skill 自治 + 框架兜底**：内置 skill 自己管理 watermark（查 watermark、early return）；外部 LLM skill 无法操作 watermark，由 heartbeat 框架在 skill 成功后自动推进
3. **Scanner 是可选优化**：高频检测场景可配置 scanner 做轻量 gate 预检，默认不需要
4. **单一管控点**：所有执行路径（inline/isolated/unified scheduler）共用同一套 guard 逻辑，不允许旁路
5. **熵减优先**：每个 reflection skill 的净效果必须是收敛的（信息量只减不增或持平）
6. **有界执行**：扫描范围、输出数量、LLM 调用次数都有硬上限
7. **故障透明**：任何步骤失败都不推进 watermark，不丢数据，下次自动重试；失败重试有退避
8. **通知自决**：是否给用户发消息由 skill 自己决定（按需调用 `context.mailbox`），框架不干预

## 2. Architecture

### 2.1 端到端链路

```
默认路径（无 scanner）：
  Heartbeat 按 schedule 调度
    → skill task 到期 → 构建 SkillContext → 调用 skill
      → skill 内部查 watermark → 无变化 → early return
      → skill 内部查 watermark → 有变化 → LLM 分析 → 产出动作

可选路径（配置 scanner，inline 和 isolated 均适用）：
  Heartbeat 按 schedule 调度
    → skill task 到期 → scanner.check(watermark) 轻量预检
      → 无变化 → 记录 skill_skipped 事件，跳过
      → 有变化 → 执行 skill → 成功 → 框架推进 watermark
        （inline: 调用 _invoke_skill_task）
        （isolated: 调用 _execute_isolated_task，独立 job session）

产出动作：
  memory-review: 静默整合 memory，不通知用户
  task-discover: 发现任务 → mailbox.deposit() 通知用户
    → 用户下次对话时看到 "Background Updates" 中的任务提议
    → 用户决定是否让 coding-master 执行（手动触发）
```

**当前闭环策略**：task-discover 的 mailbox 消息中附带可执行的指令模板（如 `$D develop --task "xxx"`），用户复制即可触发 coding-master。不做自动分发，保持人在回路。

### 2.2 inline vs isolated 模式选择

| | inline | isolated |
|---|---|---|
| **session** | 复用主会话 `web_session_{agent}` | 新建独立 job session `job_{task_id}_{uuid}` |
| **上下文** | 共享主会话的历史消息 | 干净的空会话，只有 task description |
| **结果投递** | 直接写入主会话 | 通过 mailbox 投递回主会话 |
| **scanner gate** | 支持 | **⚠️ 仅 Path A 支持，Path B 缺失**（见 §2.5） |
| **watermark 推进** | 框架自动推进 | **⚠️ 仅 Path A 支持，Path B 缺失**（见 §2.5） |
| **适用场景** | 需要对话上下文的轻量确定性任务 | 不需要历史、怕污染主会话的独立任务 |

**内置 reflection skill 选择 inline 的理由**：主干逻辑只需要一次 LLM completion 调用来分析文本，不需要完整的 LLM agent（不需要 tool calling、不需要 workspace instructions）。

**注意**：`memory-review` 的查漏补缺步骤会调用 `process_session_end()`（内部含 LLM 调用），但这是复用已有 pipeline 的尾部操作，不影响主干的 inline 模式决策。为防止阻塞 heartbeat 事件循环，查漏补缺的 session 数量硬上限为 2。

### 2.3 Scanner：可选的变化检测 Gate

Scanner 是**可选**的前置门控组件，适用于需要高频检测但低频执行的场景。不配置 scanner 时，skill 按 `schedule` 到期直接执行，内部自行判断有无工作。

```python
@dataclass
class ScanResult:
    has_changes: bool           # 是否有实质性变化
    change_summary: str         # 简短描述（用于事件日志）
    payload: Any = None         # 实际数据，仅 has_changes=True 时有值

class BaseScanner:
    """所有 scanner 的基类。"""
    def check(self, watermark: str, agent_name: str = "") -> ScanResult:
        """轻量预检，不做重计算。"""
        raise NotImplementedError
```

**何时该配置 scanner**：检测频率 >> 执行频率，且检测成本 << 执行成本（10x 以上差距）。例如秒级文件变更检测 + 分钟级构建，或持续日志监控 + 仅异常时触发分析。

**当前阶段**：memory-review 和 task-discover 都是小时级批处理，不需要 scanner gate，skill 内部 early return 即可。Scanner 仅作为工具库供 skill 调用。  
**补充**：外部 LLM skill 即使不配置 scanner，也必须能推进 watermark（由框架按执行成功后统一推进），避免重复全量扫描。

### 2.4 Heartbeat 执行扩展

在现有 heartbeat 执行路径中增加 **skill-invocation** 分支。Task 新增可选字段 `skill` 和 `scanner`：

```python
# Scanner gate（inline 和 isolated 共用逻辑）
if task.skill and task.scanner:
    scanner = self._get_scanner(task.scanner)
    if scanner:
        state = ReflectionState.load(self._get_workspace_path())
        scan_result = scanner.check(state.get_watermark(task.skill), self.agent_name)
        if not scan_result.has_changes:
            self._write_heartbeat_event("skill_skipped", ...)
            continue  # 跳过执行

# inline 路径
if task.skill:
    result = await self._invoke_skill_task(task, scan_result)
    if self._should_framework_advance_watermark(task):
        self._advance_skill_watermark(task.skill, scan_result)  # 框架推进 watermark（无 scanner 时回退到执行时间）
    continue

# isolated 路径（scanner gate 已在上方完成）
result = await self._execute_isolated_task(task, run_id)
if self._should_framework_advance_watermark(task):
    self._advance_skill_watermark(task.skill, scan_result)      # 框架推进 watermark（无 scanner 时回退到执行时间）
```

**Watermark 推进策略**：
- **单一写者原则（必须）**：每个 skill 只能有一个 watermark 写入责任方，禁止双写。
- **内置 skill**（memory-review, task-discover）：skill 代码内自行调用 `state.set_watermark()` 推进；框架不再推进该 skill watermark。
- **外部 LLM skill**（如 trajectory-reviewer）：由 heartbeat 框架在 skill 成功后推进。若有 scanner payload，候选值取 `payload` 的最新 `updated_at`；无 scanner 时候选值取 `execution_started_at`。
- **防自激**：最终写入值使用 `max(execution_started_at, candidate_watermark)`，确保不被本次结果回写主会话触发下一轮误判。

### 2.5 Heartbeat Task Lifecycle: 架构问题与重构

> **状态**：已确认存在结构性缺陷，需要在下一阶段重构。

#### 2.5.1 问题诊断

Heartbeat 任务执行反复修补仍出问题（scanner gate 失效、自激循环、重试爆发），根因不是个别 bug，而是三个互相叠加的架构缺陷：

**缺陷 1：Guard 逻辑是"路径附加"而非"选择时内建"**

当前存在两条独立的 isolated 执行路径，guard 覆盖不一致：

| | Path A: `_execute_structured_tasks` | Path B: `execute_isolated_claimed_task` |
|---|---|---|
| 入口 | `heartbeat.py` 内部循环 | `heartbeat_tasks.py` mixin，daemon 直接调用 |
| 启用条件 | `include_isolated=True` | 统一调度模式 (`include_isolated=False`) |
| scanner gate | ✅ `heartbeat.py:1182-1207` | ❌ 无 |
| min_interval | ✅ `heartbeat.py:1209-1215` | ❌ 无 |
| watermark 推进 | ✅ `heartbeat.py:1225-1227` | ❌ 无 |
| 认领机制 | `claim_task()` 内部调用 | daemon 外部 `_claim_task()` |

**后果**：每新增一条执行路径，都要手动复制全部 guard 逻辑，漏一个就是 bug。统一调度模式下 isolated 任务完全绕过 scanner gate，即"配了 scanner 仍然持续执行"的根因。

**缺陷 2：状态分裂 + 无事务保证**

任务状态分散在三个互不关联的存储中：

```
HEARTBEAT.md            → 任务调度状态 (state, retry, next_run_at)
.reflection_state.json  → watermark (scanner 去重)
内存 TaskList           → 运行时状态
```

- 三者无事务性：任务标记 DONE 但 watermark 写入失败 → 永不重触发；watermark 更新但任务未持久化 → 重复执行
- `_flush_task_state()` 做 read-modify-write 不持锁，`RoutineManager` 有 fcntl 锁但 `HeartbeatRunner` 不持同一把锁 → commit 8a860ab 修复的 HEARTBEAT.md corruption
- isolated 结果写回主会话（mailbox + history + heartbeat_delivery）→ session.updated_at 推进 → scanner 误判为"有新变化"。Path A 通过执行时间 watermark 防护（`heartbeat.py:1022-1033`），Path B 无此防护

**缺陷 3：失败重试无退避**

`task_manager.py:285-290`：任务失败且 `retry < max_retry` 时，直接设为 PENDING，`next_run_at` 保持 None → `get_due_tasks()` 立即视其为 due → 每个 heartbeat cycle 重新执行。无指数退避、无延迟、`min_execution_interval` 仅作用于 skill 任务不保护通用重试。

#### 2.5.2 重构方向：TaskExecutionGate

**核心思路**：Guard 应该是任务是否 "executable" 的内在属性，在任务选择阶段统一裁决，而不是执行路径上可选的装饰。

**当前架构**（guard 分散在路径上）：

```
get_due_tasks()  →  claim  →  [scanner gate?]  →  [min_interval?]  →  execute
                                ↑ 可能有也可能没有，取决于走哪条路径
```

**目标架构**（guard 内建于选择）：

```
                          ┌─ TaskExecutionGate ──────────────────┐
                          │  schedule check (已有)               │
                          │  + scanner gate                      │
                          │  + min_execution_interval            │
                          │  + retry backoff (2^retry × base)    │
                          │  + watermark 防自激                   │
get_due_tasks(gate) ──────┤                                     │
                          │  输出: 可执行任务 + scan_result 缓存  │
                          └─────────────────────────────────────┘
                                       │
                          claim  →  execute  →  gate.commit(watermark)
```

**关键设计决策**：

1. **`TaskExecutionGate`** 封装所有 guard 判定逻辑（scanner、min_interval、retry backoff），作为 `get_due_tasks()` 的可选参数注入。所有路径（inline/isolated/unified scheduler）通过同一个 gate 实例获取可执行任务
2. **`gate.commit()`** 原子化提交：任务状态 + watermark 在同一把锁保护下写入，消除状态分裂
3. **retry backoff**：`update_task_state(FAILED)` 时设置 `next_run_at = now + min(2^retry × 30s, 1h)`，由 `get_due_tasks` 的 schedule check 自然过滤
4. **mixin → 组合**：`IsolatedTaskMixin` 改为独立的 `IsolatedTaskExecutor`，构造注入 gate 和依赖，消除隐式契约

**影响范围**：

| 文件 | 改动 |
|------|------|
| `src/everbot/core/tasks/task_manager.py` | `update_task_state(FAILED)` 增加退避 `next_run_at`；`get_due_tasks()` 接受可选 `gate` 参数 |
| `src/everbot/core/tasks/execution_gate.py` | **新建**：`TaskExecutionGate` 类，封装 scanner/min_interval/retry backoff |
| `src/everbot/core/runtime/heartbeat.py` | 提取 scanner gate/min_interval 逻辑到 gate，`_execute_structured_tasks` 使用 gate |
| `src/everbot/core/runtime/heartbeat_tasks.py` | `execute_isolated_claimed_task` 增加 gate 校验；或改为组合模式 |
| `src/everbot/cli/daemon.py` | 统一调度路径使用 gate 获取可执行任务 |

### 2.6 SkillContext — Skill 运行时上下文

现有 `RuntimeDeps` 只服务于 `ContextStrategy`（prompt 构建），不满足 skill 的需求。新增 `SkillContext` 封装 skill 所需的全部依赖：

```python
@dataclass
class SkillContext:
    """Skill 运行时上下文，由 HeartbeatRunner._build_skill_context() 构建。"""
    sessions_dir: Path              # UserDataManager.sessions_dir
    workspace_path: Path            # agent workspace 路径（~/.alfred/agents/{agent_name}/）
    agent_name: str                 # 当前 agent 名称（来自 HeartbeatRunner.agent_name）
    memory_manager: MemoryManager   # 记忆管理（含 store）
    mailbox: MailboxPort            # 投递消息给用户
    llm: LLMClient                  # LLM 调用（用 fast model）
    scan_result: ScanResult | None  # scanner gate 预检结果（可选，无 scanner 时为 None）
```

**scan_result 使用约定**：skill 通过 `context.scan_result` 判断是否有 gate 预检结果。有则复用（避免重复查询），无则自己查 watermark。这保证 skill 在有无 scanner 时都能正常工作。

**agent_name 数据流**：`daemon` 启动时从 config 获取 agent 列表 → 每个 agent 创建 `HeartbeatRunner(agent_name=...)` → `_build_skill_context()` 透传 `self.agent_name` → skill 和 scanner 用于 session 过滤（session_id 前缀匹配 `web_session_{agent_name}`）。

**构建方式**（在 `HeartbeatRunner` 中）：

```python
def _build_skill_context(self, scan_result: ScanResult | None = None) -> SkillContext:
    memory_path = self._get_workspace_path() / "MEMORY.md"
    return SkillContext(
        sessions_dir=self.session_manager.persistence.sessions_dir,
        workspace_path=self._get_workspace_path(),
        agent_name=self.agent_name,
        memory_manager=MemoryManager(memory_path),
        mailbox=MailboxAdapter(self.session_manager, self.primary_session_id),
        llm=self._create_fast_llm_client(),
        scan_result=scan_result,
    )
```

`MailboxAdapter` 封装 `session_manager.deposit_mailbox_event()`，对 skill 暴露简单的 `deposit(summary, detail)` 接口。

## 3. Scanners: `src/everbot/core/scanners/`

Scanner 有两种角色：**工具库**（skill 内部调用数据提取方法）和**可选 gate**（heartbeat 调用 `check()` 做预检）。所有 scanner 继承 `BaseScanner`（定义见 2.3）。当前阶段 scanner 主要作为工具库使用。

### 3.1 数据源说明

Session 文件存储在 `~/.alfred/sessions/{session_id}.json`（全局目录，所有 agent 共用），由 `SessionPersistence` 管理。每个 JSON 文件是 `SessionData` 的序列化，关键字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `session_id` | str | 会话唯一标识，前缀含 agent 名 |
| `history_messages` | list[dict] | 对话历史，`{"role", "content"}` |
| `updated_at` | str | ISO timestamp，每次 save 时更新 |
| `session_type` | str | primary, chat, heartbeat, job 等 |

`history_messages` 中 assistant 消息的 `content` 可能是 string 或 list（含 text block 和 tool_use block）。Scanner 只取 text block。

### 3.2 接口定义

```python
@dataclass
class SessionSummary:
    id: str              # session_id
    path: Path           # session JSON 文件路径
    updated_at: str      # ISO timestamp（取自 SessionData.updated_at）
    session_type: str    # primary, job 等

class SessionScanner(BaseScanner):
    def __init__(self, sessions_dir: Path):
        ...

    # — Gate 接口（可选，供 heartbeat gate 调用）—

    def check(self, watermark: str, agent_name: str = "") -> ScanResult:
        """轻量预检：统计 watermark 之后的新 session 数量。
        has_changes = 新 session 数 >= 1。
        payload = list[SessionSummary]（预检时顺便收集，供 skill 复用）。
        """

    # — 数据提取接口（供 skill 直接调用）—

    def get_reviewable_sessions(
        self,
        watermark: str,           # ISO timestamp，只取 updated_at > watermark 的会话
        agent_name: str = "",     # 非空时只扫描该 agent 的 session（按 session_id 前缀匹配）
        max_sessions: int = 5,
        max_age_days: int = 7,
    ) -> list[SessionSummary]:
        """列出待扫描的会话，按 updated_at 升序返回。"""

    def extract_digest(self, session_path: Path, max_messages: int = 40, max_chars: int = 4000) -> str:
        """提取会话的紧凑文本摘要。格式：[user] {text}\n[assistant] {text}\n..."""

    def load_session_messages(self, session_id: str) -> list[dict]:
        """加载完整 history_messages（供 extractor 重新提取）。"""
```

**`check()` 实现说明**：`check()` 内部复用 `get_reviewable_sessions()` 的过滤逻辑，统计数量并收集 summary。结果通过 `ScanResult.payload` 传递给 skill，供 skill 复用以避免重复查询。当不配置 scanner 时，skill 直接调用 `get_reviewable_sessions()` 获取同样的数据。

**过滤规则**：
- 只扫描 `web_session_*` 和 `job_*` 会话，跳过 `heartbeat_session_*` 和 `workflow_*`
- 支持按 `agent_name` 过滤（session_id 前缀匹配），默认扫描所有 agent
- 增量扫描：watermark（ISO timestamp）与 `SessionData.updated_at` 比较
- 按 `SessionData.updated_at` 排序（不用文件 mtime）
- 用 `SessionPersistence.load()` 加载（复用 checksum 校验 + .bak 回退）

**Content 提取**：只取 `role=user` 和 `role=assistant`；assistant content 为 list 时只取 `type=text` block；跳过 `role=tool`。

## 4. Reflection Skills

### 4.1 `skills/memory-review/` — 记忆整合优化

**通知策略**：静默执行，不通知用户。

**职责**：先补后整（先补提遗漏，再整合现有）。先补后整的原因：避免补提的新条目与刚合并的条目重复。

**核心逻辑** (`scripts/review.py`)：

```python
async def run(context: SkillContext) -> str:
    scanner = SessionScanner(context.sessions_dir)
    state = ReflectionState.load(context.workspace_path)
    skill_wm = state.get_watermark("memory-review")

    # 1. 获取待处理 sessions（复用 gate 预检结果或自行查询）
    if context.scan_result and context.scan_result.payload:
        sessions = context.scan_result.payload
    else:
        sessions = scanner.get_reviewable_sessions(skill_wm, agent_name=context.agent_name)
    if not sessions:
        return "No new sessions to review"

    # 2. 逐个提取摘要，跳过失败的 session
    digests, digest_session_ids = [], []
    last_successful_session = None
    for s in sessions:
        try:
            digests.append(scanner.extract_digest(s.path))
            digest_session_ids.append(s.id)
            last_successful_session = s
        except Exception as e:
            logger.warning(f"Failed to extract session {s.id}: {e}, skipping")
            continue

    if not digests:
        return "All sessions failed to extract"

    # 3. 查漏补缺（轻量 prompt，只做遗漏检测）
    reextract_count = 0
    existing = context.memory_manager.load_entries()
    missed = await detect_missed_sessions(context.llm, digests, digest_session_ids, existing)
    for sid in missed.session_ids[:2]:  # 补提上限 2
        try:
            msgs = scanner.load_session_messages(sid)
            await context.memory_manager.process_session_end(msgs, sid)
            reextract_count += 1
        except Exception as e:
            logger.warning(f"Re-extract failed for {sid}: {e}, skipping")

    # 4. 整合分析（单次 LLM 调用）
    existing = context.memory_manager.load_entries()  # 重新加载
    review = await analyze_memory_consolidation(context.llm, digests, existing)

    # 5. 执行整合 + 后置校验
    entries_before = len(existing)
    review_stats = context.memory_manager.apply_review(review)
    entries_after = len(context.memory_manager.load_entries())
    if entries_after > entries_before:
        raise IntegrityError(f"整合阶段不应增加条目: {entries_before} → {entries_after}")

    # 6. Watermark 推进（由 skill 自行管理，gate 只做预检不推进）
    state.set_watermark("memory-review", last_successful_session.updated_at)
    state.save(context.workspace_path)
    return f"Memory review: {review_stats}, re-extracted: {reextract_count}"
```

**LLM 调用拆分**（两次独立调用，职责不混合）：

- **遗漏检测** (`detect_missed_sessions`)：输入 digests + digest_session_ids + existing memories → 输出 `{"session_ids": [...]}`。注意需要传入 `digest_session_ids` 让 LLM 能引用具体 session。
- **整合优化** (`analyze_memory_consolidation`)：输入 digests + existing memories → 输出 `{"merge_pairs": [...], "deprecate_ids": [...], "reinforce_ids": [...], "refined_entries": [...]}`。约束：`merge_count + deprecate_count >= reinforce_count`。

**熵控**：
- merge：新建一条 entry（`score=max(a,b)`, `activation_count=sum(a,b)`），删除原两条
- deprecate：`entry.score *= 0.3`（加速自然衰减，几天内被 purge 清除）
- refine：原地更新 content，不改变 score/count
- 补提上限：每次最多 2 个 session；补提失败不影响整合阶段
- Memory 总量软上限：active 条目 > 200 时整合阶段采用更激进的合并策略
- 后置校验：`if entries_after > entries_before: raise IntegrityError(...)`

### 4.2 `skills/task-discover/` — 任务发现

**通知策略**：发现新任务时通过 mailbox 通知用户（含可执行指令模板）；未发现则静默。

**核心逻辑** (`scripts/discover.py`)：

```python
async def run(context: SkillContext) -> str:
    scanner = SessionScanner(context.sessions_dir)
    state = ReflectionState.load(context.workspace_path)
    skill_wm = state.get_watermark("task-discover")

    # 获取待处理 sessions（复用 gate 预检结果或自行查询）
    if context.scan_result and context.scan_result.payload:
        sessions = context.scan_result.payload
    else:
        sessions = scanner.get_reviewable_sessions(skill_wm, agent_name=context.agent_name)
    if not sessions:
        return "No new sessions"

    digests = []
    last_successful_session = None
    for s in sessions:
        try:
            digests.append(scanner.extract_digest(s.path))
            last_successful_session = s
        except Exception as e:
            logger.warning(f"Failed to extract session {s.id}: {e}, skipping")
            continue

    if not digests:
        return "All sessions failed to extract"

    # LLM 分析
    task_state = TaskDiscoverState.load(context.workspace_path)
    existing_titles = [t.title for t in task_state.pending_tasks if not t.expired]
    new_tasks = await discover_tasks(context.llm, digests, existing_titles)

    # 清理过期 + 追加新任务 + 硬上限
    task_state.pending_tasks = [t for t in task_state.pending_tasks if not t.expired]
    if new_tasks:
        task_state.pending_tasks = (task_state.pending_tasks + new_tasks)[:3]
        await context.mailbox.deposit(
            summary=f"发现 {len(new_tasks)} 个待办任务",
            detail=format_task_proposals(new_tasks),  # 含 $D develop 指令模板
        )
    task_state.save(context.workspace_path)

    state.set_watermark("task-discover", last_successful_session.updated_at)
    state.save(context.workspace_path)
    return f"Discovered {len(new_tasks)} tasks"
```

**熵控**：`pending_tasks` 硬上限 3 条；Jaccard token similarity >= 0.5 去重；7 天自动过期。

## 5. State Management

### 5.1 `ReflectionState` — 共享 watermark

```python
@dataclass
class ReflectionState:
    """持久化为 {workspace_path}/.reflection_state.json。原子写入。"""
    watermarks: dict[str, str] = field(default_factory=dict)  # skill_name → ISO timestamp

    def get_watermark(self, skill_name: str) -> str: ...
    def set_watermark(self, skill_name: str, value: str) -> None: ...
    @classmethod
    def load(cls, workspace_path: Path) -> "ReflectionState": ...
    def save(self, workspace_path: Path) -> None: ...
```

每个 skill 维护独立 watermark，互不影响。Watermark 本质是 **per-skill 的数据游标**，记录"该 skill 上次处理到了哪个时间点的数据"（类似 Kafka consumer offset）。推进方式有两条路径：内置 skill 在代码中自行推进；外部 LLM skill 由 heartbeat 框架在执行成功后自动推进。

**⚠️ 当前局限**：watermark 与任务状态分别存储在 `.reflection_state.json` 和 `HEARTBEAT.md` 中，两者的写入没有事务保证。重构目标是通过 `TaskExecutionGate.commit()` 在同一把锁下原子化提交（见 §2.5.2）。

**防自激策略**：`_advance_skill_watermark()` 在框架推进场景下统一使用 `max(execution_started_at, candidate_watermark)`；其中 `candidate_watermark` 来自 scanner payload（若存在）或执行时间回退值。此规则需在 Path A/Path B 共享实现，避免统一调度旁路。

### 5.2 `TaskDiscoverState` — task-discover 领域状态

```python
@dataclass
class DiscoveredTask:
    title: str
    description: str
    urgency: str              # high | medium | low
    source_session_id: str
    discovered_at: str        # ISO timestamp
    expires_at: str           # discovered_at + 7 days

    @property
    def expired(self) -> bool:
        return datetime.fromisoformat(self.expires_at) < datetime.now(timezone.utc)

@dataclass
class TaskDiscoverState:
    """持久化为 {workspace_path}/.task_discover_state.json。原子写入。"""
    pending_tasks: list[DiscoveredTask] = field(default_factory=list)
    @classmethod
    def load(cls, workspace_path: Path) -> "TaskDiscoverState": ...
    def save(self, workspace_path: Path) -> None: ...
```

## 6. Modified Files

| 文件 | 改动 |
|------|------|
| `src/everbot/core/tasks/execution_gate.py` | **新建**：`TaskExecutionGate`，统一 scanner/min_interval/retry backoff 判定 |
| `src/everbot/core/tasks/task_manager.py` | Task dataclass 新增 `skill`、`scanner`、`min_execution_interval` 字段；`update_task_state(FAILED)` 增加 retry backoff `next_run_at` |
| `src/everbot/core/runtime/heartbeat.py` | 新增 `_invoke_skill_task()`、`_build_skill_context()`、`_advance_skill_watermark()`；scanner gate/min_interval 逻辑迁移到 `TaskExecutionGate` |
| `src/everbot/core/runtime/heartbeat_tasks.py` | `execute_isolated_claimed_task` 接入 `TaskExecutionGate`；长期改为组合模式替代 mixin |
| `src/everbot/core/runtime/skill_context.py` | **新建**：`SkillContext`、`MailboxAdapter`、`LLMClient` Protocol |
| `src/everbot/core/memory/merger.py` | 新增 `merge_entries(a, b, merged_content) → MemoryEntry` |
| `src/everbot/core/memory/manager.py` | 新增 `apply_review(result) → dict`，文件锁内完成读写 |
| `src/everbot/core/scanners/base.py` | **新建**：`BaseScanner`、`ScanResult` 定义 |
| `src/everbot/core/scanners/session_scanner.py` | **新建**：`SessionScanner(BaseScanner)` |
| `skills/memory-review/` | **新建** skill |
| `skills/task-discover/` | **新建** skill |

## 7. Heartbeat 任务注册

```json
{
  "id": "reflection_memory_review",
  "title": "记忆整合优化",
  "schedule": "2h",
  "skill": "memory-review",
  "execution_mode": "inline",
  "timeout_seconds": 120,
  "source": "system"
},
{
  "id": "reflection_task_discover",
  "title": "任务发现",
  "schedule": "2h",
  "skill": "task-discover",
  "execution_mode": "inline",
  "timeout_seconds": 120,
  "source": "system"
}
```

**字段说明**：
- `schedule`：skill 的调度频率（复用现有 heartbeat 调度机制）
- `skill`：skill 模块名，heartbeat 识别后走 skill 调用路径
- `scanner`（可选）：关联的 scanner 类型名（"session"、"trajectory" 等）。配置后 heartbeat 先执行 scanner gate 预检，无变化时跳过 skill 执行。**适用场景**：检测频率 >> 执行频率的高频监控任务
- `min_execution_interval`（可选）：搭配 scanner 使用，防止 gate 通过后 skill 执行过于频繁

自注册防重复：heartbeat 启动时按 `id` 查重，存在则跳过。

## 8. Entropy Budget

| 维度 | 上限 |
|------|------|
| Memory 条目（整合阶段） | 只减不增（IntegrityError 校验） |
| Memory 条目（补提阶段） | 每次最多 2 session，经 dedup pipeline |
| Memory 总量 | 软上限 200 active 条目 |
| Memory 衰减 | `0.99^days` + purge at `score < 0.05` |
| Discovered tasks | 硬上限 3 条 + 7 天过期 |
| 扫描范围 | 5 sessions × 40 msgs × 4000 chars ≈ 20K chars/cycle |
| LLM 调用 | memory-review 2 次 + task-discover 1 次，受 schedule 节流 |

## 9. Observability

所有 scanner 和 skill 的关键事件统一写入 `~/.alfred/logs/heartbeat_events.jsonl`（复用现有 `_write_heartbeat_event()` 基础设施），不单独开文件。

### 9.1 事件类型

| 事件 | 字段 | 说明 |
|------|------|------|
| `skill_started` | `skill`, `scan_summary` | skill 开始执行 |
| `skill_completed` | `skill`, `duration_ms`, `result` | skill 成功完成 |
| `skill_failed` | `skill`, `duration_ms`, `error` | skill 执行失败 |
| `scanner_check` | `scanner`, `has_changes`, `change_summary` | scanner gate 预检结果（仅配置 scanner 时） |
| `skill_skipped` | `skill`, `reason` (`no_changes` / `interval_not_met`) | skill 被 gate 跳过（仅配置 scanner 时） |
| `scanner_error` | `scanner`, `error` | scanner check 异常（仅配置 scanner 时） |

### 9.2 事件格式

```json
{"timestamp": "2026-03-04T10:30:00", "agent": "coding-master", "event": "scanner_check", "scanner": "session", "has_changes": true, "new_count": 3, "change_summary": "3 new sessions since last scan"}
{"timestamp": "2026-03-04T10:30:01", "agent": "coding-master", "event": "skill_started", "skill": "memory-review", "scan_summary": "3 new sessions"}
{"timestamp": "2026-03-04T10:30:15", "agent": "coding-master", "event": "skill_completed", "skill": "memory-review", "duration_ms": 14200, "result": "merged 2, deprecated 1, re-extracted 1"}
```

**设计决策**：事件和日志记在一起（同一个 jsonl 文件），不分开。分开记的问题是关联分析困难（要跨文件 join）、时序不连续。用 `event` 字段区分类型即可。

## 10. Error Handling

| 故障场景 | 行为 | watermark |
|---------|------|-----------|
| Session 文件损坏 / 读取失败 | `continue` 跳过，处理后续；持续损坏的通过 `max_age_days` 自然过期 | 推进到最后成功的 |
| LLM 调用失败（rate limit / timeout） | 记录 warn，整个 skill 返回 error | 不推进 |
| LLM 返回格式错误（JSON parse 失败） | 记录 error + 原始响应，skill 返回 error | 不推进 |
| 补提阶段 `process_session_end` 失败 | 记录 warn，跳过该 session，不影响整合阶段 | 正常推进 |
| `apply_review` 引用了不存在的 memory id | 跳过该操作，记录 warn，继续执行其余操作 | 正常推进 |
| IntegrityError（整合后条目增加） | 回滚本次整合，记录 error + LLM 原始 JSON | 不推进 |
| Memory store 并发写入冲突 | `fcntl.flock` 串行化，`entries_before` 在锁内读取 | - |
| Skill 整体超时（>120s） | heartbeat 捕获 TimeoutError，标记 task FAILED | 不推进 |
| `SkillContext` 构建失败 | heartbeat 记录 error，task 标记 FAILED | 不推进 |
| State 文件损坏（.reflection_state.json） | `load()` 返回空 state（watermark 全部重置为 ""），等价于首次运行全量扫描 | 从零开始 |
| Mailbox 投递失败 | 记录 warn，不影响 skill 结果和 watermark 推进 | 正常推进 |

| Scanner check 异常（仅配置 scanner 时） | 记录 `scanner_error` 事件，跳过本次 skill 执行 | 不推进 |

**核心不变量**：只有 skill 完整执行成功时才推进 watermark（补提失败、mailbox 失败等非关键路径除外）。

## 11. Implementation Sequence

| 阶段 | 内容 | 依赖 | 状态 |
|------|------|------|------|
| **0** | **`TaskExecutionGate` + retry backoff + 状态原子化**（§2.5.2） | task_manager | ⬜ 待实现 |
| 1 | `SkillContext` + `MailboxAdapter` | ports.py, MemoryManager | ✅ 已完成 |
| 2 | `ReflectionState` | 无 | ✅ 已完成 |
| 3 | `BaseScanner` + `ScanResult` + `SessionScanner` | SessionPersistence | ✅ 已完成 |
| 4 | Task 新增 `skill` 字段 + Heartbeat `_invoke_skill_task()` | SkillContext | ✅ 已完成 |
| 5 | `merger.merge_entries()` + `manager.apply_review()` | 无 | ⬜ |
| 6 | `skills/memory-review/` | scanner（工具库）, memory, SkillContext | ⬜ |
| 7 | `skills/task-discover/` + `TaskDiscoverState` | scanner（工具库）, mailbox, SkillContext | ⬜ |
| 8 | scanner gate 统一到 `TaskExecutionGate` | 阶段 0 | ⬜ |
| 9 | Observability 事件埋点 | 阶段 4 | ⬜ |
| 10 | HEARTBEAT.md 自注册 | 阶段 4-7 | ⬜ |

> **阶段 0 优先级最高**：当前 scanner gate / retry backoff 的结构性缺陷会影响所有后续 skill 的正确运行。在 gate 统一前，新增 skill 仍可能因路径旁路而失控。

## 12. Verification

1. **Skill 自治（无 scanner）**：skill 内部查 watermark，无变化时 early return，不调用 LLM
2. **Skill + scanner gate**：配置 scanner 时，gate 无变化 → skill 不执行；有变化 → scan_result 正确传递给 skill
3. **Scanner 过滤**：边界控制、agent_name 过滤、content 提取（list 格式 assistant content）、session 类型过滤
4. **SkillContext**：构建成功、各依赖可用、scan_result 为 None 时 skill 正常工作
5. **Skill 单测**：mock LLM + mock store，验证正常路径和各异常路径
6. **熵不变量**：`apply_review()` 在各种 LLM 返回下满足 `entries_after <= entries_before`
7. **错误恢复**：LLM 失败 → watermark 未推进 → 下次重试成功
8. **Session 跳过**：中间 session 损坏 → 后续正常处理 + watermark 正确推进
9. **并发安全**：并发 `apply_review()` 和 `process_session_end()` → 文件锁生效
10. **可观测性**：skill_started、skill_completed 事件正确写入 jsonl；配置 scanner 时 scanner_check、skill_skipped 事件正确写入
11. **端到端**：daemon 启动 → heartbeat 调度 → skill 执行 → MEMORY.md 变化 + mailbox 任务提议
12. **路径等价性（阻断项）**：`include_isolated=True`（Path A）与统一调度 `include_isolated=False`（Path B）在 scanner/min_interval/retry backoff/watermark 提交上的行为一致（同一输入同一输出）。
