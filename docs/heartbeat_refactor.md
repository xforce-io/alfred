# Heartbeat 拆分重构：Inspector + Cron

## 1. 背景与动机

当前 `HeartbeatRunner`（heartbeat.py，1625 行）承担了两类本质不同的职责：

| 职责 | 触发模型 | 典型频率 | 需要 LLM |
|------|---------|---------|----------|
| **巡查发现**：扫描上下文、发现重复意图、提议新 routine | 周期 + 事件驱动 | 低频（1h 周期 / 文件变更提前触发） | 是 |
| **定时执行**：按 schedule 调度 due tasks、管理状态机 | 定时器驱动 | 高频（分钟级） | 部分（skill/deterministic 不需要） |

**核心问题**：两种本质不同的职责揉在同一个 `HeartbeatRunner` 里：

1. **架构耦合** — mode dispatch（`structured_due` vs `structured_reflect`）本质是两个独立流程的 if-else，触发模型、执行路径、资源需求完全不同
2. **不必要的 LLM 开销** — skill/deterministic 任务不需要 LLM，但与 reflection 共享同一套 session 初始化路径
3. **可测试性差** — 测 cron 执行要 mock reflection，反之亦然
4. **扩展受限** — 增加新的调度能力或新的巡查策略，都要改动同一个 1600+ 行的文件

## 2. 目标架构

### 逻辑分层架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Daemon (daemon.py)                          │
│                     进程生命周期 · 信号处理                           │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ start / stop
┌──────────────────────────▼──────────────────────────────────────────┐
│                     Scheduler (scheduler.py)                       │
│              统一 tick loop · 调度决策 · 背压控制                     │
│                                                                    │
│   ┌─ inspector tick ──────────────┐  ┌─ cron tick ───────────────┐ │
│   │  低频：1h 周期 / 文件变更触发   │  │  高频：每分钟              │ │
│   │  active-hours gate            │  │  active-hours gate        │ │
│   │  idle-cooldown gate           │  │  idle-cooldown gate       │ │
│   │                               │  │   (仅 LLM 任务;           │ │
│   │                               │  │    skill/determ. 免检)    │ │
│   │                               │  │  flock 互斥               │ │
│   └───────────┬───────────────────┘  └───────────┬───────────────┘ │
└───────────────┼──────────────────────────────────┼─────────────────┘
                │                                  │
┌───────────────▼───────────────┐  ┌───────────────▼─────────────────┐
│        Inspector 层            │  │          Cron 层                │
│       (inspector.py)          │  │         (cron.py)               │
│                               │  │                                 │
│  输入：                        │  │  RoutineManager                 │
│   · MEMORY.md                 │  │   .claim() → .gate_check()      │
│   · session 上下文摘要         │  │       │                         │
│   · 任务执行统计               │  │  ┌────▼────┬──────────┬──────┐  │
│   · 现有 routine 列表          │  │  │ Skill   │ Determ.  │ LLM  │  │
│                               │  │  │ (零LLM) │ (零LLM)  │Agent │  │
│  处理：                        │  │  └────┬────┴─────┬────┴──┬───┘  │
│   · ReflectionManager (LLM)   │  │       └──────────┼───────┘      │
│   · 提议 → 审批 / 自动注册     │  │                  │              │
│                               │  │  RoutineManager                 │
│  输出：                        │  │   .update_state() / .flush()    │
│   · RoutineManager            │  │       │                         │
│     .add / .update / .remove  │  │  CronDelivery                   │
│                               │  │   · suppress / mailbox / push   │
└───────────────┬───────────────┘  └───────────────┬─────────────────┘
                │                                  │
                │  via RoutineManager          via RoutineManager
                ▼                                  ▼
┌───────────────────────────────────────────────────────────────────┐
│                      数据层 (共享状态)                              │
│                                                                   │
│   HEARTBEAT.md          heartbeat_events.jsonl     Session Store   │
│   (task store,          (事件日志,                 (session 持久化, │
│    HeartbeatFileManager  source: cron|inspector)   SessionManager) │
│    读写 + flock)                                                   │
│                                                                   │
│   MEMORY.md             .heartbeat_snapshot.json                  │
│   (长期记忆,             (task 快照,                               │
│    Inspector 只读)        crash recovery)                          │
└───────────────────────────────────────────────────────────────────┘
```

### 组件交互总览

```
┌─────────────────────────────────────────────┐
│           Daemon / Scheduler                │
│     (统一定时器基座，asyncio tick loop)       │
└──────────┬────────────────────┬─────────────┘
           │                    │
     ┌─────▼──────┐      ┌─────▼──────┐
     │  Inspector  │      │    Cron     │
     │  (巡查员)    │      │  (调度器)   │
     └─────┬──────┘      └─────┬──────┘
           │                    │
           │ register/          │ claim → gate →
           │ update/remove      │ execute → update_state
           │                    │
           └────────┬───────────┘
                    ▼
             RoutineManager
          (HEARTBEAT.md 唯一写入者)
