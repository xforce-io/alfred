# Self-Reflection: Agent 持续进化机制

## 1. Background

Alfred 当前缺乏从历史经验中主动学习的能力。Memory 只在会话结束时提取，没有跨会话的整合和修剪；用户提到但未解决的问题会被遗忘。

本设计引入 **Self-Reflection** 机制：一套基于 **Heartbeat + Skill + Scanner** 的可扩展框架，让 agent 定期扫描历史数据、调用分析 skill、产出可执行的进化动作。

### 设计原则

1. **复用而非新建**：不引入新框架概念，完全复用现有的 heartbeat（调度）+ skill（处理逻辑）
2. **Scanner 是工具库**：scanner 只是 skill 内部使用的数据源适配器，不是框架组件
3. **熵减优先**：每个 reflection skill 的净效果必须是收敛的（信息量只减不增或持平）
4. **有界执行**：扫描范围、输出数量、LLM 调用次数都有硬上限
5. **故障透明**：任何步骤失败都不推进 watermark，不丢数据，下次自动重试
6. **通知自决**：是否给用户发消息由 skill 自己决定（按需调用 `context.mailbox`），框架不干预

## 2. Architecture

### 2.1 端到端链路

```
Heartbeat 定时触发
  → _invoke_skill_task(task) 构建 SkillContext 并调用 skill
    → skill 内部使用 SessionScanner 扫描指定 agent 的 session
    → LLM 分析
    → 产出动作：
        memory-review: 静默整合 memory，不通知用户
        task-discover: 发现任务 → mailbox.deposit() 通知用户
          → 用户下次对话时看到 "Background Updates" 中的任务提议
          → 用户决定是否让 coding-master 执行（手动触发）
```

**当前闭环策略**：task-discover 的 mailbox 消息中附带可执行的指令模板（如 `$D develop --task "xxx"`），用户复制即可触发 coding-master。不做自动分发，保持人在回路。

### 2.2 关键设计决策：为什么不用 isolated 模式？

Reflection skill 的主干逻辑只需要一次 LLM completion 调用来分析文本，不需要完整的 LLM agent（不需要 tool calling、不需要 workspace instructions）。用 inline async-deterministic 模式：
- 避免创建独立 session 和 agent（减少资源开销）
- 精确控制 LLM prompt（不经过 ContextStrategy 管道）
- 保持确定性的流程控制（LLM 只负责分析，不负责执行）

**注意**：`memory-review` 的查漏补缺步骤会调用 `process_session_end()`（内部含 LLM 调用），但这是复用已有 pipeline 的尾部操作，不影响主干的 inline 模式决策。为防止阻塞 heartbeat 事件循环，查漏补缺的 session 数量硬上限为 2。

### 2.3 Heartbeat 执行扩展

在现有 heartbeat 执行路径中增加 **skill-invocation** 分支。Task 新增可选字段 `skill`，heartbeat 识别到该字段后直接调用对应 skill 的入口函数，而非创建 LLM agent：

```python
# 现有：同步确定性任务
deterministic_result = self._try_deterministic_task(task)
if deterministic_result is not None:
    continue

# 新增：skill 调用任务
if task.skill:
    result = await self._invoke_skill_task(task)
    continue

# 现有：LLM agent 执行
result = await self._run_agent(...)
```

### 2.4 SkillContext — Skill 运行时上下文

现有 `RuntimeDeps` 只服务于 `ContextStrategy`（prompt 构建），不满足 skill 的需求。新增 `SkillContext` 封装 skill 所需的全部依赖：

```python
@dataclass
class SkillContext:
    """Skill 运行时上下文，由 HeartbeatRunner._build_skill_context() 构建。"""
    sessions_dir: Path              # UserDataManager.sessions_dir
    workspace_path: Path            # agent workspace 路径
    agent_name: str                 # 当前 agent 名称（用于 session 过滤）
    memory_manager: MemoryManager   # 记忆管理（含 store）
    mailbox: MailboxPort            # 投递消息给用户
    llm: LLMClient                  # LLM 调用（用 fast model）
```

**构建方式**（在 `HeartbeatRunner` 中）：

