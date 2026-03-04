# Alfred 术语表

## 核心概念

| 术语 | 定义 |
|------|------|
| **Heartbeat** | 定时调度引擎，按 `schedule` 驱动 task 执行。每个 agent 一个 HeartbeatRunner 实例 |
| **Task** | Heartbeat 调度的最小单元，定义在 `HEARTBEAT.md` 的 JSON 块中，含 schedule、skill、scanner 等字段 |
| **Skill** | 可复用的处理逻辑模块。分两类：**内置 skill**（Python 模块，如 memory-review）和**外部 skill**（用户定义，由 LLM agent 执行） |
| **Scanner** | 轻量变化检测组件（gate），在 skill 执行前做预检。当前实现：`SessionScanner`（检测 session 变更） |
| **Watermark** | Per-skill 的数据游标（ISO timestamp），记录该 skill 上次处理到的数据时间点。类似 Kafka consumer offset |

## 执行模式

| 术语 | 定义 |
|------|------|
| **inline** | Task 在主会话上下文中执行，共享 `web_session_{agent}` 的历史消息 |
| **isolated** | Task 在独立 job session 中执行（`job_{task_id}_{uuid}`），不污染主会话。结果通过 mailbox 投递回主会话 |

## Scanner Gate 机制

| 术语 | 定义 |
|------|------|
| **Gate** | Scanner 的预检机制。`scanner.check(watermark)` 返回 `has_changes`，为 false 时跳过 skill 执行 |
| **ScanResult** | Scanner 预检结果，含 `has_changes`（是否有变化）、`change_summary`（日志描述）、`payload`（变化数据，供 skill 复用） |
| **Watermark 推进** | Skill 执行成功后将 watermark 更新到最新数据时间点。内置 skill 自行推进；外部 skill 由框架通过 `_advance_skill_watermark()` 推进 |

## 状态与存储

| 术语 | 定义 |
|------|------|
| **ReflectionState** | 持久化 watermark 的 JSON 文件（`{workspace}/.reflection_state.json`），每个 skill 独立一个 watermark 条目 |
| **SkillContext** | Skill 运行时上下文，封装 sessions_dir、workspace_path、memory_manager、mailbox、llm、scan_result 等依赖 |
| **Mailbox** | 异步消息投递机制，skill 通过 `context.mailbox.deposit()` 向用户主会话发送通知 |

## Session 类型

| 前缀 | 说明 |
|------|------|
| `web_session_{agent}` | 用户主会话，Scanner 会扫描 |
| `job_{task_id}_{uuid}` | Isolated task 的独立会话，Scanner 会扫描 |
| `heartbeat_session_{agent}` | Heartbeat 内部会话，Scanner 跳过 |
| `workflow_*` | 工作流会话，Scanner 跳过 |

## 执行路径

| 术语 | 定义 |
|------|------|
| **Path A** | `_execute_structured_tasks(include_isolated=True)` — heartbeat 内部循环执行 isolated 任务，包含完整 guard（scanner gate + min_interval + watermark 推进） |
| **Path B** | `execute_isolated_claimed_task()` — 统一调度模式下 daemon 直接调用 mixin 方法执行 isolated 任务。**⚠️ 当前缺失 guard，待 `TaskExecutionGate` 重构修复** |
| **TaskExecutionGate** | （计划中）统一的 guard 判定组件，封装 scanner/min_interval/retry backoff 逻辑。所有路径通过它获取可执行任务，消除路径旁路风险 |

## Task 生命周期

```
pending → (get_due_tasks + gate 校验) → running → (执行完成) → done → (rearm) → pending
                                                → (执行失败) → failed → (retry + backoff) → pending
```

- Skill task 的 `done` 状态会被 `_rearm_skill_task()` 自动重置为 `pending`，等待下次 schedule 到期
- Failed 重试设置 `next_run_at = now + min(2^retry × 30s, 1h)` 退避（待实现）