```

### 2.1 职责边界

**Inspector（巡查员）— "观察 → 决策 → 注册"**

- 输入源：
  - MEMORY.md（用户长期意图）
  - 当前 session 上下文（最近对话、活跃意图）
  - 现有任务状态（反复失败的 pattern、缺失的 routine）
- 输出：通过 RoutineManager 注册/修改/删除 cron 任务
- **自己不执行任何业务任务**
- 触发条件：默认 1h 周期 / 文件 hash 变化提前触发 / daemon 启动时

**Cron（调度器）— "定时 → 执行 → 交付"**

- 纯定时器驱动，每分钟 tick 检查 due tasks
- 执行流程：claim → gate check → execute → state update → deliver
- 执行路径：
  - Skill 任务：直接调用 skill module，零 LLM 开销
  - Deterministic 任务：纯计算，无 agent
  - LLM 任务：按需创建 agent session
- 门控策略：
  - active-hours gate：所有任务统一受限
  - idle-cooldown gate：**仅 LLM 任务**受限（避免打扰活跃用户）；skill/deterministic 任务静默执行，免检
  - flock 互斥：防止与 chat session 并发写
- 自愈：stuck task 恢复、failed task 重试
- **不关心任务从哪里来，只管按 schedule 执行**

### 2.2 数据流

```
Session 上下文 ──┐
MEMORY.md ───────┼──→ Inspector ──→ RoutineManager.add/update/remove()
任务执行状态 ────┘                          │
                                            ▼
                                     HEARTBEAT.md ◄── RoutineManager.update_state/flush()
                                            │                          ▲
                        RoutineManager ─────┘                          │
                         .get_due_tasks()                              │
                                │                                      │
                    ┌───────────┼───────────┐                          │
                    ▼           ▼           ▼                          │
                  Skill    Deterministic   LLM Agent                   │
                    │           │           │                          │
                    └───────────┼───────────┘                          │
                                │                                      │
                         Cron ──┴──────────────────────────────────────┘
                          · execute → update_state via RoutineManager
                          · CronDelivery (mailbox / history / push)
```

## 3. 模块设计

### 3.1 新文件结构

```
src/everbot/core/runtime/
├── inspector.py          # NEW: 巡查员 (从 heartbeat.py reflection 逻辑抽出)
├── cron.py               # NEW: Cron 调度执行器 (从 heartbeat.py 任务执行逻辑抽出)
├── cron_delivery.py      # NEW: 执行结果投递 (从 heartbeat.py delivery 逻辑抽出)
├── scheduler.py          # MODIFY: 注册 inspector tick + cron tick
├── heartbeat.py          # DEPRECATE → 瘦身为兼容入口，委托给 inspector + cron
├── heartbeat_file.py     # KEEP: HEARTBEAT.md I/O (RoutineManager 内部依赖)
├── heartbeat_tasks.py    # MOVE → 合并入 cron.py
├── heartbeat_utils.py    # KEEP: 工具函数
├── reflection.py         # MOVE → 合并入 inspector.py
├── ...
```

### 3.2 Inspector 接口

```python
# inspector.py

class Inspector:
    """巡查员：观察上下文，管理 routine 生命周期。

    不执行任何业务任务，只通过 RoutineManager 注册/修改/删除 cron 任务。
    """

    def __init__(
        self,
        *,
        agent_name: str,
        workspace_path: Path,
        session_manager: Any,
        agent_factory: Any,
        routine_manager: RoutineManager,
        # 触发控制
        inspect_interval: timedelta = timedelta(hours=1),
        # LLM 配置
        auto_register_routines: bool = False,
    ): ...

    async def inspect(self, context: InspectionContext) -> InspectionResult:
        """执行一次巡查。

        Args:
            context: 巡查上下文，包含 session 摘要、任务执行状态等

        Returns:
            InspectionResult: 巡查结果，包含提议的 routine 变更
        """
        ...

    def should_skip(self) -> bool:
        """基于文件 hash 和时间间隔判断是否可以跳过。"""
        ...


