# Feature 1: Register Skill Evaluation to Scheduler

## Spec
修改 `src/everbot/cli/daemon.py`，在 `_build_scheduler()` 方法中添加 Skill Evaluation Job 的定期调度。配置为每小时执行一次（3600 秒）。

**Acceptance Criteria**:
- [ ] Skill Evaluate Job 成功注册到 Scheduler
- [ ] 调度间隔设置为 3600 秒（每小时）
- [ ] Job 使用 isolated task 机制运行（与设计文档一致）
- [ ] 添加必要的依赖导入

## Analysis

> 已验证：以下分析基于实际代码库核实（2026-03-18）

### 关键文件

| 文件 | 角色 |
|------|------|
| `src/everbot/cli/daemon.py` | 需要修改的文件。`EverBotDaemon._build_scheduler()` 方法（L120-252）构建 Scheduler 并注入所有回调 |
| `src/everbot/core/runtime/scheduler.py` | `Scheduler` 类，管理 heartbeat/cron/inspector 三类周期性调度 |
| `src/everbot/core/jobs/skill_evaluate.py` | Skill Evaluation Job 实现。`async def run(context: SkillContext) -> str`，评估所有有新 segment 的技能 |
| `src/everbot/core/runtime/skill_context.py` | `SkillContext` dataclass，skill_evaluate.run() 的入参 |
| `docs/skill_lifecycle_design.md` | 设计文档，明确 SLM 任务作为 "isolated task 协程，由 Scheduler 调度" |

### 现有调度架构

`Scheduler` 管理三类周期性任务：
1. **Heartbeat ticks** — per-agent，间隔分钟级，由 `run_heartbeat` 回调执行
2. **Cron tasks** — per-agent，由 `_collect_due_tasks` 从 HeartbeatRunner 收集，分 inline/isolated 两种模式执行
3. **Inspector ticks** — per-agent，间隔分钟级，由 `run_inspector` 回调执行

`_build_scheduler()` 在 daemon.py L120-252 中：
- 定义了 `_collect_due_tasks`、`_claim_task`、`_run_inline`、`_run_isolated`、`_run_inspector` 五个闭包回调
- 从 `self.heartbeat_runners` 构建 `agent_schedules` 和 `inspector_schedules`
- 将所有回调注入 `Scheduler` 构造函数

### Scheduler 的 cron task 调度流程

`Scheduler._tick_tasks(ts)` 的调度流程（scheduler.py L249-280）：
1. 调用 `_get_due_tasks(ts)` 收集所有到期任务 → 返回 `list[SchedulerTask]`
2. 按 `execution_mode` 拆分为 inline / isolated
3. **Isolated tasks**: 逐个调用 `_claim_task(task.id)` → 若成功则调用 `_run_isolated(task, ts)`

daemon.py 中的闭包实现：
- `_collect_due_tasks`: 遍历所有 runner，调用 `list_due_inline_tasks` / `list_due_isolated_tasks`，将结果存入 `isolated_lookup` dict（key 为 `agent_name:task_id`）
- `_claim_task`: 从 `isolated_lookup` 取出 `(runner, snapshot)`，调用 `runner.claim_isolated_task(task_id)`
- `_run_isolated`: 从 `isolated_lookup` 取出 `(runner, snapshot)`，调用 `runner.execute_isolated_claimed_task(snapshot, run_id, now)`

### Skill Evaluation 的特殊性

`skill_evaluate.run(context: SkillContext)` 是一个**全局任务**（评估所有技能），而不是 per-agent 任务。它需要 `SkillContext`，其中关键依赖是 `context.llm`（LLM 客户端）。

`SkillContext` 由 `HeartbeatRunner._build_skill_context()` 构建（heartbeat.py L1069），包含：
- `sessions_dir`, `workspace_path`, `agent_name` — 来自 runner
- `memory_manager` — MemoryManager（基于 workspace MEMORY.md）
- `mailbox` — MailboxAdapter（需要 session_manager + primary_session_id）
- `llm` — LLM 客户端（通过 `_create_skill_llm_client()`）