```python
def _build_skill_context(self) -> SkillContext:
    memory_path = self._get_workspace_path() / "MEMORY.md"
    return SkillContext(
        sessions_dir=self.session_manager.persistence.sessions_dir,
        workspace_path=self._get_workspace_path(),
        agent_name=self.agent_name,
        memory_manager=MemoryManager(memory_path),
        mailbox=MailboxAdapter(self.session_manager, self.primary_session_id),
        llm=self._create_fast_llm_client(),
    )
```

`MailboxAdapter` 封装 `session_manager.deposit_mailbox_event()`，对 skill 暴露简单的 `deposit(summary, detail)` 接口。

## 3. SessionScanner: `src/everbot/core/scanners/`

Scanner 是纯工具函数，供 skill 按需调用。不是框架组件，不需要注册。

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

class SessionScanner:
    def __init__(self, sessions_dir: Path):
        ...

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

    # 1. 扫描（按 updated_at 升序）
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

    # 6. Watermark 推进
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

每个 skill 维护独立 watermark，互不影响。

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
| `src/everbot/core/tasks/task_manager.py` | Task dataclass 新增 `skill: Optional[str] = None` |
| `src/everbot/core/runtime/heartbeat.py` | 新增 `_invoke_skill_task()`、`_build_skill_context()`、`_load_skill()` |
| `src/everbot/core/runtime/context_strategy.py` | 新增 `SkillContext` dataclass |
| `src/everbot/core/memory/merger.py` | 新增 `merge_entries(a, b, merged_content) → MemoryEntry` |
| `src/everbot/core/memory/manager.py` | 新增 `apply_review(result) → dict`，文件锁内完成读写 |
| `src/everbot/core/scanners/session_scanner.py` | **新建**文件 |
| `skills/memory-review/` | **新建** skill |
| `skills/task-discover/` | **新建** skill |

## 7. Heartbeat 任务注册

```json
{
  "id": "reflection_memory_review",
  "title": "记忆整合优化",
  "schedule": "1d",
  "skill": "memory-review",
  "execution_mode": "inline",
  "timeout_seconds": 120,
  "source": "system"
},
{
  "id": "reflection_task_discover",
  "title": "任务发现",
  "schedule": "1d",
  "skill": "task-discover",
  "execution_mode": "inline",
  "timeout_seconds": 120,
  "source": "system"
}
```

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
| LLM 调用 | memory-review 2 次 + task-discover 1 次 /天 |

## 9. Error Handling

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

**核心不变量**：只有 skill 完整执行成功时才推进 watermark（补提失败、mailbox 失败等非关键路径除外）。

## 10. Implementation Sequence

| 阶段 | 内容 | 依赖 |
|------|------|------|
| 1 | `SkillContext` + `MailboxAdapter` | ports.py, MemoryManager |
| 2 | `SessionScanner` | SessionPersistence |
| 3 | `ReflectionState` | 无 |
| 4 | Task.skill 字段 + Heartbeat `_invoke_skill_task()` | SkillContext |
| 5 | `merger.merge_entries()` + `manager.apply_review()` | 无 |
| 6 | `skills/memory-review/` | scanner, memory, SkillContext |
| 7 | `skills/task-discover/` + `TaskDiscoverState` | scanner, mailbox, SkillContext |
| 8 | HEARTBEAT.md 自注册 | 阶段 4-7 |

## 11. Verification

1. **Scanner**：边界控制、agent_name 过滤、content 提取（list 格式 assistant content）、session 类型过滤
2. **SkillContext**：构建成功、各依赖可用、构建失败时 graceful error
3. **Skill 单测**：mock LLM + mock store，验证正常路径和各异常路径
4. **熵不变量**：`apply_review()` 在各种 LLM 返回下满足 `entries_after <= entries_before`
5. **错误恢复**：LLM 失败 → watermark 未推进 → 下次重试成功
6. **Session 跳过**：中间 session 损坏 → 后续正常处理 + watermark 正确推进
7. **并发安全**：并发 `apply_review()` 和 `process_session_end()` → 文件锁生效
8. **端到端**：daemon 启动 → heartbeat 触发 → MEMORY.md 变化 + mailbox 任务提议