@dataclass
class InspectionContext:
    """巡查员的输入上下文。"""
    memory_content: Optional[str]           # MEMORY.md 内容
    session_summary: Optional[str]          # 最近 session 摘要
    task_execution_stats: Dict[str, Any]    # 任务执行统计（失败率、频次等）
    existing_routines: List[Task]           # 现有 routine 列表


@dataclass
class InspectionResult:
    """巡查结果。"""
    proposals: List[RoutineProposal]        # 新增/修改/删除提议
    skipped: bool = False                   # 是否跳过了巡查
    skip_reason: Optional[str] = None
```

### 3.3 Cron 接口

```python
# cron.py

class CronExecutor:
    """Cron 调度执行器：按 schedule 执行 due tasks。

    纯执行器，不关心任务来源，只管按时执行。
    """

    def __init__(
        self,
        *,
        agent_name: str,
        workspace_path: Path,
        session_manager: Any,
        agent_factory: Any,
        routine_manager: RoutineManager,
        execution_gate: TaskExecutionGate,
        delivery: CronDelivery,
    ): ...

    async def tick(self) -> CronTickResult:
        """执行一次 cron tick：检查并执行所有 due tasks。"""
        ...

    async def execute_task(self, task: Task, run_id: str) -> TaskResult:
        """执行单个任务（根据类型分发到不同执行路径）。"""
        ...


@dataclass
class CronTickResult:
    """一次 tick 的执行摘要。"""
    executed: int
    skipped: int
    failed: int
    results: List[TaskResult]


@dataclass
class TaskResult:
    """单个任务的执行结果。"""
    task_id: str
    status: str                  # "done" | "failed" | "skipped" | "timeout"
    output: Optional[str]
    error: Optional[str] = None
    execution_path: str = ""     # "skill" | "deterministic" | "llm_inline" | "llm_isolated"
```

### 3.4 Scheduler 变更

```python
# scheduler.py (修改)

class Scheduler:
    """统一调度器：管理 inspector 和 cron 的 tick 节奏。"""

    def __init__(self, ...):
        # 现有字段 ...
        self._inspector_schedules: Dict[str, InspectorSchedule] = {}
        self._cron_tick_interval_seconds: float = 60.0  # cron 每分钟 tick

    async def tick(self, now=None):
        ts = now or datetime.now()
        # Phase 1: Cron tick（高频，每分钟）
        await self._tick_cron(ts)
        # Phase 2: Inspector tick（低频，按需）
        await self._tick_inspector(ts)

    async def _tick_cron(self, ts: datetime):
        """检查 due tasks 并执行。"""
        ...

    async def _tick_inspector(self, ts: datetime):
        """检查是否需要巡查，如需要则触发。"""
        ...
```

### 3.5 CronDelivery（结果投递）

```python
# cron_delivery.py

class CronDelivery:
    """Cron 执行结果的投递策略。"""

    def __init__(
        self,
        *,
        session_manager: Any,
        primary_session_id: str,
        ack_max_chars: int = 300,
        broadcast_scope: str = "agent",
        realtime_push: bool = True,
    ): ...

    async def deliver(self, result: TaskResult, run_id: str) -> bool:
        """投递单个任务结果，返回是否实际投递。"""
        ...

    def should_suppress(self, output: str) -> bool:
        """判断是否抑制投递（HEARTBEAT_OK 逻辑）。"""
        ...