**skill_evaluate.run() 实际依赖**：只使用 `context.llm`，通过 `get_user_data_manager()` 获取 `skill_logs_dir` 和 `skills_dir`。不依赖特定 agent 的 workspace/memory/mailbox。

### 实现方案

**方案：将 skill_evaluate 作为合成 isolated task 注入现有 cron task 调度流程**

在 `_build_scheduler()` 中修改三个闭包：
1. `_collect_due_tasks` — 每 3600 秒注入一个合成 `SchedulerTask`
2. `_claim_task` — 识别合成 task，直接返回 True
3. `_run_isolated` — 识别合成 task，构建 SkillContext 并调用 `skill_evaluate.run()`

用闭包变量 `_next_skill_eval_at` 跟踪下次执行时间。

**优势**：
- 不修改 `scheduler.py`，零侵入 Scheduler 核心逻辑
- 复用 Scheduler 现有的 isolated task claim/dispatch/error-isolation 流程
- 与设计文档一致（"SLM 不修改 Scheduler"、"作为普通 isolated task 注册到由现有 Scheduler 统一调度"）

**约束**：
- `_collect_due_tasks` 仅在 `self._scheduler_cron_jobs=True` 时被传入 Scheduler（daemon.py L243）。当 cron_jobs 关闭时 skill evaluation 不会运行——这是可接受的，因为 cron_jobs=False 意味着所有定期任务由 runner 内部管理
- 选择第一个可用 runner 来构建 SkillContext 是合理的，因为 skill_evaluate 只使用 `context.llm`

### 合成 task 标识

使用 `__skill_evaluate__` 作为固定 task.id。该 id 不含 `:` 因此不会与正常的 `agent_name:task_id` 格式冲突。不需要存入 `isolated_lookup`——在 `_claim_task` 和 `_run_isolated` 中直接检查 id 前缀做分支处理。

## Plan

1. **添加导入** — 在 daemon.py 顶部添加 `from datetime import timedelta`（检查是否已导入）以及在 `_run_isolated` 闭包内延迟导入 `from ..core.jobs import skill_evaluate`

2. **添加闭包调度状态** — 在 `_build_scheduler()` 方法开头（`isolated_lookup` 之后），添加：
   - `_SKILL_EVAL_INTERVAL = 3600`（常量）
   - `_next_skill_eval_at: list[datetime | None] = [None]`（用 list 包装以支持闭包 nonlocal 写入，或使用 nonlocal 关键字）

3. **修改 `_collect_due_tasks` 闭包** — 在末尾（`return due` 之前）添加：当 `_next_skill_eval_at[0]` 为 None 或 `ts >= _next_skill_eval_at[0]` 时，向 `due` 追加 `SchedulerTask(id="__skill_evaluate__", agent_name="__daemon__", execution_mode="isolated", timeout_seconds=300)`

4. **修改 `_claim_task` 闭包** — 在开头添加：若 `task_key == "__skill_evaluate__"` 则直接 `return True`

5. **修改 `_run_isolated` 闭包** — 在开头添加 `__skill_evaluate__` 分支：
   - 从 `self.heartbeat_runners` 取第一个 runner（若无则 return）
   - 调用 `runner._build_skill_context()` 构建 SkillContext
   - `from ..core.jobs import skill_evaluate` 延迟导入
   - `result = await skill_evaluate.run(context)`
   - `_next_skill_eval_at[0] = ts + timedelta(seconds=_SKILL_EVAL_INTERVAL)`
   - `logger.info("Skill evaluation completed: %s", result)`
   - `return`

6. **验证** — 确认修改不影响正常 cron task 流程；确认 `_scheduler_cron_jobs=False` 时 skill evaluation 不注入

## Test Results

## Dev Log