```

## 4. 迁移策略：渐进式三阶段

### Phase 1: 抽出 Cron（最大收益，最小风险）

**改动范围**：

| 操作 | 文件 | 说明 |
|------|------|------|
| CREATE | `cron.py` | 从 `heartbeat.py` 抽出 `_execute_structured_tasks()` 及相关方法 |
| CREATE | `cron_delivery.py` | 从 `heartbeat.py` 抽出 `_should_deliver()`, `_inject_result_to_primary_history()`, `_deposit_deliver_event()` |
| MODIFY | `heartbeat.py` | 删除已迁移的方法，`_execute_once()` 中 `structured_due` 分支委托给 `CronExecutor` |
| MODIFY | `scheduler.py` | 新增 `_tick_cron()` 分支，cron tick 独立于 heartbeat tick |
| MOVE | `heartbeat_tasks.py` | `IsolatedTaskMixin` 合并入 `cron.py` |

**验证**：
- 现有 HEARTBEAT.md 格式不变
- 所有 skill/deterministic/LLM 任务执行路径行为不变
- 结果投递行为不变

### Phase 2: 抽出 Inspector

**改动范围**：

| 操作 | 文件 | 说明 |
|------|------|------|
| CREATE | `inspector.py` | 从 `heartbeat.py` 抽出 `structured_reflect` 分支 + reflection 逻辑 |
| MODIFY | `heartbeat.py` | 删除 reflection 相关方法，瘦身为兼容入口 |
| MODIFY | `scheduler.py` | 新增 `_tick_inspector()` 分支 |
| MODIFY | `reflection.py` | 合并入 `inspector.py` 或保留为 inspector 的内部模块 |
| MODIFY | `inspector.py` | 扩展 `InspectionContext`，接入 session 上下文和任务执行统计 |

**新增能力**：
- Inspector 读取最近 session 对话，识别用户意图中的重复 pattern
- Inspector 分析任务执行统计（哪些任务反复失败、执行频率是否合理）
- Inspector 可以修改/删除已有 routine（不只是新增）

### Phase 3: 瘦身 HeartbeatRunner

**改动范围**：

| 操作 | 文件 | 说明 |
|------|------|------|
| MODIFY | `heartbeat.py` | 降级为薄包装层，仅保留锁管理和向后兼容入口 |
| MODIFY | `daemon.py` | 直接使用 `CronExecutor` + `Inspector`，不再依赖 `HeartbeatRunner` |
| DELETE | `heartbeat_tasks.py` | 已合并入 `cron.py` |

最终 `heartbeat.py` 只保留：
- 锁管理（cross-process flock + in-process semaphore）
- Session 生命周期（create/save/archive）
- 向后兼容入口（`run_once()` 委托给 cron + inspector）

## 5. 关键设计决策

### 5.1 共享状态：HEARTBEAT.md 并发访问

Inspector 和 Cron 都读写 HEARTBEAT.md，需要防止竞态。

**方案**：沿用现有 file lock 机制
- Cron tick 持有 flock 期间通过 RoutineManager 读取 due tasks、更新 task state
- Inspector 持有 flock 期间通过 RoutineManager 注册/修改/删除 routine
- 两者不会同时运行（Scheduler 串行调度）

**未来演进**：如果需要并发，可以迁移到 SQLite（单文件数据库，支持 WAL 模式并发读写）。

### 5.2 Inspector 的输入上下文

Inspector 需要比当前 ReflectionManager 更丰富的上下文：

```python
# 构建 InspectionContext 的伪代码
context = InspectionContext(
    memory_content=read_memory_md(),
    session_summary=session_manager.get_recent_summary(agent_name, max_turns=20),
    task_execution_stats=cron.get_execution_stats(),  # 从 heartbeat_events.jsonl 聚合
    existing_routines=routine_manager.list_routines(),
)
```

### 5.3 Cron tick 频率

| 任务类型 | 当前 | 重构后 |
|---------|------|--------|
| Skill 任务 | 30 分钟检查一次 | 每分钟检查 due |
| Deterministic | 30 分钟 | 每分钟 |
| LLM inline | 30 分钟 | 每分钟（但受 idle_cooldown 约束）|
| LLM isolated | 30 分钟 | 每分钟（独立 session）|
| Inspector | 30 分钟（常跳过）| 独立 1h 周期（文件变更可提前触发）|

Cron tick 本身极轻量（读 HEARTBEAT.md + 比较时间），每分钟一次无性能压力。

### 5.4 事件日志

两者共享 `heartbeat_events.jsonl`，通过 `source` 字段区分：
- `source: "cron"` — 任务执行事件
- `source: "inspector"` — 巡查事件

## 6. 收益总结

| 维度 | 现状 | 重构后 |
|------|------|--------|
| 调度精度 | 30 分钟统一 tick | Cron 分钟级，Inspector 1h 周期 |
| LLM 开销 | 每次 tick 都可能初始化 session | Skill/deterministic 零 LLM |
| 代码规模 | heartbeat.py 1625 行 | cron.py ~500 行 + inspector.py ~300 行 |
| 可测试性 | 测 cron 要 mock reflection | 各自独立测试 |
| 扩展性 | 改调度影响巡查 | 互不影响 |
| 职责清晰度 | mode dispatch if-else | Inspector 只管发现，Cron 只管执行 |
