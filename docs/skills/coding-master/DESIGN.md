# Coding Master: 公约驱动 + 两层工具架构

> **版本**: v5.0（v4.6 → 两层工具架构 / `_cm_next` 自动推进 / Agent 工具面 25→7 / 断点模式）
> **创建时间**: 2026-03-08
> **最后更新**: 2026-03-15
> **状态**: Active

---

## 1. 问题陈述

### 1.1 现状

当前 coding master 是一个 **4800 行 Python 的命令调度系统**：

```
scripts/
├── dispatch.py          # 1762 行，路由 + 编排
├── workspace.py         # lock 管理、环境探测
├── config_manager.py    # 配置
├── git_ops.py           # git 封装
├── test_runner.py       # 测试运行
├── feature_manager.py   # feature plan（JSON 状态机）
├── env_probe.py         # 环境探测
├── repo_target.py       # repo 解析
└── engine/              # claude/codex runner
```

核心问题：

| 问题 | 证据 |
|------|------|
| **Agent 绕过 dispatch** | demo 会话中 agent 全程用 bash 自己写代码 |
| **命令摩擦大** | 6+ 命令 + 复杂参数组合，agent 记不住 |
| **状态不透明** | 锁在 JSON 里，feature plan 在 JSON 里，agent 看不见全局 |
| **Python 编排冗余** | LLM 天然能做 write→test→fix 循环，不需要 Python 中间层 |
| **维护成本高** | 4800 行代码 + 大量测试，改一个流程要改多处 |

### 1.2 核心洞察

**LLM 能读懂 markdown 里的公约，不需要 Python 状态机来强制流程。**

Alfred 已有成功先例：MEMORY.md、AGENTS.md、HEARTBEAT.md 都是 "MD 文件即状态" 的范式。coding master 应该跟它们一致。

### 1.3 设计目标

1. **消费者决定形态** — agent/人类读的用 MD，工具原子操作的用 JSON
2. **SKILL.md 定义公约** — 流程、规则、状态转换都在 markdown 里
3. **工具只做机械活** — lock/unlock、claim/unclaim、git、跑测试，不做编排
4. **Feature 并行** — 多 agent 各自认领、独立工作、汇总结果
5. **从 4800 行 → 精简的工具代码**（tools.py ~2900 行 + engine.py ~200 行）

---

## 2. 关键 Tradeoff

### T1: Python 编排 vs MD 公约

| 方案 | 优点 | 缺点 |
|------|------|------|
| Python 编排（现状） | 强制流程一致性 | agent 绕过就失效；维护重 |
| **MD 公约（选定）** | agent 天然理解；状态透明；零摩擦 | 依赖 agent 遵守公约 |

**决策**：agent 不遵守公约时，靠 inspector 检测 + 提醒纠正，而不是靠 Python 强制。就像人类团队靠 code review 而不是靠编译器来保证流程。

### T2: 全 MD vs MD + JSON 混合

| 方案 | 优点 | 缺点 |
|------|------|------|
| 全 MD | 最简单，一致性好 | 并发认领无法原子化；多 agent 编辑同一 MD 必冲突 |
| **MD + JSON 混合（选定）** | 各取所长：MD 给 agent 读写，JSON 给工具做原子操作 | 两种格式需要明确边界 |

**决策**：MD + JSON 混合，按分层判定。

- 需要 agent 理解和表达的 → **上浮到计划层（MD）**：规格、工作现场
- 需要程序保证正确性的 → **下沉到数据层（JSON）**：锁、认领状态

Agent 只接触计划层（MD）和工具层（调用），永远不直接碰数据层（JSON）。

### T3: 工具粒度 — 全封装 vs 最小工具

| 方案 | 优点 | 缺点 |
|------|------|------|
| 全封装（auto-dev 一步到位） | agent 只需一次调用 | 黑盒；失败了 agent 不知道怎么修 |
| **最小工具（选定）** | agent 有完整控制力；出错可定位 | agent 需要多步操作 |

**决策**：工具只做 agent 自己做不了或做不好的事——文件锁（原子性）、认领（原子性）、git push/PR（auth）、跑测试（环境）。开发本身就让 agent 直接干。

**v4.5 修正**：「agent 直接干」的前提是 agent 有基本的代码作业权——读文件、找文件、搜索内容、编辑文件。早期设计假设 agent 天然具备这些能力（如 Claude Code 的原生工具），但在 Dolphin/Telegram 等 skillkit 宿主中，agent **只能**通过注册的工具函数操作环境。缺少文件操作工具会导致：
- agent 只有流程控制权（锁/认领/状态推进），没有代码作业权
- `cm engine-run` 成为唯一的代码接触通道，API 限额/失败时 agent 完全失明
- agent 被迫用 `_cm_git show` 找文件、读代码，语义别扭且频繁失败（git 不是 shell）

因此工具集需要补充**文件操作层**（`cm read`/`cm find`/`cm grep`/`cm edit`），使 engine 从"唯一路径"降级为"深度分析加速器"。这不违反最小工具原则——文件操作是 agent 在 skillkit 宿主中"自己做不了"的事。

### T4: Session/Feature 两级状态与锁策略

| 方案 | 优点 | 缺点 |
|------|------|------|
| 单级锁（session 或 feature 择一） | 简单 | session 级不够精细，feature 级缺全局协调 |
| Session + Feature 双层文件锁 | 精细控制 | 复杂度高，死锁风险，两把锁的获取顺序需严格约定 |
| **Session 锁 + Feature 认领 + Worktree 隔离（选定）** | 各司其职：session 锁防多 plan 冲突，claim 防重复认领，worktree 防代码冲突 | 一个 repo 同时只能一个 session |

**决策**：三个机制各解决一类问题，不需要 feature 级文件锁。

- **Session 级**（`lock.json` + `session.json`）：排他锁，保证同一时间只有一组 agent 在同一个 repo 上执行一个 PLAN。解决的是"谁在用这个 repo"。`lock.json` 仅存活跃 session 状态（unlock 时清空），`session.json` 持久化 session 历史（branch、mode、phase），使下一次 `cm lock` 能复用上次的 dev branch 而不是每次新建。
- **Feature 级**（`claims.json` 认领）：原子认领，保证一个 feature 只有一个 owner。解决的是"谁负责哪个 feature"。用 flock 保证认领的原子性，但不是持久文件锁。
- **代码隔离**（worktree）：**session 级和 feature 级都使用独立 worktree**，物理上不会冲突。解决的是"代码改动互不影响"且"不污染主 repo 工作区"。

一个 repo 同时只能有一个 session（一个 lock = 一个 PLAN），但一个 session 内可以有多个 agent 并行工作在不同 feature 的 worktree 中。这是有意的简化——并行多 session 带来的 branch 管理和 merge 复杂度不值得。

**Session Worktree 隔离**：`cm lock` 创建 dev branch 时不在主 repo 上 checkout，而是通过 `git worktree add` 在独立目录（`<repo-parent>/<repo-name>-session`）中创建 session worktree。主 repo 始终保持在用户原来的分支上，用户的未提交修改不受影响。Feature worktree 的父目录与 session worktree 同级（`<repo-parent>/<repo-name>-feature-N`）。`cm integrate` 和 `cm submit` 的所有 git 操作都在 session worktree 中执行。

**Branch 复用**：`session.json` 持久化上一次 session 的 branch 名。`cm lock` 创建新 session 时，先读 `session.json` 中的 branch，检查该分支是否仍指向 HEAD（即没有产生过 commit）。如果是，则复用该分支；否则新建 `dev/<repo>-MMDD-HHMM`。这避免了 session 反复创建/销毁时产生大量空分支。优先级：用户 `--branch` 参数 > session.json 中可复用的分支 > 新时间戳分支。

### T5: 平铺工具集 → 分层工具集 → 两层架构（v5.0）

| 方案 | 优点 | 缺点 |
|------|------|------|
| 平铺（25 个工具同级暴露） | 实现简单；agent 可自由组合 | agent 认知负担重；选错→报错→猜→循环 |
| 分层（v4.6：25 个工具按 L0-L3 分组 + available_actions） | 有层级意识；错误消息可精准 | 仍暴露 25 个工具；agent 仍需理解状态机 |
| **两层架构（v5.0：7 个 Agent 工具 + 内部工具）（选定）** | agent 工具面降到 7 个；状态机对 agent 完全不可见 | `_cm_next` 内部逻辑复杂 |

**决策**：从"agent 编排 + 工具执行"转变为"系统编排 + agent 在断点创造"。

**v5.0 核心洞察**：如果 12 步中 9 步不需要 agent 动脑，为什么要让 agent 调 9 个工具？系统应该自动跑完机械步骤，只在需要创造力的地方停下来（断点）。

**v5.1 进化**：v5.0 减少了机械步骤断点，但仍在"创造性断点"（write_analysis、write_code、fix_code）让 **agent** 通过 `_cm_edit` 逐行写代码。生产轨迹（2026-03-15）暴露根因：

1. **弱模型干强模型的活** — agent（kimi-code）在 `write_code` 断点需要理解代码库、实现功能、修 bug，但它的能力不足，频繁空转调 `_cm_next` 不写代码
2. **Engine 已经能做** — `cmd_engine_run` 调用 claude-code CLI（有 Read/Edit/Write/Bash 全套权限），deliver 模式的 prompt 和 tools 已定义，但 deliver 流程没用它
3. **Agent 职责错位** — agent 应该做"理解用户意图 + 任务分解 + 呈现结果"，不应该做"读代码 + 写代码 + 修 bug"

**v5.1 决策**：Agent 降级为**调度者**，Engine 接管所有代码工作。断点从 6 个降到 3 个。

**三层架构**：

```
┌─────────────────────────────────────────────────────┐
│  Agent 层（调度者 — 理解意图、写 PLAN、呈现结果）       │
│                                                      │
│  _cm_next    流水线推进（唯一的流程入口）               │
│  _cm_edit    编辑 PLAN.md / feature MD（规划文件）     │
│  _cm_read    读文件                                   │
│  _cm_find    找文件                                   │
│  _cm_grep    搜内容                                   │
│  _cm_status  状态 + 进度 + repos 列表                 │
│  _cm_doctor  诊断 + 修复                              │
├─────────────────────────────────────────────────────┤
│  Engine 层（执行者 — claude-code CLI 子进程）           │
│                                                      │
│  analyze:    读代码 + 写 Analysis/Plan 到 feature MD  │
│  implement:  在 worktree 里实现功能                    │
│  fix:        根据 test output 修复代码                 │
│                                                      │
│  权限: Read,Edit,Write,Glob,Grep,Bash                │
│  运行目录: feature worktree（不是 repo root）          │
├─────────────────────────────────────────────────────┤
│  Internal 层（状态机 — _cm_next 和 engine 内部调用）    │
│                                                      │
│  cmd_lock / cmd_unlock / cmd_plan_ready              │
│  cmd_claim / cmd_dev / cmd_test / cmd_done           │
│  cmd_reopen / cmd_integrate / cmd_submit             │
│  cmd_git / cmd_journal / cmd_change_summary          │
│                                                      │
│  仍可通过 CLI (cm lock, cm claim ...) 手动调用        │
└─────────────────────────────────────────────────────┘
```

**`_cm_next` 断点模式（deliver）**：

```
_cm_next()
  ├─ 无 lock → auto lock
  │   → no PLAN.md → breakpoint: write_plan (agent 写)
  │   → PLAN.md ok → auto plan-ready → auto claim
  │
  ├─ feature analyzing
  │   → ENGINE: analyze（读代码，写 Analysis+Plan 到 feature MD）
  │   → auto dev → recurse
  │
  ├─ feature developing
  │   → ENGINE: implement（在 worktree 里写代码）
  │   → auto commit → auto test
  │   ├─ pass → auto done → auto claim next 或 integrate
  │   └─ fail → ENGINE: fix（带 test output 重试，最多 3 次）
  │       └─ 仍失败 → breakpoint: engine_failed
  │
  └─ all done → auto integrate → auto submit
      → breakpoint: complete (附 PR URL)
```

**`_cm_next` 断点模式（review/debug/analyze）**：

```
_cm_next(mode="review")
  ├─ 无 lock → auto lock(mode) → breakpoint: define_scope
  ├─ _cm_next(diff="...") → auto scope + auto engine-run
  │   → breakpoint: write_report (附 findings)
  │   （也支持 intent="scope" 写法，但 diff/files 参数足以自动识别）
  └─ report 已写 → auto unlock → breakpoint: complete
```

**断点返回值契约**：

```json
{
  "ok": true,
  "breakpoint": "complete",
  "pr_url": "https://github.com/.../pull/42",
  "instruction": "PR 已创建，将结果展示给用户。"
}
```

**关键设计约束**：
- **Agent 只调 `_cm_next` + `_cm_edit`** — Agent 编辑仅限 PLAN.md 等规划文件，源码编辑由 engine 完成
- **状态机对 agent 不可见** — agent 不知道 session_phase、feature_phase 等概念
- **`_cm_next` 递归推进** — 自动跳过所有机械步骤，engine 处理所有代码工作
- **Engine 重试保护** — 每阶段最多重试 3 次，超过返回 `engine_failed` 断点
- **递归深度保护** — `max_depth=25`，支持 4 feature plan 的完整自动推进
- **CLI 不受影响** — `cm lock`、`cm claim` 等命令仍可通过 CLI 手动调用

---

## 3. 文件架构

### 3.1 总览

```
<repo>/.coding-master/               # 被 .gitignore 忽略，仅本地协作状态
├── lock.json                        # 结构化：workspace 锁（工具原子读写，unlock 时清空）
├── session.json                     # 结构化：持久化 session 历史（跨 lock/unlock 生存）
├── JOURNAL.md                       # MD：append-only 开发日志（跨 session 保留）
│
│   ── 以下为 per-session 状态，新 session lock 时自动清理 ──
├── PLAN.md                          # MD：feature 规格（写一次，agent 读）
├── claims.json                      # 结构化：feature 认领状态（工具原子读写）
│
├── features/                        # MD：每个 feature 的独立工作现场
│   ├── 01-scanner-interface.md
│   └── 02-new-scan-logic.md
│
├── evidence/                        # v4: 结构化验证证据
│   ├── 1-verify.json               # per-feature lint+typecheck+test 结果
│   ├── 2-verify.json
│   └── integration-report.json     # session 级集成测试结果
│
└── delegation/                      # v4: 委托请求/结果
    ├── 1-request.json
    └── 1-result.json
```

四层架构：

```
┌──────────────────────────────────────────────────────────┐
│  公约层（SKILL.md）                                        │
│                                                          │
│  定义流程、规则、状态机、模板                                 │
│  谁写：人类设计时          谁读：agent                       │
│  **不可变**：agent 不得修改                                 │
├──────────────────────────────────────────────────────────┤
│  计划层（MD）— agent 读写                                   │
│                                                          │
│  PLAN.md — feature 规格 + AC（写一次，所有 agent 只读）      │
│  JOURNAL.md — 开发日志（通过 cm journal 追加）              │
│  features/XX.md — 工作现场：分析/方案/AC打勾/日志（owner写） │
├──────────────────────────────────────────────────────────┤
│  工具层（Python）— 两层架构                                   │
│                                                          │
│  Agent 层（7 个，agent 直接调用）：                          │
│    next / edit / read / find / grep / status / doctor     │
│                                                          │
│  Internal 层（不暴露，_cm_next 内部调用）：                    │
│    lock / unlock / start / plan-ready                     │
│    claim / dev / test / done / reopen                     │
│    integrate / submit / git / journal / ...               │
│                                                          │
│  向上：被 agent 调用      向下：读写数据层                    │
├──────────────────────────────────────────────────────────┤
│  数据层（JSON）— agent 不可见                               │
│                                                          │
│  lock.json — session 状态（锁 + session phase）            │
│  claims.json — feature 申请表（phase + 各阶段子状态）       │
│                                                          │
│  工具的持久化存储，flock 原子操作                             │
└──────────────────────────────────────────────────────────┘

上层依赖下层，agent 只接触上面两层（读 MD + 调工具）。
JSON 是工具的"数据库"，对 agent 不可见。
.coding-master/ 是运行期状态目录，不进入 git 历史。
```

**分层判定原则**：

- 需要 agent 理解和表达的 → **上浮到计划层（MD）**：分析、方案、AC 打勾
- 需要程序保证正确性的 → **下沉到数据层（JSON）+ 工具层**：状态机推进、前置条件检查
- agent 永远不直接碰 JSON，就像用户不直接写数据库
- `cm progress` 是数据层到 agent 的"翻译窗口"——将 JSON 状态转换为自然语言指引

### 3.2 lock.json — Workspace 锁

**消费者**：工具（原子读写）
**Agent 不直接编辑**，通过 `cm lock` / `cm unlock` 操作。

**一个 lock = 一个开发会话（session）= 一个 PLAN**。lock 保证同一时间只有一组 agent 在同一个 repo 上执行一个 PLAN。lock 建立时创建 dev branch 作为基线，session 结束时 submit + unlock。

**Session 状态机**：

```
cm lock        cm plan-ready          cm claim      cm integrate(pass)  cm submit
   │                │                    │                │                │
   ▼                ▼                    ▼                ▼                ▼
┌────────┐    ┌──────────┐        ┌──────────┐    ┌─────────────┐    ┌──────┐
│ locked │───►│ reviewed │───────►│ working  │───►│ integrating │───►│ done │
└────────┘    └──────────┘        └──────────┘    └─────────────┘    └──────┘
                                        ▲                │
                                        └────────────────┘
                                        cm integrate 失败
                                       + cm reopen 修复后重试
```

| session_phase | 含义 | 进入条件 | cm progress 显示 |
|---------------|------|----------|------------------|
| `locked` | workspace 锁定，尚未规划 | `cm start` | PLAN.md 不存在："用 _cm_edit 创建 PLAN.md"；PLAN.md 已存在："运行 _cm_start 验证并推进" |
| `reviewed` | PLAN.md 已审核通过，可以开始开发 | `cm start`（内部调 plan-ready）检查通过 | "认领 feature 开始开发" |
| `working` | 有 feature 在进行中 | 第一个 `cm claim` | 显示各 feature 状态 |
| `integrating` | 所有 feature done，集成验证通过 | `cm integrate` 成功 | "运行 cm submit 提交" |
| `done` | 已提交并解锁 | `cm submit` 成功 | — |

```json
{
  "repo": "alfred",
  "mode": "deliver",
  "session_phase": "working",
  "branch": "dev/alfred-0308-1000",
  "session_worktree": "../alfred-session",
  "read_only": false,
  "locked_by": "dolphin",
  "locked_at": "2026-03-08T10:00:00Z",
  "lease_expires_at": "2026-03-08T12:00:00Z",
  "session_agents": ["dolphin-a", "dolphin-b"]
}
```

`session_phase` 由工具在关键操作时自动更新（`cm start` → locked/reviewed，`cm claim` → working，`cm integrate` → integrating 等），agent 不需要手动管理。`cm progress` 是纯查询工具，只读取状态并展示，**不修改任何状态**。

为什么是 JSON 不是 MD：锁需要原子性判断（是否过期、是否已占用），session phase 需要精确枚举，程序解析 JSON 零歧义。

**Mode 系统**：

| Mode | 读写 | 用途 | 完成条件 |
|------|------|------|----------|
| `deliver` | 读写 | Feature 开发（默认） | 所有 feature done + evidence pass |
| `debug` | 读写 | 调查和修复 | diagnosis.md exists |
| `review` | 只读 | 代码审查 | report.md exists |
| `analyze` | 只读 | 代码分析 | report.md exists |

**Session 连续性**（数据层即事实）：

`lock.json` 是跨 turn、跨 conversation 的唯一事实源。所有命令先读 lock.json 再决策，不依赖调用者身份或上下文连续性。

- **跨 turn 复用**：`cm lock` 发现 `session_phase` 存在且不是 `"done"` 时，join 已有 session（续约 lease，返回已有 session_worktree 路径），不创建新分支。Agent ID 仅用于 journal 审计，不作为身份校验。
- **Lease 自动续约**：所有写命令的前置检查 (`_check_lease`) 在发现过期时自动续约，不阻塞操作。这确保长时间或中断后恢复的 session 可以继续。
- **Read-only overlay**：review/analyze mode 在 write session 上叠加时，不修改 lock.json。overlay 的 unlock 不会破坏底层 write session。
- **Write session 保护**：`cm unlock` 在 write session 进行中（`session_phase != "done"`）时拒绝清空，必须走 `cm submit` 或 `cm unlock --force`。

**Dev branch 约束**：dev branch 存在于 session worktree 中（`<repo-parent>/<repo-name>-session`），在整个 session 期间只作为基线和最终汇总点，不应有直接 commit。所有开发在 feature worktree 中进行。`cm integrate` 的 merge 和 `cm submit` 的 commit/push 操作是唯一合法的 dev branch 写入，均在 session worktree 中执行。主 repo 工作区不受任何 session 操作影响。

**原子性要求**：

- `cm lock` 必须使用 `flock + read-modify-write`，不能先 `exists()` 再写
- session worktree 创建失败时，不得保留 lock.json
- lock 建立失败时，需要回滚刚创建的 session worktree，避免留下脏状态

**Agent Identity**：

- agent identity 默认为 `hostname-PID`，可通过 `--agent` 参数传入
- Identity 仅用于 journal 审计和 session_agents 列表，**不用于权限校验**
- 同一 session 内的所有 agent 共享 lease，任一 agent 可续租

**Lease 机制**：

- 默认 lease 120 分钟
- `cm renew` 显式续租；写命令的前置检查自动续约过期 lease
- `cm lock` join 已有 session 时自动续约

### 3.3 PLAN.md — Feature 规格

**消费者**：agent 读（了解要做什么）
**生命周期**：分析阶段创建一次 → 开发过程中基本不改 → 全部完成后归档

```markdown
# Feature Plan

## Origin Task
重构 inspector 模块，拆分 SessionScanner 和 ReportGenerator

## Features

### Feature 1: 提取 SessionScanner 接口
**Depends on**: —

#### Task
将 `inspector.py` 中的 scan 逻辑提取为独立的 `SessionScanner` 类，
定义清晰的接口。

#### Acceptance Criteria
- [ ] `SessionScanner` 类存在且有 `scan(messages) -> ScanResult` 方法
- [ ] 原有测试全部通过（`pytest tests/unit/test_inspector.py`）
- [ ] 无新增 lint 警告

---

### Feature 2: 实现新的 scan 逻辑
**Depends on**: Feature 1

#### Task
基于新的 SessionScanner 接口，实现支持增量 scan 的逻辑。

#### Acceptance Criteria
- [ ] 增量 scan：只处理上次 watermark 之后的新消息
- [ ] 性能：1000 条消息的 scan < 100ms
- [ ] 测试覆盖率 > 90%

---

### Feature 3: 拆分 ReportGenerator
**Depends on**: Feature 1

#### Task
将报告生成逻辑从 inspector.py 拆分到独立的 ReportGenerator 类。

#### Acceptance Criteria
- [ ] ReportGenerator 类独立存在
- [ ] inspector.py 通过组合调用 Scanner + Generator
- [ ] 原有测试全过

---

### Feature 4: 补充集成测试
**Depends on**: Feature 2, Feature 3

#### Task
补充 Scanner + Generator 联合工作的集成测试。

#### Acceptance Criteria
- [ ] 至少 3 个集成测试场景
- [ ] 覆盖空 session、正常 session、超大 session
```

**设计要点**：

- **只写规格，不写进展** — 没有 Status、没有 Dev Log、没有 Test Results
- **Acceptance Criteria 的 checkbox 在这里不打勾** — 这是"定义"，agent 在自己的 feature MD 里打勾
- **Depends on 是文本** — agent 直接读懂依赖关系
- **创建后基本不改** — 避免多 agent 同时编辑的冲突

### 3.4 claims.json — Feature 状态（申请表）

**消费者**：工具（原子读写）
**角色**：每个 feature 的"办事申请表"——记录当前阶段、各阶段的独立状态和产出物。agent 不直接编辑，通过 cm 命令推进状态。

#### 3.4.1 Feature 状态机

**主状态（`phase`）**：`pending` → `analyzing` → `developing` → `done`（4 个阶段）

```
    cm claim              cm dev                                cm done
       │                    │                                      │
       ▼                    ▼                                      ▼
  ┌─────────┐       ┌───────────┐       ┌──────────────┐     ┌──────────┐
  │ pending │──────►│ analyzing │──────►│  developing  │────►│   done   │
  └─────────┘       └───────────┘       └──────────────┘     └──────────┘
                    analysis: ─/done              │
                    plan: ─/done                  │
                                                  │
                                        ┌─────────┴──────────────────┐
                                        │  developing 内部（test 循环） │
                                        │                            │
                                        │  edit → commit → test ─────┤│
                                        │   ↑              failed ───┘│
                                        │   └────────────────────     │
                                        │             passed ──► done │
                                        │                            │
                                        │  代码变更后:                 │
                                        │  test passed → stale       │
                                        └────────────────────────────┘
```

**test 循环说明**：
- 编码 → 提交 → `cm test`，解决"代码能不能跑"。test 失败则修复后重新 test。
- `cm done` 要求 `test_status == passed && test_commit == git HEAD`。

每个阶段内部有**独立的子状态**，记录该阶段的进展和产出物。`cm progress` 从子状态生成精确的自然语言指引。

#### 3.4.2 各阶段的子状态定义

**pending 阶段**：

| 字段 | 取值 | 含义 |
|------|------|------|
| `blocked_by` | `["1", "3"]` / `[]` | 阻塞该 feature 的未完成依赖 |

阻塞时 `cm claim` 拒绝。依赖全部 done 后 `blocked_by` 为空，可认领。

---

**analyzing 阶段**：分析代码、撰写方案

| 字段 | 取值 | 含义 |
|------|------|------|
| `analysis` | `pending` / `done` | 是否已完成 Analysis 段落 |
| `plan` | `pending` / `done` | 是否已完成 Plan 段落 |

- `cm dev` 的前置条件：`analysis == done && plan == done`
- 工具如何判定：`cm dev` 时检查 `features/XX.md` 是否包含非空的 `## Analysis` 和 `## Plan` 段落

---

**developing 阶段**：编码 + 测试循环

| 字段 | 取值 | 含义 |
|------|------|------|
| `commit_count` | `0`, `1`, `5`... | feature branch 上的 commit 数量 |
| `latest_commit` | `"a1b2c3d"` | 最新 commit SHA |
| `test_status` | `pending` / `passed` / `failed` | 最近一次测试结果 |
| `test_commit` | `"a1b2c3d"` / `null` | 测试时的 HEAD SHA |
| `test_passed_at` | ISO timestamp / `null` | 最近一次测试通过的时间 |
| `test_output` | `"3 passed, 1 failed: ..."` / `null` | 最近一次测试的输出摘要（截断到 500 字符） |

**test 循环**：
- `cm test` 更新 `test_status`、`test_commit`、`test_passed_at`、`test_output`；同时从 git log 更新 `commit_count`、`latest_commit`
- test 失败 → 修复代码 → 重新 `cm test`
- **自动回退**：`cm progress` 检测到 `test_commit ≠ latest_commit` 时，将 `test_status` 显示为 `stale`（需重新 cm test）
- **接力支持**：`test_output` 持久化测试失败原因，接力 agent 无需重跑测试即可理解失败上下文

**cm done 的前置条件**：
1. `test_status == passed && test_commit == git HEAD`

> **v5.1 规划**：外层 review 循环（独立 engine 代码审查，`VERDICT: APPROVED/CHANGES_REQUESTED`）尚未实现，预留于多 agent 场景。

---

**done 阶段**：无子状态，终态。

---

#### 3.4.3 JSON 结构

```json
{
  "features": {
    "1": {
      "agent": "dolphin-a",
      "phase": "done",
      "branch": "feat/scanner-interface",
      "worktree": "../alfred-feature-1",
      "claimed_at": "2026-03-08T10:00:00Z",
      "analyzing": {
        "analysis": "done",
        "plan": "done",
        "completed_at": "2026-03-08T10:15:00Z"
      },
      "developing": {
        "started_at": "2026-03-08T10:15:00Z",
        "commit_count": 4,
        "latest_commit": "a1b2c3d",
        "test_status": "passed",
        "test_commit": "a1b2c3d",
        "test_passed_at": "2026-03-08T10:41:00Z",
        "test_output": null
      },
      "completed_at": "2026-03-08T10:42:00Z"
    },
    "2": {
      "agent": "dolphin-a",
      "phase": "developing",
      "branch": "feat/new-scan",
      "worktree": "../alfred-feature-2",
      "claimed_at": "2026-03-08T11:00:00Z",
      "analyzing": {
        "analysis": "done",
        "plan": "done",
        "completed_at": "2026-03-08T11:10:00Z"
      },
      "developing": {
        "started_at": "2026-03-08T11:10:00Z",
        "commit_count": 3,
        "latest_commit": "b2c3d4e",
        "test_status": "failed",
        "test_commit": "a1b2c3d",
        "test_passed_at": null,
        "test_output": "test_scan_incremental FAILED - AssertionError: watermark not updated"
      }
    },
    "3": {
      "agent": "dolphin-b",
      "phase": "developing",
      "branch": "feat/report-generator",
      "worktree": "../alfred-feature-3",
      "claimed_at": "2026-03-08T11:00:00Z",
      "analyzing": {
        "analysis": "done",
        "plan": "done",
        "completed_at": "2026-03-08T11:05:00Z"
      },
      "developing": {
        "started_at": "2026-03-08T11:05:00Z",
        "commit_count": 5,
        "latest_commit": "e4f5g6h",
        "test_status": "passed",
        "test_commit": "e4f5g6h",
        "test_passed_at": "2026-03-08T11:30:00Z",
        "test_output": null
      }
    }
  }
}
```

#### 3.4.4 `cm progress` 状态展示

`cm progress` 从 session 状态 + 每个 feature 的阶段子状态计算展示信息和**分步操作指引**。

**返回格式**：工具层返回结构化 JSON（见 §4.3），CLI 层将 JSON 格式化为以下人可读文本输出给 agent：

```
## Session: working (3 features: 1 done, 2 developing)
## Progress: 1/3 done

Feature 1 [done] dolphin-a
  worktree: ../alfred-feature-1
  feature_md: .coding-master/features/01-scanner-interface.md
  ✓ analyzing: analysis done, plan done
  ✓ developing: 4 commits, tests passed (a1b2c3d)
  ✓ completed

Feature 2 [developing] dolphin-a
  worktree: ../alfred-feature-2
  feature_md: .coding-master/features/02-incremental-scan.md
  ✓ analyzing: analysis done, plan done
  ⚠ developing: 3 commits, tests FAILED (tested a1b2c3d, latest b2c3d4e)
    last output: test_scan_incremental FAILED - AssertionError: watermark not updated
  → 操作步骤:
    1. cd ../alfred-feature-2
    2. 阅读 .coding-master/features/02-incremental-scan.md 的 Dev Log 了解上下文
    3. 失败原因: test_scan_incremental FAILED - AssertionError: watermark not updated
    4. 修复代码并 git commit
    5. 运行 cm test --feature 2

Feature 3 [developing] dolphin-b
  worktree: ../alfred-feature-3
  feature_md: .coding-master/features/03-report-generator.md
  ✓ analyzing: analysis done, plan done
  ✓ developing: 5 commits, tests passed (e4f5g6h)
  → 操作步骤:
    1. 阅读 {feature_md} 确认 Acceptance Criteria 全部满足
    2. 运行 cm done --feature 3

建议:
- dolphin-a 修复 Feature 2 的测试失败（进入 worktree ../alfred-feature-2）
- dolphin-b 确认 AC 后标记 Feature 3 完成
```

**接力 agent 通过 progress 获得的完整信息**：
1. **Session 全局** — session_phase + 总体进度统计
2. **去哪工作** — worktree 路径（含 cd 命令）
3. **读什么** — feature_md 路径（包含 Analysis/Plan/AC/Dev Log）
4. **现在什么状况** — 各阶段子状态 + 测试输出摘要
5. **下一步做什么** — 分步操作指引（每步都是可执行的指令，无歧义）

**指引生成规则**：

`cm progress` 输出的 `action_steps` 是一个有序列表，每条都是可直接执行的指令。session 级和 feature 级分别生成：

**Session 级指引**：

| session_phase | PLAN.md 存在？ | 指引 |
|---------------|---------------|------|
| `locked` | 否 | 1. 分析需求 2. 创建 `.coding-master/PLAN.md`（按模板） |
| `locked` | 是 | 1. 用 `_cm_edit` 创建 PLAN.md（每个 feature 有 Task + AC + Depends on） 2. 运行 `_cm_start` 验证并推进 |
| `reviewed` | — | 1. 运行 `cm claim --feature N` 认领可用 feature |
| `working` | — | （显示各 feature 的指引） |
| `integrating` | — | 1. 运行 `cm submit --title "..."` 提交 |

**Feature 级指引**：

| phase | 子状态 | action_steps |
|-------|--------|--------------|
| `blocked` | — | 1. 等待依赖完成: Feature X, Y |
| `pending` | — | 1. 运行 `cm claim --feature N` |
| `analyzing` | `analysis == pending` | 1. `cd {worktree}` 2. 阅读 `{feature_md}` 中的 Spec 3. 在 Analysis 段落分析代码 |
| `analyzing` | `analysis == done, plan == pending` | 1. 在 `{feature_md}` 中撰写 Plan 2. Plan 写完后运行 `cm dev --feature N` |
| `analyzing` | `analysis == done, plan == done` | 1. 运行 `cm dev --feature N` 进入开发阶段 |
| `developing` | `test_status == pending` | 1. `cd {worktree}` 2. 阅读 `{feature_md}` 的 Plan 了解开发计划 3. 编写代码 4. `git commit` 5. 运行 `cm test --feature N` |
| `developing` | `test_status == failed` | 1. `cd {worktree}` 2. 阅读 `{feature_md}` 的 Dev Log 了解上下文 3. 失败原因: `{test_output}` 4. 修复代码 5. `git commit` 6. 运行 `cm test --feature N` |
| `developing` | `test_status == passed, test_commit ≠ latest` | 1. `cd {worktree}` 2. 代码在测试后有变更 3. 运行 `cm test --feature N` 重新测试 |
| `developing` | `test passed, test_commit == latest` | 1. 阅读 `{feature_md}` 确认 Acceptance Criteria 全部满足 2. 运行 `cm done --feature N` |
| `done` | — | ✓ 已完成 |

#### 3.4.5 隐式约定：pending feature 无需预写入

claims.json 是**按需填充**的——`cm claim` 首次认领时才写入对应 feature 的条目。未被认领的 feature 不在 claims.json 中。工具层对不存在的 feature 条目一律视为 `phase = "pending"`（见 `cmd_done` 的 `features.get(dep, {}).get("phase", "pending")` 等处）。

#### 3.4.6 为什么是 JSON 不是 MD

- **原子认领**：两个 agent 同时想认领 Feature 3，工具读 JSON → 检查未被认领 → 写入，用文件锁保证原子性
- **依赖检查**：工具检查 depends_on 的 feature 是否都 done，这是结构化查询
- **阶段内子状态**：每个阶段有独立的取值空间（analyzing 的 analysis/plan，developing 的 test_status/test_commit），程序精确判定当前进展
- **自动失效检测**：developing 阶段 `test_commit ≠ latest_commit` 或 `review_commit ≠ latest_commit` 时自动标记过期，不依赖 agent 自觉

### 3.5 features/XX.md — Feature 工作现场

**消费者**：认领该 feature 的 agent 写
**每个 feature 一个独立文件，各 agent 写自己的，无冲突。**

```markdown
# Feature 2: 实现新的 scan 逻辑

## Spec
> 从 PLAN.md 复制或引用，让 agent 不用来回跳文件

基于新的 SessionScanner 接口，实现支持增量 scan 的逻辑。

**Acceptance Criteria**:
- [x] 增量 scan：只处理上次 watermark 之后的新消息
- [ ] 性能：1000 条消息的 scan < 100ms
- [ ] 测试覆盖率 > 90%

## Analysis
- SessionScanner.scan() 当前是全量扫描
- 需要增加 watermark 参数，只处理 messages[watermark:]
- watermark 存在 ScanState 里，每次 scan 后更新

## Plan
1. [x] 给 ScanState 加 watermark 字段
2. [x] scan() 接受 watermark，只处理新消息
3. [ ] 性能测试：生成 1000 条消息，验证 < 100ms
4. [ ] 补充测试覆盖

## Test Results
```
tests/unit/test_scanner.py: 8/8 passed
tests/unit/test_inspector.py: 12/12 passed
覆盖率: 87% (目标 90%)
```

## Dev Log
- 11:30 增量 scan 逻辑完成，功能测试过了，覆盖率差一点
- 11:15 watermark 参数加好，基本逻辑通了
- 11:00 开始开发，先读 Feature 1 的代码
```

**设计要点**：

- **Spec section 从 PLAN.md 复制** — agent 在这一个文件里就有完整上下文
- **Acceptance Criteria 在这里打勾** — 这里是执行层，PLAN.md 是定义层
- **每个文件只有一个 owner** — 认领了 Feature 2 的 agent 独占写 `02-new-scan-logic.md`，不会和其他 agent 冲突
- **命名规则**：`{序号}-{slug}.md`，序号对应 PLAN.md 里的 Feature 编号

### 3.6 JOURNAL.md — 开发日志

**消费者**：所有 agent 追加写，submit 时用于生成 PR body
**生命周期**：lock 时创建 → 开发过程中 append-only → submit 后归档

```markdown
# Development Journal

## 2026-03-08T10:00 [dolphin-a] lock
Workspace locked, branch: dev/alfred-0308-1000

## 2026-03-08T10:05 [dolphin-a] plan
Task: 重构 inspector 模块，拆分 SessionScanner 和 ReportGenerator
Decomposed into 4 features, dependency chain: 1 → {2,3} → 4

## 2026-03-08T10:42 [dolphin-a] done feature-1
提取 SessionScanner 接口完成，12/12 tests passed

## 2026-03-08T10:43 [dolphin-a] claim feature-2
## 2026-03-08T10:43 [dolphin-b] claim feature-3

## 2026-03-08T11:15 [dolphin-b] done feature-3
拆分 ReportGenerator 完成，inspector.py 改为组合调用

## 2026-03-08T11:20 [dolphin-a] done feature-2
增量 scan 实现完成，性能 < 100ms 达标，覆盖率 92%

## 2026-03-08T11:45 [dolphin-a] done feature-4
集成测试补充完成，3 个场景全覆盖

## 2026-03-08T11:46 [dolphin-a] submit
All 4 features done. PR created.
```

**设计要点**：

- **Append-only + flock 保护** — 只追加，不修改历史条目。工具层通过 `flock + O_APPEND` 写入，保证多 agent 同时追加不会互相覆盖（裸 read→append→write 会丢失并发写入）
- **结构化前缀** — `## timestamp [agent] action` 格式，便于工具解析生成 PR body
- **与 features/XX.md 互补** — JOURNAL 记录全局时间线和里程碑，feature MD 记录细节分析和开发过程
- **与 PLAN.md 不同** — PLAN.md 是静态规格（写一次），JOURNAL.md 是动态过程（持续追加）
- **cm 工具自动追加关键事件** — 以下命令成功时自动往 JOURNAL.md 追加一行：`cm lock`、`cm plan-ready`、`cm claim`、`cm done`、`cm integrate`、`cm submit`。agent 可随时通过 `cm journal --message "..."` 补充上下文
- **PR body 生成** — `cm submit` 时 `_generate_pr_body` 从 JOURNAL.md 提取里程碑条目（done/submit action），结合 PLAN.md 的 feature 列表生成结构化 PR body；agent 可在 submit 前追加一条 summary entry 来丰富 PR 描述

### 3.7 Worktree 基点策略

Feature worktree 的创建基点取决于依赖关系：

| 场景 | 基点 | 原因 |
|------|------|------|
| 无依赖（Depends on: —） | dev branch（`cm lock` 创建的主分支） | 从干净基线开始 |
| 有依赖（Depends on: Feature N） | 最近完成的依赖 feature 的 branch | 需要依赖的代码改动 |
| 多依赖（Depends on: Feature M, N） | 从 dev branch 创建后 merge 所有依赖 branch | 需要多个 feature 的代码 |

**`cm claim` 自动处理基点**：

```
cm claim --feature 2  (depends on Feature 1)
→ git worktree add ../repo-feature-2 -b feat/2-xxx feat/1-scanner-interface
  （从 Feature 1 的 branch 创建 worktree）

cm claim --feature 4  (depends on Feature 2, Feature 3)
→ git worktree add ../repo-feature-4 -b feat/4-xxx dev/alfred-0308-1000
→ cd ../repo-feature-4
→ git merge feat/2-xxx feat/3-xxx --no-edit
  （从 dev branch 创建后 merge 两个依赖 branch）
```

**Merge 冲突处理**：如果依赖 branch 之间有冲突，`cm claim` 返回错误，agent 需要先在某个 worktree 中手动解决冲突后重试。

### 3.8 单 Feature vs 多 Feature

| | 单 Feature | 多 Feature |
|---|---------|---------|
| **场景** | 简单任务、无需拆分 | 需要拆分、可能多 agent |
| **PLAN.md** | 只有 1 个 feature | 有多个 feature |
| **claims.json** | 只有 Feature 1 | 多个 feature 的认领状态 |
| **工作现场** | `features/01-*.md` | `features/XX.md`（每个 feature 一个） |
| **session worktree** | 1 个 | 1 个 |
| **feature worktree** | 1 个 | 每个 feature 1 个 |

---

## 4. 并行开发流程

### 4.1 多 Agent 协作模型

```
              主 repo（不动，用户工作区不受影响）
                         │
                  cm lock --repo alfred
                         │
                         ▼
              session worktree（../alfred-session）
              dev branch: dev/alfred-0308-1000
                         │
                    PLAN.md（规格，只读）
                         │
              ┌──────────┼──────────┐
              ▼          ▼          ▼
         Agent A     Agent B    Agent C
           │            │          │
    cm claim 1    cm claim 2  cm claim 3
           │            │          │
           ▼            ▼          ▼
  feature worktree  feature wt  feature wt
  ../alfred-feat-1  ../alfred-  ../alfred-
      branch A      feat-2      feat-3
           │        branch B    branch C
           ▼            │          │
    features/       features/   features/
    01-xxx.md       02-xxx.md   03-xxx.md
    (独立写)        (独立写)    (独立写)
           │            │          │
    cm done 1     cm done 2   cm done 3
           │            │          │
           └──────────┬─┴──────────┘
                      ▼
               claims.json（汇总状态）
                      │
              全部 done → cm integrate（在 session worktree 中 merge）
                      │
              cm submit（在 session worktree 中 commit/push/PR）
                      │
              cleanup: 删除 session worktree + feature worktrees
```

**关键约束**：

- 每个 feature 有独立的 branch 和 worktree
- agent 只在自己 feature 的 worktree 内修改代码和跑测试
- `claims.json` 解决”谁负责哪个 feature”，worktree 解决”谁在改哪份代码”

### 4.2 认领流程

```
Agent A: "我来做 Feature 1"
                                    cm claim --feature 1
                                    → 工具检查 claims.json:
                                      Feature 1 未被认领 ✓
                                      Depends on: 无 ✓
                                    → 原子写入 claims.json
                                    → 创建 feature branch + worktree + feature lock
                                    → 创建 features/01-scanner-interface.md（从模板）
                                    → 返回 {"ok": true, "data": {"branch": "...", "worktree": "..."}}

Agent B: "我也想做 Feature 1"
                                    cm claim --feature 1
                                    → 工具检查 claims.json:
                                      Feature 1 已被 Agent A 认领 ✗
                                    → 返回 {"ok": false, "error": "already claimed by dolphin-a"}

Agent B: "那我做 Feature 3"
                                    cm claim --feature 3
                                    → 工具检查 claims.json:
                                      Feature 3 未被认领 ✓
                                      Depends on: Feature 1 → phase developing ✗
                                    → 返回 {"ok": false, "error": "blocked: Feature 1 not done"}

Agent A 完成 Feature 1:
                                    cm done --feature 1
                                    → 更新 claims.json: phase → done
                                    → 返回 {"ok": true, "unblocked": ["Feature 3"]}

Agent B: "Feature 3 解锁了"
                                    cm claim --feature 3
                                    → Feature 1 已 done ✓
                                    → 认领成功
```

### 4.3 总进度查看

`cm progress` 工具读 claims.json，返回汇总：

```json
{
  "ok": true,
  "data": {
    "total": 4,
    "done": 1,
    "developing": 2,
    "pending": 0,
    "blocked": 1,
    "features": [
      {"id": "1", "title": "提取 SessionScanner 接口", "phase": "done", "agent": "dolphin-a", "branch": "feat/scanner-interface"},
      {"id": "2", "title": "实现新的 scan 逻辑", "phase": "developing", "agent": "dolphin-a", "branch": "feat/new-scan"},
      {"id": "3", "title": "拆分 ReportGenerator", "phase": "developing", "agent": "dolphin-b", "branch": "feat/report-generator"},
      {"id": "4", "title": "补充集成测试", "phase": "pending", "blocked_by": ["2", "3"]}
    ]
  }
}
```

---

## 5. 研发流程指令状态转移总览

四种模式对应四种研发流程，每种流程有独立的指令序列和状态转移路径。

### 5.1 Deliver 模式 — Feature 交付流程

**最完整的流程，涉及 session + feature 两级状态机。**

```
                          Session 状态机
                          ═════════════

cm start        cm start(+PLAN)     cm claim(首次)    cm integrate      cm submit
   │                │                    │                │                │
   ▼                ▼                    ▼                ▼                ▼
┌────────┐    ┌──────────┐        ┌──────────┐    ┌─────────────┐    ┌──────┐
│ locked │───►│ reviewed │───────►│ working  │───►│ integrating │───►│ done │
└────────┘    └──────────┘        └──────────┘    └─────────────┘    └──────┘
                                        ▲                │
                                        └────────────────┘
                                       cm reopen + 修复后重试


                          Feature 状态机（每个 Feature 独立）
                          ═══════════════════════════════

cm claim           cm dev          cm test    cm review      cm done
   │                 │                │          │                │
   ▼                 ▼                ▼          ▼                ▼
┌─────────┐    ┌───────────┐    ┌──────────────────────┐    ┌──────────┐
│ pending │───►│ analyzing │───►│      developing      │───►│   done   │
└─────────┘    └───────────┘    └──────────────────────┘    └──────────┘
                                        │  ▲
                                        │  │ cm reopen
                                        │  │
                               ┌────────┴──┴──────────────┐
                               │  developing 子状态（双层） │
                               │                          │
                               │  内层 test 循环:           │
                               │  edit → commit → test     │
                               │    ↑        failed ──┘    │
                               │    └─────────────────     │
                               │     passed ↓              │
                               │                           │
                               │  外层 review 循环:         │
                               │  review (独立 engine)      │
                               │    ↑  changes_req ──┘     │
                               │    └──────────────────    │
                               │     approved → done       │
                               │                           │
                               │  代码变更后:                │
                               │  test passed → stale      │
                               │  review approved → stale  │
                               └───────────────────────────┘
```

**Deliver 指令序列与关键状态**：

| 步骤 | 指令 | Agent 调用 | Session Phase | Feature Phase | 关键状态字段 | 前置条件 |
|------|------|-----------|--------------|---------------|-------------|---------|
| 1 | `cm start` | `_cm_start` | → `locked`/`reviewed` | — | `mode=deliver`, `branch`, `session_worktree` | 无活跃 session（或自动 join） |
| 2 | 创建 PLAN.md + `cm start` | `_cm_edit` + `_cm_start` | → `reviewed` | — | `plan_reviewed_at` | PLAN.md 存在且合法 |
| 3 | `cm claim --feature N` | `_cm_claim` | → `working`* | → `analyzing` | `agent`, `branch`, `worktree` | 无阻塞依赖，未被认领 |
| 4 | `cm dev --feature N` | `_cm_dev` | — | → `developing` | `analyzing.completed_at`, `developing.started_at` | Analysis + Plan 段落非空 |
| 5 | `cm test --feature N` | `_cm_test` | — | — (子状态更新) | `test_status`, `test_commit`, `test_output` | phase=developing，无未提交变更 |
| 6 | `cm review --feature N` | — | — | — (子状态更新) | `review_status`, `review_commit`, `review_output` | test 通过且 test_commit=HEAD |
| 7 | `cm done --feature N` | `_cm_done` | — | → `done` | `completed_at`, `diff_url` | test 通过 + review approved，均不 stale |
| 8 | `cm integrate` | `_cm_integrate` | → `integrating` | — | `integration_passed_at` | 所有 feature done |
| 9 | `cm submit` | `_cm_submit` | → `done` | — | PR URL | 集成测试通过 |

> **注意**：`cm plan-ready` 不作为独立工具暴露给 agent（无 `_cm_plan_ready`）。`_cm_start` 内部自动调用 plan-ready。Agent 的操作路径是：`_cm_edit` 创建 PLAN.md → `_cm_start` 验证并推进到 reviewed。

\* 仅首次 claim 时从 `reviewed` → `working`，后续 claim 不变更 session phase。

**回退路径**：`cm reopen --feature N` 将 feature `done` → `developing`（重置 test_status=pending），同时 session `integrating` → `working`。

---

### 5.2 Review 模式 — 代码审查流程

**只读模式，不创建 session worktree，不修改 git 状态。无 feature 状态机。**

```
cm lock          cm scope     cm read/grep/find    cm report        cm unlock
(mode=review)        │         cm engine-run           │              (自动)
   │                 │              │                  │                │
   ▼                 ▼              ▼                  ▼                ▼
┌────────┐      ┌─────────┐   ┌──────────┐      ┌──────────┐     ┌──────────┐
│ locked │─────►│ scoped  │──►│ analyzed │─────►│ reported │────►│ unlocked │
└────────┘      └─────────┘   └──────────┘      └──────────┘     └──────────┘
                (scope.json)                     (report.md)
                                    │
                                    ├── cm engine-run（深度分析，可选）
                                    └── cm read + cm grep（轻量分析）
```

> 注：`scoped` / `analyzed` / `reported` 不是 `session_phase` 的值（session_phase 始终为 `locked`），
> 而是通过 artifact 存在性隐式表达的进度。`cm progress` 检测 artifact 缺失来生成指引。

| 步骤 | 指令 | 产出 Artifact | 关键参数 | 说明 |
|------|------|--------------|---------|------|
| 1 | `cm lock --mode review` | lock.json (`read_only=true`) | `--diff`, `--files`, `--pr` | 只读锁，可叠加在 write session 上 |
| 2 | `cm scope` | scope.json | `--diff`, `--files`, `--pr`, `--goal` | 定义审查范围 |
| 3a | `cm engine-run` | engine_result.json | `--goal`, `--engine` | 委托引擎深度分析（可选，可降级） |
| 3b | `cm read`/`cm grep`/`cm find` | — | 文件路径、pattern | 轻量级代码阅读和搜索（engine 的替代/补充） |
| 4 | `cm report` | report.md | `--content` / `--file` | 写入审查报告，**自动触发 unlock** |

**完成条件**：`report.md` 存在。`cm report` 成功后自动调用 `cm unlock`。

**模式升级**：review 发现问题后需要修复时，直接 `cm lock --mode debug` 原地升级为可写模式（见 §5.6）。

---

### 5.3 Debug 模式 — 问题诊断与修复流程

**可读写模式，与 deliver 共享 session worktree。可选修改代码。**

```
cm lock          cm scope     cm read/grep/find    cm report
(mode=debug)         │         cm engine-run           │
   │                 │              │                  │
   ▼                 ▼              ▼                  ▼
┌────────┐      ┌─────────┐   ┌──────────┐      ┌───────────┐
│ locked │─────►│ scoped  │──►│ analyzed │─────►│ diagnosed │──► cm unlock（手动）
└────────┘      └─────────┘   └──────────┘      └───────────┘
                                                 (diagnosis.md)
                                    │
                                    ▼ (可选：发现需要修复)
                               cm edit + cm test
                               (直接修改代码)
```

| 步骤 | 指令 | 产出 Artifact | 说明 |
|------|------|--------------|------|
| 1 | `cm lock --mode debug` | lock.json | 可读写，可叠加在 deliver session 上 |
| 2 | `cm scope` | scope.json | 定义调查范围 |
| 3a | `cm engine-run` | engine_result.json | 引擎深度分析（可选，可降级） |
| 3b | `cm read`/`cm grep`/`cm find` | — | 轻量级代码阅读和搜索 |
| 4 | `cm report` | diagnosis.md | 写入诊断报告，**不自动 unlock** |
| 5a | `cm unlock` | — | 仅诊断不修复时，手动 unlock |
| 5b | `cm edit` + `cm test` | 代码变更 | 需要修复时，直接编辑并验证 |

**与 Review 的区别**：允许 `cm edit` 和 `cm test` 指令，产出文件为 `diagnosis.md`（非 `report.md`）。

**v4.5 修正**：debug 模式下 `cm report` **不再自动 unlock**。理由：debug 的典型使用模式是"诊断 → 修复"，写完诊断报告后用户大概率要继续修复，自动 unlock 会打断这个自然流程。用户不修复时显式 `cm unlock` 即可。

**完成条件**：`diagnosis.md` 存在 + 手动 unlock。

---

### 5.4 Analyze 模式 — 结构化代码分析流程

**只读模式，流程与 Review 完全相同，仅语义不同。**

```
cm lock          cm scope     cm read/grep/find    cm report
(mode=analyze)       │         cm engine-run           │
   │                 │              │                  │
   ▼                 ▼              ▼                  ▼
┌────────┐      ┌─────────┐   ┌──────────┐      ┌──────────┐
│ locked │─────►│ scoped  │──►│ analyzed │─────►│ reported │──► unlock
└────────┘      └─────────┘   └──────────┘      └──────────┘
                                                 (report.md)
```

指令序列与 Review 相同。区别在于 engine prompt 模板和 scope 的语义（分析理解 vs 质量审查）。

**完成条件**：`report.md` 存在。

---

### 5.5 四种模式对比

| 维度 | Deliver | Review | Debug | Analyze |
|------|---------|--------|-------|---------|
| **读写** | 读写 | 只读 | 读写 | 只读 |
| **Session Worktree** | 创建 | 不创建 | 复用/创建 | 不创建 |
| **状态机层级** | Session + Feature 两级 | 仅 Artifact 进度 | Artifact + 可选 Feature | 仅 Artifact 进度 |
| **Feature 工作流** | claim→dev→test→done | 无 | 可选 edit/test | 无 |
| **文件操作** | read/find/grep/edit | read/find/grep（只读） | read/find/grep/edit | read/find/grep（只读） |
| **产出** | PR + 代码 | report.md | diagnosis.md (+ 可选代码) | report.md |
| **完成条件** | 所有 feature done + evidence | report.md 存在 | diagnosis.md 存在 + 手动 unlock | report.md 存在 |
| **自动 unlock** | 否（需 submit） | 是（report 后） | **否（手动 unlock）** | 是（report 后） |
| **Overlay 支持** | 否（排他） | 是 | 是 | 是 |
| **cm lock 升级** | — | → debug | → debug | — |

### 5.6 cm lock 的幂等升级语义

**问题**：review 发现问题后用户说"帮我修"，agent 需要从只读模式切换为可写模式。早期设计没有这条路径，导致 agent 陷入模式切换混乱（先 unlock review → 再 lock debug → 底下已有 write session 则 unlock 被拒）。

**解决**：不新增命令。`cm lock --mode debug` 自身承担"创建 / 加入 / 升级"三种语义，agent 不需要判断当前状态，永远先试 `cm lock --mode X`，工具自己裁决。

**cm lock 状态裁决矩阵**：

| 当前状态 | `cm lock --mode debug` 行为 |
|---------|---------------------------|
| 无 session | 正常创建 debug session |
| review/analyze 主 session | **原地升级**：改 `mode=debug`、`read_only=false`，创建 session worktree |
| write session 上叠加的 review overlay | 不新建锁，返回"底层 write session 已存在，直接使用" |
| 已是 debug/deliver | join，保持幂等 |

**只允许两类升级**：
- `review` → `debug`
- `analyze` → `debug`

不支持 → `deliver`。deliver 绑定的是 feature 开发流（PLAN.md、claim、integrate），不适合从审查结果直接进入。"基于审查结果继续调查/修复"的语义是 debug，不是 deliver。

**实现要点**：
- 升级是原子操作：`_atomic_json_update` 修改 lock.json 的 `mode`/`read_only` 字段
- 升级时保留已有 artifact（scope.json, engine_result.json, report.md）
- 升级到写模式时需要创建 session worktree（如果还没有）
- `cmd_lock()` 现有逻辑已经在做"无 session 创建 / 有 session join / read-only overlay"分支决策，升级只是补完这套状态机

**配套修复**：
- `cmd_report()` 必须去掉 debug 的 auto-unlock。debug 是读写模式，report 只是中间产出，不是终态。只有 review/analyze 的 report 才自动 unlock。

---

### 5.7 关键状态字段速查

**Session 级（lock.json）**：

| 字段 | 说明 | 写入时机 |
|------|------|---------|
| `session_phase` | `locked`→`reviewed`→`working`→`integrating`→`done` | 各阶段指令 |
| `mode` | `deliver`/`review`/`debug`/`analyze` | `cm lock` |
| `branch` | dev 分支名 | `cm lock` |
| `session_worktree` | session worktree 路径 | `cm lock`（写模式） |
| `read_only` | 是否只读 | `cm lock` |
| `lease_expires_at` | Lease 到期时间 | `cm lock`，写命令自动续约 |
| `session_agents` | 参与的 agent 列表 | `cm lock`（join），`cm claim` |

**Feature 级（claims.json）**：

| 字段 | 所属阶段 | 说明 | 写入时机 |
|------|---------|------|---------|
| `phase` | 全局 | `pending`→`analyzing`→`developing`→`done` | claim/dev/done/reopen |
| `agent` | 全局 | 认领者 identity | `cm claim` |
| `branch` | 全局 | feature 分支名 | `cm claim` |
| `worktree` | 全局 | feature worktree 路径 | `cm claim` |
| `analyzing.analysis` | analyzing | `pending`/`done` | `cm dev` 检查 |
| `analyzing.plan` | analyzing | `pending`/`done` | `cm dev` 检查 |
| `developing.test_status` | developing | `pending`/`passed`/`failed` | `cm test` |
| `developing.test_commit` | developing | 测试时的 HEAD SHA | `cm test` |
| `developing.latest_commit` | developing | 当前最新 commit SHA | `cm test` |
| `developing.test_output` | developing | 测试输出摘要（≤500 字符） | `cm test` |
| `developing.commit_count` | developing | feature 分支 commit 数 | `cm test` |
| `completed_at` | done | 完成时间戳 | `cm done` |

**Evidence 层（evidence/N-verify.json）**：

| 字段 | 说明 | 写入时机 |
|------|------|---------|
| `overall` | `passed`/`failed`/`skipped` | `cm test` |
| `commit` | 测试时的 HEAD SHA | `cm test` |
| `lint.passed` | lint 是否通过 | `cm test` |
| `typecheck.passed` | 类型检查是否通过 | `cm test` |
| `test.passed` | 单元测试是否通过 | `cm test` |

---

## 6. SKILL.md 公约设计

```markdown
---
name: coding-master
description: "Code development conventions and minimal tooling"
version: "3.0.0"
---

# Coding Master

## 工作目录

所有开发状态存放在目标 repo 的 `.coding-master/` 目录下（被 .gitignore 忽略）：

| 文件 | 形态 | 用途 | 谁维护 |
|------|------|------|--------|
| `lock.json` | JSON | workspace 锁 | 工具 |
| `PLAN.md` | MD | feature 规格 | 你创建，之后只读 |
| `JOURNAL.md` | MD | 开发日志（append-only） | 工具自动 + 你补充 |
| `claims.json` | JSON | feature 认领状态 | 工具 |
| `features/XX.md` | MD | 每个 feature 的工作现场 | 认领者 |

**原则**：JSON 给工具做原子操作（锁、认领），MD 给你读写（规格、工作记录）。
**SKILL.md 不可变**：你不得修改 SKILL.md，它是人类定义的公约。违规由 inspector 检测并提醒纠正。

## 开发流程

### 统一 Feature Workflow

**Session 级**：
1. **锁定** — `cm lock --repo <name>`（session: locked）
2. **规划** — 创建 PLAN.md，定义 feature 列表和验收标准
3. **审核** — `cm start` 内部调用 plan-ready 检查 PLAN.md 格式和内容完整性（session: locked → reviewed）

**Feature 级**（每个 feature 重复）：
4. **认领** — `cm claim --feature <n>`（feature: pending → analyzing，session: working）
5. **分析** — 在 `features/XX.md` 中撰写 Analysis + Plan
6. **进入开发** — `cm dev --feature <n>`（feature: analyzing → developing）
7. **开发** — 在 worktree 里改代码，commit
8. **测试** — `cm test --feature <n>`（通过：test_status=passed / 失败：test_status=failed）
9. **修复循环** — 测试失败 → 读 test_output → 改代码 → commit → `cm test` → 直到通过
10. **完成** — `cm done --feature <n>`（feature: developing → done，需 test_status=passed 且 test_commit=HEAD）
11. **继续** — 认领下一个可用 feature，重复 4-10

**收尾**：
12. **查看进度** — `cm progress` 展示 session + 各 feature 的状态和自然语言指引
13. **集成验证** — 全部 feature done 后 `cm integrate`（merge 所有 feature branch → 跑全量测试 → session: integrating）
14. **集成失败修复** — 如果集成测试失败：`cm reopen --feature <n>` 重新打开需要修复的 feature → 修复 → `cm test` → `cm done` → 重新 `cm integrate`
15. **提交** — 集成通过后 `cm submit`（session: done）

### 跨 Agent 并行

- 多个 agent 可以同时认领不同 feature（各自 `cm claim`）
- 每个 agent 只编辑自己的 `features/XX.md`，不冲突
- `cm claim` 自动检查依赖，被阻塞的 feature 无法认领
- `cm done` 完成时自动通知被解锁的 feature

## 工具

工具分两类：**文件操作**（agent 的"手和眼"）和**流程控制**（状态推进和环境交互）。

### 文件操作工具

| 工具 | 用途 | 可用模式 |
|------|------|---------|
| `cm read --file <path> [--start N] [--end N]` | 读文件内容（支持行范围） | 全部 |
| `cm find --pattern <glob>` | 按 glob 模式查找文件 | 全部 |
| `cm grep --pattern <regex> [--glob <filter>]` | 搜索文件内容，返回文件名+行号+上下文 | 全部 |
| `cm edit --file <path> --old <text> --new <text>` | 精确替换编辑文件（受控 patch 模型） | deliver, debug |

### 流程控制工具

| 工具 | 用途 |
|------|------|
| `cm lock --repo <name> [--mode M]` | 锁定 workspace（创建 / 加入 / 升级，见 §5.6） |
| `cm unlock` | 释放锁 |
| `cm plan-ready` | 检查 PLAN.md 格式完整性 → session: locked → reviewed |
| `cm claim --feature <n>` | 认领 feature，创建 feature branch/worktree/lock 和 features/XX.md |
| `cm dev --feature <n>` | 检查 Analysis+Plan 已写 → analyzing → developing |
| `cm test --feature <n>` | 跑测试 → 写入 test_status/test_commit/test_output |
| `cm review --feature <n> [--engine E]` | 独立 engine 审查 diff → 写入 review_status/review_output，通过时返回 diff_url |
| `cm done --feature <n>` | 检查 test + review 均 passed 且不 stale → developing → done |
| `cm reopen --feature <n>` | 集成失败后重新打开 feature → done → developing（重置 test_status） |
| `cm integrate` | 全部 feature done 后：merge feature branches → 跑全量测试 → session: integrating |
| `cm progress` | 展示 session 状态 + 各 feature 阶段/子状态 + 分步操作指引 |
| `cm submit --repo <name> --title "..."` | 幂等提交：push → PR，成功后自动 unlock（需 session: integrating） |
| `cm renew` | 续租当前 lock 的 lease（长任务防超时） |
| `cm journal --message "..."` | 向 JOURNAL.md 追加一条记录（flock 保护） |
| `cm doctor --repo <name>` | 诊断状态一致性，`--fix` 自动修复 |
| `cm status` | 显示当前锁状态 |
| `cm scope [--diff R] [--files F] [--pr N] [--goal G]` | 定义分析范围（review/debug/analyze 模式） |
| `cm report [--content C] [--file F]` | 写入报告或诊断（review/analyze → 自动 unlock；debug → 不自动 unlock） |
| `cm engine-run [--goal G] [--engine E] [--timeout T]` | 委托引擎深度分析（失败时返回降级提示） |

## 规则

1. **所有代码修改限定在目标 repo 内**
2. **不 push main/master，始终在 feature branch 上**
3. **不 force push**
4. **不修改 SKILL.md** — 它是不可变公约（inspector 会检测违规）
5. **保持工作现场 MD 更新** — `features/XX.md` 是你的工作记录
6. **JOURNAL.md 只追加不修改** — 通过 `cm journal` 补充上下文，不要直接编辑文件（flock 保护防并发丢失）
7. **先 cm test 再 cm done** — cm done 不跑测试，只检查 developing 子状态中 test_status=passed 且 test_commit=HEAD；代码改了必须重新 cm test
8. **开发完释放锁**
9. **只编辑自己认领的 feature MD** — 不动别人的文件
10. **只在自己的 feature worktree 中改代码** — 不进入别人的 worktree

## 模板

### PLAN.md

    # Feature Plan

    ## Origin Task
    <!-- 原始任务描述 -->

    ## Features

    ### Feature 1: ...
    **Depends on**: —

    #### Task
    <!-- 描述 -->

    #### Acceptance Criteria
    - [ ] ...

    ---

    ### Feature 2: ...
    **Depends on**: Feature 1

    #### Task
    #### Acceptance Criteria

### features/XX.md

    # Feature N: <title>

    ## Spec
    > 从 PLAN.md 复制

    **Acceptance Criteria**:
    - [ ] ...

    ## Analysis
    ## Plan
    ## Test Results
    ## Dev Log
```

---

## 7. 工具设计

### 7.1 整体结构

```
skills/coding-master/
├── SKILL.md                     # 公约层：不可变的流程定义
├── coding_master_skillkit.py    # Dolphin 原生 tools 注册（替代 _bash + cm CLI）
├── scripts/
│   ├── tools.py                 # 核心工具（~3600 行，含 v4 证据/委托/模式/engine/文件操作）
│   ├── engine.py                # Engine 抽象 + ClaudeCodeEngine 实现
│   ├── config_manager.py        # 配置管理
│   └── test_runner.py           # 测试运行器
└── tests/
    └── unit/
        ├── test_engine.py       # Engine 单元测试
        └── test_skillkit.py     # Skillkit 注册测试 + hints 一致性守护
```

#### 两层工具架构（v5.0）

v4.6 的四层分组（L0-L3）缓解了认知过载，但 25 个工具仍全部暴露给 agent，根因未解决。v5.0 的核心转变：**agent 不再编排流程，系统自动推进，agent 只在创造性断点停下来写代码**。

```
┌─────────────────────────────────────────────────────────────────┐
│  Agent 层（7 个工具 — agent 直接调用）                             │
│                                                                 │
│  _cm_next     流水线推进（唯一的流程入口）                          │
│               自动跑完所有机械步骤，在断点停下                       │
│               intent 参数触发特定操作（test / scope）              │
│                                                                 │
│  _cm_edit     编辑文件（plan / feature MD / 源码）                │
│               .coding-master/*.md 元数据无门槛放行                │
│               源码需 feature 在 developing 阶段                   │
│                                                                 │
│  _cm_read     读文件（无 lock 时 fallback 到原始仓库）             │
│  _cm_find     找文件                                             │
│  _cm_grep     搜索内容                                           │
│                                                                 │
│  _cm_status   查看状态 + 进度 + repos 列表                        │
│               无 repo 参数 → 列出所有 repos                       │
│               有 repo 参数 → 完整 session/feature 状态             │
│                                                                 │
│  _cm_doctor   诊断 + 修复（fix=True 时有写操作）                   │
│                                                                 │
│  设计原则：agent 日常只用 _cm_next + _cm_edit 两个工具。           │
│  read/find/grep 用于理解代码。status/doctor 用于调试。             │
├─────────────────────────────────────────────────────────────────┤
│  Internal 层（不暴露给 agent — _cm_next 内部调用）                 │
│                                                                 │
│  Session:  cmd_lock / cmd_unlock / cmd_start / cmd_plan_ready   │
│  Feature:  cmd_claim / cmd_dev / cmd_test / cmd_done            │
│            cmd_reopen / cmd_integrate / cmd_submit              │
│  开发:    cmd_git / cmd_journal / cmd_change_summary            │
│  Review:  cmd_scope / cmd_engine_run / cmd_report               │
│  辅助:    cmd_regression / cmd_renew                             │
│                                                                 │
│  仍可通过 CLI (cm lock, cm claim ...) 手动调用。                  │
│  CLI 面向高级用户和调试，不影响 agent 工作流。                       │
└─────────────────────────────────────────────────────────────────┘
```

#### `_cm_next` 断点模式

Agent 只需要反复调用 `_cm_next`，系统告诉它做什么：

```
Agent 典型流程:

  _cm_next(repo)           → breakpoint: write_plan + template
  _cm_edit(PLAN.md)
  _cm_next(repo)           → auto plan-ready ✓ auto claim ✓
                             breakpoint: write_analysis + feature spec
  _cm_edit(features/01.md)
  _cm_next(repo)           → auto dev ✓
                             breakpoint: write_code + worktree 路径
  _cm_edit(src/foo.py)
  _cm_next(repo, intent="test")  → auto test ✓ auto done ✓
                                   breakpoint: write_analysis + feature 2
  ... 循环 ...
  _cm_next(repo)           → all done, auto integrate ✓
                             auto title (从 PLAN.md 提取) → auto submit ✓
                             breakpoint: complete + PR URL
                             （若 PLAN.md 无法提取 title → breakpoint: need_title）
```

断点返回值契约：

```json
{
  "ok": true,
  "breakpoint": "write_code",
  "feature": 1,
  "worktree": "/path/to/alfred-feature-1",
  "instruction": "编写代码实现 Feature 1，完成后调 _cm_next(intent='test')",
  "context": {"title": "...", "task": "...", "acceptance_criteria": ["..."]}
}
```

`_cm_next` 参数：

| 参数 | 类型 | 说明 |
|------|------|------|
| `repo` | str | 必需，repo 名称 |
| `mode` | str | 首次调用指定（deliver/review/debug/analyze），之后从 lock.json 读 |
| `force` | bool | mode 冲突时强制切换（无进展的 session 自动切换，有 working/integrating 进展时需 force） |
| `intent` | str | 触发特定操作：`test`（跑测试）、`scope`（定义分析范围，也可直接传 `diff`/`files`） |
| `diff` | str | 分析范围的 diff range（直接传即可，自动识别为 scope intent） |
| `files` | str | 分析范围的 file list（直接传即可，自动识别为 scope intent） |
| `title` | str | PR 标题（可选，缺失时从 PLAN.md 自动提取） |

#### 断点类型总表

| breakpoint | 模式 | 含义 | Agent 接下来做什么 |
|------------|------|------|:---|
| `write_plan` | deliver | PLAN.md 不存在 | `_cm_edit` 写 PLAN.md（返回值含 template） |
| `fix_plan` | deliver | PLAN.md 格式解析失败 | `_cm_edit` 修 PLAN.md（返回值含格式说明） |
| `write_analysis` | deliver | Feature 已 claim，需写 Analysis+Plan | `_cm_edit` 填 feature markdown |
| `write_code` | deliver | Feature 进入 developing | `_cm_edit` 写源码，完成后 `_cm_next(intent="test")` |
| `fix_code` | deliver | 测试失败 | `_cm_edit` 修代码，再 `_cm_next(intent="test")` |
| `fix_integration` | deliver | 集成测试失败 | `_cm_edit` 修代码，再 `_cm_next(intent="test")` |
| `need_title` | deliver | integration 完成，等待 PR title | `_cm_next(repo=..., title="feat: ...")` |
| `complete` | all | 全流程完成 | 无需操作（deliver 模式返回 PR URL） |
| `mode_conflict` | all | 当前 session mode 与请求 mode 不符 | `_cm_next(repo=..., mode=..., force=True)` 强制切换 |
| `define_scope` | review/debug | 需要定义分析范围 | `_cm_next(intent="scope", diff="...")` |
| `write_report` | review/debug | Engine 分析完成，需要写报告 | **先** `_cm_edit` 写 report/diagnosis，**再** `_cm_next` 完成 |

#### Hints 一致性守护

`TestHintToolConsistency`（`tests/unit/test_skillkit.py`）确保所有 hints/errors 中的 `_cm_xxx` 引用都存在于 skillkit 注册表中。v5.0 中注册表只有 7 个工具，hints 只应引用这 7 个。

### 7.2 工具实现概要

```python
#!/usr/bin/env python3
"""Coding Master 最小工具集。

每个工具做一件机械的事，不做编排。
JSON 文件通过 flock 保证原子性。
并行开发通过 per-feature worktree 隔离代码工作区。
"""

import argparse, copy, fcntl, json, os, subprocess, sys, tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

CONFIG_PATH = Path("~/.alfred/config.yaml").expanduser()  # coding_master section
CM_DIR = ".coding-master"
LEASE_MINUTES = 120

# ── 原子 JSON 操作 ────────────────────────────────────

def _atomic_json_update(path: Path, updater):
    """flock + read-modify-write，保证并发安全。

    updater 返回 dict，若 result["ok"] 为 False 则不写入（回滚语义）。
    updater 应在确认成功后再修改 data，或使用 copy 避免副作用。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    with open(path, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            content = f.read()
            try:
                data = json.loads(content) if content.strip() else {}
            except json.JSONDecodeError:
                data = {}  # 损坏的 JSON 降级为空，不崩溃
            snapshot = copy.deepcopy(data)  # 深拷贝快照，用于回滚
            result = updater(data)
            if result.get("ok", True):  # 成功才写入
                f.seek(0)
                f.truncate()
                json.dump(data, f, indent=2, ensure_ascii=False)
            else:
                # updater 失败，恢复原始数据（防止 updater 意外修改了 data）
                if data != snapshot:
                    f.seek(0)
                    f.truncate()
                    json.dump(snapshot, f, indent=2, ensure_ascii=False)
            return result
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)

def _atomic_json_read(path: Path) -> dict:
    """flock 保护的只读操作。预检查用，不修改文件。"""
    if not path.exists():
        return {}
    with open(path, "r") as f:
        fcntl.flock(f, fcntl.LOCK_SH)  # 共享锁，允许并发读
        try:
            content = f.read()
            return json.loads(content) if content.strip() else {}
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)

# ── cm lock ───────────────────────────────────────────

def _resolve_agent(args) -> str:
    """解析 agent identity：优先用 --agent 参数，fallback 到 hostname-pid。"""
    if getattr(args, 'agent', None):
        return args.agent
    import socket
    return f"{socket.gethostname()}-{os.getpid()}"

def cmd_lock(args) -> dict:
    """原子锁定 workspace，创建 lock.json。"""
    repo = _repo_path(args.repo)
    lock_path = repo / CM_DIR / "lock.json"

    # 检查 working tree 是否 clean，避免带着脏状态创建 dev branch
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
    )
    if status.stdout.strip():
        return {"ok": False, "error": "working tree not clean, commit or stash first"}

    agent = _resolve_agent(args)
    reserved = {}

    def reserve_lock(data):
        if data and not _is_expired(data):
            return {"ok": False, "error": "already locked", "data": data}

        now = datetime.now(timezone.utc)
        branch = args.branch or f"dev/{args.repo}-{now.strftime('%m%d-%H%M')}"
        reserved.update({
            "repo": args.repo,
            "session_phase": "locked",
            "branch": branch,
            "locked_by": agent,
            "locked_at": now.isoformat(),
            "lease_expires_at": (now + timedelta(minutes=LEASE_MINUTES)).isoformat(),
            "session_agents": [agent],
        })
        data.clear()
        data.update(reserved)
        return {"ok": True}

    result = _atomic_json_update(lock_path, reserve_lock)
    if not result.get("ok"):
        return result

    # 在独立 worktree 中创建 dev branch，不污染主 repo 工作区
    session_worktree = str(repo.parent / f"{repo.name}-session")
    try:
        _run_git(repo, ["worktree", "add", session_worktree, "-b", reserved["branch"]])
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        # 回滚 lock.json（原子写空）
        _atomic_json_update(lock_path, lambda data: (data.clear(), {"ok": True})[1])
        return {"ok": False, "error": f"session worktree creation failed: {exc}"}

    # 记录 session_worktree 到 lock.json
    _atomic_json_update(lock_path, lambda d: (
        d.update({"session_worktree": session_worktree}), {"ok": True}
    )[1])

    _ensure_gitignore(repo)
    return {"ok": True, "data": {"branch": reserved["branch"], "session_worktree": session_worktree}}

# ── cm claim ──────────────────────────────────────────

def cmd_claim(args) -> dict:
    """原子认领一个 feature，并为其创建独立 branch/worktree。

    写入顺序（崩溃安全）：
    1. 预检查（读 claims.json，不写入）— 快速失败
    2. 创建 worktree + feature MD — 可逆副作用
    3. 最后原子写入 claims.json — 提交点

    如果在步骤 2 崩溃：claims.json 未更新，feature 仍是 pending，
    残留的 worktree/MD 由 cm doctor 清理。
    """
    repo = _resolve_locked_repo(args)
    claims_path = repo / CM_DIR / "claims.json"
    lock_path = repo / CM_DIR / "lock.json"
    feature_id = str(args.feature)
    agent = _resolve_agent(args)

    # 检查 lease 是否过期
    lease_check = _check_lease(repo)
    if not lease_check["ok"]:
        return lease_check

    # ── 步骤 0：检查 session_phase（必须在 claims.json 写入之前） ──
    lock = _atomic_json_read(lock_path)
    if lock.get("session_phase") == "locked":
        return {"ok": False, "error": "session is locked, "
                "create PLAN.md via cm edit, then run cm start to validate before claiming features"}
    if lock.get("session_phase") not in ("reviewed", "working"):
        return {"ok": False, "error": f"session is {lock.get('session_phase')}, cannot claim"}

    # 读 PLAN.md 获取 feature 信息
    plan = _parse_plan_md(repo / CM_DIR / "PLAN.md")
    if feature_id not in plan:
        return {"ok": False, "error": f"Feature {feature_id} not found in PLAN.md"}

    # ── 步骤 1：预检查（只读，不写） ──
    pre_check = _atomic_json_read(claims_path)
    features = pre_check.get("features", {})
    if feature_id in features and features[feature_id].get("phase") != "pending":
        existing = features[feature_id]
        return {"ok": False, "error": f"already {existing.get('phase')} by {existing.get('agent')}"}
    deps = plan[feature_id].get("depends_on", [])
    for dep in deps:
        dep_phase = features.get(dep, {}).get("phase", "pending")
        if dep_phase != "done":
            return {"ok": False, "error": f"blocked: Feature {dep} is {dep_phase}"}

    branch = f"feat/{feature_id}-{_slugify(plan[feature_id]['title'])}"
    worktree = str(repo.parent / f"{repo.name}-feature-{feature_id}")

    # ── 步骤 2：先创建 worktree + MD（可逆副作用） ──
    # 根据依赖关系确定 worktree 基点
    dep_branches = [features[d]["branch"] for d in deps if d in features]
    try:
        _create_feature_worktree(repo, branch, worktree, base_branches=dep_branches)
    except Exception as exc:
        return {"ok": False, "error": f"worktree creation failed: {exc}"}

    spec = plan[feature_id]
    slug = _slugify(spec["title"]) or f"feature-{feature_id}"
    feature_md = repo / CM_DIR / "features" / f"{feature_id.zfill(2)}-{slug[:30]}.md"
    _write_file(feature_md, FEATURE_TEMPLATE.format(
        id=feature_id, title=spec["title"],
        task=spec.get("task", ""), criteria=spec.get("criteria", ""),
    ))

    # ── 步骤 3：最后原子写入 claims.json（提交点） ──
    def do_claim(data):
        feats = data.setdefault("features", {})
        # 再次检查（flock 内），防止步骤 1~3 之间被其他 agent 抢先
        if feature_id in feats and feats[feature_id].get("phase") not in ("pending", None):
            existing = feats[feature_id]
            return {"ok": False, "error": f"race: already {existing.get('phase')} by {existing.get('agent')}"}
        # 再次检查依赖（flock 内），防止步骤 1~3 之间依赖被 doctor --fix 重置
        for dep in deps:
            dep_phase = feats.get(dep, {}).get("phase", "pending")
            if dep_phase != "done":
                return {"ok": False, "error": f"race: dependency Feature {dep} reverted to {dep_phase}"}
        feats[feature_id] = {
            "agent": agent,
            "phase": "analyzing",
            "branch": branch,
            "worktree": worktree,
            "claimed_at": datetime.now(timezone.utc).isoformat(),
            "analyzing": {"analysis": "pending", "plan": "pending"},
        }
        return {"ok": True}

    result = _atomic_json_update(claims_path, do_claim)
    if not result.get("ok"):
        # 提交失败（被抢先），回滚 worktree
        _remove_worktree(repo, worktree)
        return result

    # 更新 session_phase：首次 claim 时 reviewed → working；同时将 agent 加入 session_agents
    def update_session(data):
        if data.get("session_phase") == "reviewed":
            data["session_phase"] = "working"
        agents = data.setdefault("session_agents", [])
        if agent not in agents:
            agents.append(agent)
        return {"ok": True}
    _atomic_json_update(lock_path, update_session)

    return {"ok": True, "data": {
        "feature_md": str(feature_md),
        "branch": branch,
        "worktree": worktree,
    }}

# ── cm plan-ready ─────────────────────────────────────

def cmd_plan_ready(args) -> dict:
    """检查 PLAN.md 格式完整性，通过后推进 session: locked → reviewed。

    接受 session_phase 为 locked 或 reviewed（幂等）。
    前置条件：PLAN.md 存在且格式完整。

    检查项：
    1. PLAN.md 存在且非空
    2. 每个 feature 有 title、task、acceptance criteria
    3. depends_on 引用的 feature ID 都存在
    4. 依赖图无环
    """
    repo = _resolve_locked_repo(args)
    lock_path = repo / CM_DIR / "lock.json"
    plan_path = repo / CM_DIR / "PLAN.md"

    if not plan_path.exists() or not plan_path.read_text().strip():
        return {"ok": False, "error": "PLAN.md not found or empty"}

    plan = _parse_plan_md(plan_path)
    if not plan:
        return {"ok": False, "error": "PLAN.md contains no parseable features"}

    # 检查每个 feature 的完整性
    issues = []
    for fid, spec in plan.items():
        if not spec.get("task", "").strip():
            issues.append(f"Feature {fid}: missing Task section")
        if not spec.get("criteria", "").strip():
            issues.append(f"Feature {fid}: missing Acceptance Criteria")
        for dep in spec.get("depends_on", []):
            if dep not in plan:
                issues.append(f"Feature {fid}: depends on Feature {dep} which does not exist")

    # 检查依赖图无环
    sorted_ids = _topo_sort(plan)
    if len(sorted_ids) != len(plan):
        issues.append("Dependency graph has a cycle")

    if issues:
        return {"ok": False, "error": "PLAN.md validation failed", "data": {"issues": issues}}

    # 推进 session_phase: locked → reviewed
    def to_reviewed(data):
        phase = data.get("session_phase")
        if phase == "reviewed":
            return {"ok": True}  # 幂等
        if phase != "locked":
            return {"ok": False, "error": f"session is {phase}, expected locked"}
        data["session_phase"] = "reviewed"
        data["plan_reviewed_at"] = datetime.now(timezone.utc).isoformat()
        return {"ok": True}

    result = _atomic_json_update(lock_path, to_reviewed)
    if not result.get("ok"):
        return result
    return {"ok": True, "data": {"features": len(plan), "plan": list(plan.keys())}}

# ── cm dev ────────────────────────────────────────────

def cmd_dev(args) -> dict:
    """检查 Analysis+Plan 已写 → 标记 feature 从分析阶段进入开发阶段。

    前置条件：
    - phase == analyzing
    - features/XX.md 中包含非空的 ## Analysis 和 ## Plan 段落
    """
    repo = _resolve_locked_repo(args)
    claims_path = repo / CM_DIR / "claims.json"
    feature_id = str(args.feature)

    # 检查 features/XX.md 中 Analysis 和 Plan 段落
    feature_md = _find_feature_md(repo, feature_id)
    has_analysis, has_plan = _check_feature_md_sections(feature_md)

    def do_dev(data):
        features = data.setdefault("features", {})
        if feature_id not in features:
            return {"ok": False, "error": f"Feature {feature_id} not found"}
        feat = features[feature_id]
        if feat.get("phase") != "analyzing":
            return {"ok": False, "error": f"Feature {feature_id} is {feat.get('phase')}, expected analyzing"}

        # 更新 analyzing 子状态
        analyzing = feat.setdefault("analyzing", {})
        analyzing["analysis"] = "done" if has_analysis else "pending"
        analyzing["plan"] = "done" if has_plan else "pending"

        if not has_analysis:
            return {"ok": False, "error": f"Analysis section is empty in {feature_md}. Write analysis first"}
        if not has_plan:
            return {"ok": False, "error": f"Plan section is empty in {feature_md}. Write plan first"}

        analyzing["completed_at"] = datetime.now(timezone.utc).isoformat()
        feat["phase"] = "developing"
        feat["developing"] = {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "commit_count": 0,
            "latest_commit": None,
            "test_status": "pending",
            "test_commit": None,
            "test_passed_at": None,
            "test_output": None,
        }
        return {"ok": True}

    return _atomic_json_update(claims_path, do_dev)

# ── cm test ───────────────────────────────────────────

def cmd_test(args) -> dict:
    """在 feature worktree 中跑测试，将结果写入 claims.json developing 子状态。

    前置条件：phase == developing
    测试通过 → developing.test_status="passed", test_commit=HEAD
    测试失败 → developing.test_status="failed", test_output 记录失败摘要
    """
    repo = _resolve_locked_repo(args)
    claims_path = repo / CM_DIR / "claims.json"
    feature_id = str(args.feature)

    # 检查 tracked 文件是否有未提交修改（untracked 文件不阻塞测试）
    worktree = _get_feature_worktree(claims_path, feature_id)
    wt_path = Path(worktree) if worktree else repo
    git_status = subprocess.run(
        ["git", "status", "--porcelain", "-uno"], cwd=wt_path, capture_output=True, text=True
    )
    if git_status.stdout.strip():
        return {"ok": False, "error": "uncommitted changes to tracked files, commit before testing"}

    # 获取当前 HEAD + feature branch commit count
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=wt_path, capture_output=True, text=True
    ).stdout.strip()
    # 只计算 feature branch 上特有的 commit（相对于 dev branch 基线）
    lock = _atomic_json_read(repo / CM_DIR / "lock.json")
    dev_branch = lock.get("branch", "HEAD")
    commit_count = int(subprocess.run(
        ["git", "rev-list", "--count", f"{dev_branch}..HEAD"],
        cwd=wt_path, capture_output=True, text=True
    ).stdout.strip() or "0")

    # 跑测试
    test_result = _run_tests(wt_path)
    output_summary = (test_result.get("output", "") or "")[:500]  # 截断到 500 字符

    # 将测试结果写入 claims.json developing 子状态
    # 注意：updater 始终返回 ok:True 以确保写入（无论测试是否通过），
    # 测试失败信息通过 data.test_status 和 data.test_output 传递给调用者。
    def update_test_state(data):
        features = data.setdefault("features", {})
        if feature_id not in features:
            return {"ok": False, "error": f"Feature {feature_id} not found"}
        feat = features[feature_id]
        if feat.get("phase") != "developing":
            return {"ok": False, "error": f"Feature {feature_id} is {feat.get('phase')}, expected developing"}
        now = datetime.now(timezone.utc).isoformat()
        dev = feat.setdefault("developing", {})
        dev["commit_count"] = commit_count
        dev["latest_commit"] = head
        dev["test_commit"] = head
        dev["test_output"] = output_summary
        if test_result["ok"]:
            dev["test_status"] = "passed"
            dev["test_passed_at"] = now
        else:
            dev["test_status"] = "failed"
            dev["test_passed_at"] = None
        # 始终 ok:True 保证写入；测试结果通过 data 返回
        return {"ok": True, "data": {
            "test_passed": test_result["ok"],
            "test_status": dev["test_status"],
            "test_commit": head,
            "output": output_summary,
        }}

    return _atomic_json_update(claims_path, update_test_state)

# ── cm review ─────────────────────────────────────────

def cmd_review(args) -> dict:
    """调用独立 engine 审查 feature diff，写入 review evidence。

    前置条件：
    1. phase == developing
    2. test_status == passed && test_commit == git HEAD（同 cm done）

    review 通过 → review_status="approved", 返回 diff_url
    review 不通过 → review_status="changes_requested", 返回问题列表
    """
    repo = _resolve_locked_repo(args)
    claims_path = repo / CM_DIR / "claims.json"
    feature_id = str(args.feature)

    # 前置检查：test 必须通过且不 stale
    worktree = _get_feature_worktree(claims_path, feature_id)
    wt_path = Path(worktree) if worktree else repo
    current_head = _run_git(wt_path, ["rev-parse", "HEAD"]).stdout.strip()

    claims = _atomic_json_read(claims_path)
    feat = claims["features"].get(feature_id)
    if feat.get("phase") != "developing":
        return {"ok": False, "error": f"Feature {feature_id} is {feat.get('phase')}, expected developing"}
    dev = feat.get("developing", {})
    if dev.get("test_status") != "passed":
        return {"ok": False, "error": "tests not passed, run cm test first"}
    if dev.get("test_commit") != current_head:
        return {"ok": False, "error": "code changed after test, run cm test first"}

    # 构建 diff context
    lock = _atomic_json_read(repo / CM_DIR / "lock.json")
    base_ref = lock.get("branch", "HEAD~1")
    diff = _run_git(wt_path, ["diff", f"{base_ref}..HEAD"]).stdout

    # 选择 review engine（必须与开发 engine 不同）
    review_engine_name = getattr(args, "engine", None) or _get_review_engine()
    engine = get_engine(review_engine_name)

    # 构建 review prompt（要求末尾输出 VERDICT 标记行）
    prompt = f"""Review the following code changes. Focus on:
    - Correctness and edge cases
    - Error handling
    - Code quality and readability
    - Security concerns

    IMPORTANT: End your review with exactly one of these lines:
    VERDICT: APPROVED
    VERDICT: CHANGES_REQUESTED

    Diff:
    {diff[:20000]}
    """

    # 调用 engine（推送中间进度消息）
    _append_journal(repo, agent, "review-start", f"engine={review_engine_name}")
    result = engine.run(prompt=prompt, repo_path=repo, mode="review", timeout=300)
    _append_journal(repo, agent, "review-done", f"engine={review_engine_name}")

    # 解析 VERDICT 标记行
    output = result.summary or ""
    verdict = "changes_requested"  # 默认不通过（解析失败时保守处理）
    for line in reversed(output.splitlines()):
        line = line.strip()
        if line == "VERDICT: APPROVED":
            verdict = "approved"
            break
        elif line == "VERDICT: CHANGES_REQUESTED":
            verdict = "changes_requested"
            break

    # 写入 claims.json
    now = datetime.now(timezone.utc).isoformat()
    def update_review_state(data):
        feat = data["features"][feature_id]
        dev = feat.setdefault("developing", {})
        dev["review_status"] = verdict
        dev["review_commit"] = current_head
        dev["review_engine"] = review_engine_name
        dev["review_output"] = output[:2000]
        dev["review_at"] = now
        return {"ok": True, "data": {
            "review_status": verdict,
            "review_commit": current_head,
            "review_engine": review_engine_name,
            "review_output": output[:2000],
        }}

    result = _atomic_json_update(claims_path, update_review_state)

    if verdict == "approved":
        # 构建 diff_url 并返回
        diff_url = _build_diff_url(wt_path, base_ref, current_head)
        result.setdefault("data", {})["diff_url"] = diff_url
        result["data"]["next_action"] = _hint(
            f"cm done --feature {feature_id}", "Review approved, mark feature complete")
    else:
        result.setdefault("data", {})["next_action"] = _hint(
            f"cm review --feature {feature_id}",
            "Fix issues raised in review, then cm test + cm review")

    return result

# ── cm done ───────────────────────────────────────────

def cmd_done(args) -> dict:
    """检查 developing 子状态（test + review 双重 gate）→ 标记 feature 完成，返回被解锁的 feature 列表。

    前置条件：
    1. phase == "developing"
    2. developing.test_status == "passed" && test_commit == git HEAD
    3. developing.review_status == "approved" && review_commit == git HEAD
    不满足则拒绝。

    注意：test_commit / review_commit 与 latest_commit 的对比不能只看 claims.json 内部——
    必须在 flock 外先读 git HEAD，在 flock 内与 test_commit / review_commit 对比，
    才能检测到之后的新 commit。
    """
    repo = _resolve_locked_repo(args)
    claims_path = repo / CM_DIR / "claims.json"
    feature_id = str(args.feature)
    plan = _parse_plan_md(repo / CM_DIR / "PLAN.md")

    # 在 flock 外读取 worktree 的真实 HEAD（避免在 flock 内执行耗时的 git 操作）
    worktree = _get_feature_worktree(claims_path, feature_id)
    wt_path = Path(worktree) if worktree else repo
    current_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=wt_path, capture_output=True, text=True
    ).stdout.strip()

    def do_done(data):
        features = data.setdefault("features", {})
        if feature_id not in features:
            return {"ok": False, "error": f"Feature {feature_id} not found"}

        feat = features[feature_id]
        phase = feat.get("phase", "pending")

        if phase == "done":
            return {"ok": False, "error": f"Feature {feature_id} is already done"}
        if phase == "analyzing":
            return {"ok": False, "error": "still in analysis phase, run cm dev first"}
        if phase != "developing":
            return {"ok": False, "error": f"Feature {feature_id} is {phase}, expected developing"}

        dev = feat.get("developing", {})

        # Gate 1: test 必须通过且不 stale
        test_status = dev.get("test_status", "pending")
        if test_status == "pending":
            return {"ok": False, "error": "no test record, run cm test first"}
        if test_status == "failed":
            return {"ok": False, "error": f"last test failed: {dev.get('test_output', '')[:100]}. Fix and run cm test again"}
        if dev.get("test_commit") != current_head:
            return {"ok": False, "error": f"code changed after last test "
                    f"(tested {dev.get('test_commit', '?')[:7]}, HEAD {current_head[:7]}), run cm test again"}

        # Gate 2: review 必须通过且不 stale
        review_status = dev.get("review_status", "pending")
        if review_status == "pending":
            return {"ok": False, "error": "no review record, run cm review first"}
        if review_status == "changes_requested":
            return {"ok": False, "error": f"review requested changes: {dev.get('review_output', '')[:200]}. "
                    "Fix and run cm test + cm review again"}
        if dev.get("review_commit") != current_head:
            return {"ok": False, "error": f"code changed after review "
                    f"(reviewed {dev.get('review_commit', '?')[:7]}, HEAD {current_head[:7]}), "
                    "run cm test + cm review again"}

        feat["phase"] = "done"
        feat["completed_at"] = datetime.now(timezone.utc).isoformat()

        # 找出被解锁的 feature
        done_ids = {fid for fid, f in features.items() if f.get("phase") == "done"}
        unblocked = []
        for fid, spec in plan.items():
            if fid in features and features[fid].get("phase") != "pending":
                continue
            deps = spec.get("depends_on", [])
            if deps and all(d in done_ids for d in deps):
                unblocked.append({"id": fid, "title": spec["title"]})

        all_done = all(f.get("phase") == "done" for f in features.values())
        return {"ok": True, "data": {"unblocked": unblocked, "all_done": all_done}}

    result = _atomic_json_update(claims_path, do_done)
    # 注意：不自动推进 session_phase。all_done 时 session 仍然是 working，
    # agent 需要显式运行 cm integrate 进行集成验证后才能 submit。
    return result

# ── cm progress ───────────────────────────────────────

def cmd_progress(args) -> dict:
    """纯查询：读 lock.json + claims.json + PLAN.md，返回两级状态 + 自然语言指引。

    **不修改任何状态文件**。所有状态推进都通过显式的 cm 命令完成。
    输出格式参见 §3.4.4 的展示示例。
    """
    repo = _resolve_locked_repo(args)
    lock_path = repo / CM_DIR / "lock.json"
    lock = _atomic_json_read(lock_path)
    plan_path = repo / CM_DIR / "PLAN.md"
    plan = _parse_plan_md(plan_path)
    claims = _atomic_json_read(repo / CM_DIR / "claims.json")
    features_claims = claims.get("features", {})

    session_phase = lock.get("session_phase", "unknown")
    plan_exists = plan_path.exists() and bool(plan)

    # Session 级指引
    session_steps = _generate_session_steps(session_phase, plan_exists)

    result = []
    done_ids = {fid for fid, f in features_claims.items() if f.get("phase") == "done"}
    for fid, spec in plan.items():
        claim = features_claims.get(fid, {})
        phase = claim.get("phase", "pending")
        blocked_by = []

        # pending 时检查是否 blocked
        if phase == "pending":
            deps = spec.get("depends_on", [])
            blocked_by = [d for d in deps if d not in done_ids]
            if blocked_by:
                phase = "blocked"

        # 从子状态生成分步操作指引
        feature_md = _find_feature_md(repo, fid) if phase not in ("pending", "blocked") else None
        action_steps = _generate_action_steps(phase, claim, fid, feature_md, blocked_by if phase == "blocked" else [])

        result.append({
            "id": fid, "title": spec["title"],
            "phase": phase,
            "agent": claim.get("agent"),
            "worktree": claim.get("worktree"),
            "feature_md": str(feature_md) if feature_md else None,
            "sub_status": _format_sub_status(phase, claim),
            "action_steps": action_steps,
        })

    suggestions = _generate_suggestions(result, lock)

    return {"ok": True, "data": {
        "session_phase": session_phase,
        "session_steps": session_steps,
        "total": len(result),
        "done": sum(1 for r in result if r["phase"] == "done"),
        "analyzing": sum(1 for r in result if r["phase"] == "analyzing"),
        "developing": sum(1 for r in result if r["phase"] == "developing"),
        "pending": sum(1 for r in result if r["phase"] == "pending"),
        "blocked": sum(1 for r in result if r["phase"] == "blocked"),
        "features": result,
        "suggestions": suggestions,
    }}

def _generate_session_steps(session_phase: str, plan_exists: bool) -> list:
    """根据 session_phase 生成 session 级分步指引。纯函数，不修改任何状态。"""
    if session_phase == "locked":
        if plan_exists:
            return [
                "检查 PLAN.md 内容完整性（每个 feature 有 Task + Acceptance Criteria + Depends on）",
                "运行 _cm_start 验证 PLAN.md 并推进到 reviewed",
            ]
        return ["分析需求", "创建 .coding-master/PLAN.md（按 SKILL.md 模板）"]
    if session_phase == "reviewed":
        return ["运行 cm claim --feature N 认领可用 feature"]
    if session_phase == "working":
        return []  # feature 级指引接管
    if session_phase == "integrating":
        return ["集成验证已通过", "运行 cm submit --title '...' 提交"]
    return []

def _generate_action_steps(phase, claim, fid, feature_md, blocked_by) -> list:
    """根据 phase + 子状态生成分步操作指引（有序列表，每步可直接执行）。"""
    wt = claim.get("worktree", "")
    if phase == "blocked":
        return [f"等待依赖完成: {', '.join(f'Feature {d}' for d in blocked_by)}"]
    if phase == "pending":
        return [f"运行 cm claim --feature {fid}"]
    if phase == "analyzing":
        a = claim.get("analyzing", {})
        if a.get("analysis") != "done":
            return [f"cd {wt}", f"阅读 {feature_md} 中的 Spec", "在 Analysis 段落分析代码"]
        if a.get("plan") != "done":
            return [f"在 {feature_md} 中撰写 Plan", f"Plan 写完后运行 cm dev --feature {fid}"]
        return [f"运行 cm dev --feature {fid} 进入开发阶段"]
    if phase == "developing":
        dev = claim.get("developing", {})
        ts = dev.get("test_status", "pending")
        if ts == "pending":
            return [f"cd {wt}", f"阅读 {feature_md} 的 Plan 了解开发计划",
                    "编写代码", "git commit", f"运行 cm test --feature {fid}"]
        if ts == "failed":
            output = dev.get("test_output", "")[:200]
            return [f"cd {wt}", f"阅读 {feature_md} 的 Dev Log 了解上下文",
                    f"失败原因: {output}", "修复代码", "git commit",
                    f"运行 cm test --feature {fid}"]
        if ts == "passed":
            if dev.get("test_commit") != dev.get("latest_commit"):
                return [f"cd {wt}", "代码在测试后有变更", f"运行 cm test --feature {fid} 重新测试"]
            # test 通过后进入 review 循环
            rs = dev.get("review_status", "pending")
            if rs == "pending":
                return [f"运行 cm review --feature {fid} 进行代码审查"]
            if rs == "changes_requested":
                review_output = dev.get("review_output", "")[:200]
                return [f"cd {wt}", f"review 意见: {review_output}",
                        "修复代码", "git commit",
                        f"运行 cm test --feature {fid}",
                        f"运行 cm review --feature {fid}"]
            if rs == "approved":
                if dev.get("review_commit") != dev.get("latest_commit"):
                    return [f"cd {wt}", "代码在 review 后有变更",
                            f"运行 cm test --feature {fid}",
                            f"运行 cm review --feature {fid}"]
                return [f"阅读 {feature_md} 确认 Acceptance Criteria 全部满足",
                        f"运行 cm done --feature {fid}"]
    return ["✓ 已完成"]

def _generate_suggestions(features: list, lock: dict) -> list:
    """根据全局状态生成行动建议。"""
    suggestions = []
    session_phase = lock.get("session_phase", "unknown")
    all_done = all(f["phase"] == "done" for f in features)

    if session_phase == "integrating":
        suggestions.append("集成验证已通过，运行 cm submit 提交")
        return suggestions
    if all_done and session_phase == "working":
        suggestions.append("所有 feature 已完成，运行 cm integrate 进行集成验证")
        return suggestions

    for f in features:
        if f["phase"] == "pending":
            suggestions.append(f"Feature {f['id']} ({f['title']}) 可认领")
        elif f["phase"] == "developing":
            dev = f.get("sub_status", {})
            if isinstance(dev, dict) and dev.get("test_status") == "passed" and dev.get("test_commit") == dev.get("latest_commit"):
                suggestions.append(f"{f['agent']} 可以验收 Feature {f['id']}")
            elif f.get("agent"):
                suggestions.append(f"{f['agent']} 继续开发 Feature {f['id']}")
    return suggestions

# ── JOURNAL.md 追加（flock 保护） ─────────────────────

def _append_journal(repo: Path, agent: str, action: str, message: str = ""):
    """flock 保护的 append-only 写入，防止多 agent 同时追加时丢失数据。"""
    journal_path = repo / CM_DIR / "JOURNAL.md"
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
    entry = f"\n## {now} [{agent}] {action}\n{message}\n" if message else f"\n## {now} [{agent}] {action}\n"
    with open(journal_path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(entry)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)

# ── cm renew ─────────────────────────────────────────

def cmd_renew(args) -> dict:
    """续租当前 lock 的 lease。同一 session 内任一 agent 可续租。"""
    repo = _resolve_locked_repo(args)
    lock_path = repo / CM_DIR / "lock.json"

    def do_renew(data):
        if not data:
            return {"ok": False, "error": "no active lock"}
        # 校验 agent 属于当前 session（通过 session_agents 列表）
        agent = _resolve_agent(args)
        session_agents = data.get("session_agents", [data.get("locked_by")])
        if agent not in session_agents:
            return {"ok": False, "error": f"agent '{agent}' is not in this session. "
                    f"Session agents: {session_agents}"}
        now = datetime.now(timezone.utc)
        data["lease_expires_at"] = (now + timedelta(minutes=LEASE_MINUTES)).isoformat()
        data["renewed_by"] = agent
        return {"ok": True, "data": {"new_expires_at": data["lease_expires_at"]}}

    return _atomic_json_update(lock_path, do_renew)

# ── cm reopen ─────────────────────────────────────────

def cmd_reopen(args) -> dict:
    """集成失败后重新打开 feature，回退到 developing 阶段。

    前置条件：feature phase == done
    效果：phase → developing，test_status → pending（需重新测试）
    用途：cm integrate 失败后，agent 用此命令重新打开需要修复的 feature
    """
    repo = _resolve_locked_repo(args)
    claims_path = repo / CM_DIR / "claims.json"
    feature_id = str(args.feature)

    def do_reopen(data):
        features = data.setdefault("features", {})
        if feature_id not in features:
            return {"ok": False, "error": f"Feature {feature_id} not found"}
        feat = features[feature_id]
        if feat.get("phase") != "done":
            return {"ok": False, "error": f"Feature {feature_id} is {feat.get('phase')}, expected done"}
        feat["phase"] = "developing"
        feat.pop("completed_at", None)
        dev = feat.setdefault("developing", {})
        dev["test_status"] = "pending"
        dev["test_commit"] = None
        dev["test_passed_at"] = None
        dev["test_output"] = None
        dev["reopened_at"] = datetime.now(timezone.utc).isoformat()
        return {"ok": True}

    result = _atomic_json_update(claims_path, do_reopen)
    if not result.get("ok"):
        return result

    # session_phase 回退到 working（因为有 feature 不再是 done）
    lock_path = repo / CM_DIR / "lock.json"
    def back_to_working(data):
        if data.get("session_phase") in ("integrating",):
            data["session_phase"] = "working"
        return {"ok": True}
    _atomic_json_update(lock_path, back_to_working)

    worktree = _atomic_json_read(claims_path).get("features", {}).get(feature_id, {}).get("worktree")
    return {"ok": True, "data": {"worktree": worktree}}

# ── cm integrate ──────────────────────────────────────

def cmd_integrate(args) -> dict:
    """集成验证：merge 所有 feature branch 到 dev branch，跑全量测试。

    前置条件：所有 feature 都 done
    执行顺序：
    1. 检查所有 feature done
    2. 切到 dev branch，按依赖拓扑序 merge feature branches
    3. 跑全量测试
    4. 通过 → session_phase = integrating，记录测试结果
    5. 失败 → 返回失败详情，session_phase 不变，agent 用 cm reopen 修复后重试
    """
    repo = _resolve_locked_repo(args)
    plan = _parse_plan_md(repo / CM_DIR / "PLAN.md")
    claims = _atomic_json_read(repo / CM_DIR / "claims.json")

    # Step 1: 检查所有 feature done
    for fid in plan:
        phase = claims.get("features", {}).get(fid, {}).get("phase", "pending")
        if phase != "done":
            return {"ok": False, "error": f"Feature {fid} is {phase}, not done. "
                    "All features must be done before integration"}

    lock = _atomic_json_read(repo / CM_DIR / "lock.json")
    branch = lock.get("branch", "dev/unknown")

    # Step 2: 切到 dev branch，记录 merge 前的 HEAD 用于回滚
    _run_git(repo, ["checkout", branch])
    pre_merge_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()

    feature_branches = [
        claims["features"][fid]["branch"]
        for fid in _topo_sort(plan)
        if "branch" in claims["features"].get(fid, {})
    ]
    for fb in feature_branches:
        merge_rc = subprocess.run(
            ["git", "merge", fb, "--no-edit"], cwd=repo, capture_output=True, text=True
        )
        if merge_rc.returncode != 0:
            # merge 失败（冲突或其他原因），先 abort 未完成的 merge 再回滚到基线
            subprocess.run(["git", "merge", "--abort"], cwd=repo, capture_output=True)
            subprocess.run(["git", "reset", "--hard", pre_merge_sha], cwd=repo, capture_output=True)
            return {"ok": False, "error": f"merge failed when merging {fb} into {branch}: "
                    f"{merge_rc.stderr.strip()}. "
                    f"Run cm reopen --feature <n> for the conflicting feature, "
                    f"resolve in its worktree (rebase onto {branch}), "
                    f"cm test, cm done, then retry cm integrate."}

    # Step 3: 跑全量测试（在 dev branch 上，包含所有 feature 的合并代码）
    test_result = _run_tests(repo)
    output_summary = (test_result.get("output", "") or "")[:1000]

    if not test_result["ok"]:
        # 集成测试失败：回滚到 merge 前的 dev branch HEAD
        _run_git(repo, ["reset", "--hard", pre_merge_sha])
        return {"ok": False, "error": "integration tests failed",
                "data": {"output": output_summary,
                         "hint": "Run cm reopen --feature <n> for the feature that needs fixing, "
                                 "fix the code, cm test, cm done, then retry cm integrate"}}

    # Step 4: 集成测试通过 → session_phase = integrating
    def to_integrating(data):
        data["session_phase"] = "integrating"
        data["integration_passed_at"] = datetime.now(timezone.utc).isoformat()
        return {"ok": True}
    _atomic_json_update(repo / CM_DIR / "lock.json", to_integrating)

    return {"ok": True, "data": {"test_output": output_summary}}

# ── cm submit（幂等） ────────────────────────────────

def cmd_submit(args) -> dict:
    """幂等提交：push + PR + cleanup。需要 session_phase == integrating。

    前置条件：cm integrate 已成功（session_phase == integrating）
    执行顺序：
    1. 检查 session_phase == integrating
    2. git add + commit（跳过如果 working tree clean）
    3. git push（跳过如果远端已有且一致）
    4. gh pr create（跳过如果 PR 已存在）
    5. 清理 feature worktrees（失败仅警告，cm doctor 兜底）
    6. unlock（失败不阻塞返回，仅警告）
    """
    repo = _resolve_locked_repo(args)
    lock = _atomic_json_read(repo / CM_DIR / "lock.json")

    # Step 1: 检查 session_phase
    if lock.get("session_phase") != "integrating":
        return {"ok": False, "error": f"session is {lock.get('session_phase')}, "
                "run cm integrate first (merge + integration tests must pass)"}

    plan = _parse_plan_md(repo / CM_DIR / "PLAN.md")
    claims = _atomic_json_read(repo / CM_DIR / "claims.json")
    branch = lock.get("branch", "dev/unknown")

    # Step 2: commit（幂等 — working tree clean 则跳过）
    # 显式排除 .coding-master/，防止运行期状态泄露到 git
    _run_git(repo, ["add", "-A", "--", ":(exclude).coding-master"])
    status_out = _run_git(repo, ["status", "--porcelain"])
    if status_out.strip():
        _run_git(repo, ["commit", "-m", args.title])

    # Step 3: push（幂等 — 远端一致则 no-op）
    push_result = _run_git(repo, ["push", "-u", "origin", branch], check=False)

    # Step 4: PR（幂等 — 已存在则跳过）
    existing_pr = subprocess.run(
        ["gh", "pr", "view", branch, "--json", "url"],
        cwd=repo, capture_output=True, text=True
    )
    if existing_pr.returncode != 0:
        pr_body = _generate_pr_body(repo)
        subprocess.run(
            ["gh", "pr", "create", "--title", args.title, "--body", pr_body],
            cwd=repo, check=True, capture_output=True
        )

    # Step 5: 清理 feature worktrees（失败仅警告）
    for fid in plan:
        wt = claims.get("features", {}).get(fid, {}).get("worktree")
        if wt:
            try:
                _remove_worktree(repo, wt)
            except Exception:
                pass  # cm doctor 兜底清理

    # Step 6: session_phase → done + unlock（失败仅警告，不阻塞成功返回）
    try:
        def mark_done(data):
            data["session_phase"] = "done"
            return {"ok": True}
        _atomic_json_update(repo / CM_DIR / "lock.json", mark_done)
        cmd_unlock(args)
    except Exception as exc:
        return {"ok": True, "data": {"branch": branch},
                "warning": f"PR created but unlock failed: {exc}. Run cm doctor to fix."}

    return {"ok": True, "data": {"branch": branch}}

# ── cm doctor ────────────────────────────────────────

def cmd_doctor(args) -> dict:
    """诊断并修复状态不一致。

    检查项：
    1. lock.json 引用的 branch 是否存在
    2. claims.json 中每个 analyzing/developing feature 的 worktree 是否存在
    3. 存在残留 worktree 但 claims.json 中无记录（claim 崩溃残留）
    4. lease 是否过期
    5. PLAN.md 与 claims.json 的 feature ID 一致性
    """
    repo = _repo_path(args.repo)
    issues = []
    fixes = []

    # 1. lock 状态检查
    lock_path = repo / CM_DIR / "lock.json"
    if lock_path.exists():
        lock = json.loads(lock_path.read_text())
        if lock:
            if _is_expired(lock):
                issues.append(f"lock expired at {lock.get('lease_expires_at')}")
                fixes.append("run: cm unlock (or cm renew if work is still in progress)")
            branch = lock.get("branch", "")
            branch_exists = subprocess.run(
                ["git", "rev-parse", "--verify", branch],
                cwd=repo, capture_output=True
            ).returncode == 0
            if not branch_exists:
                issues.append(f"lock references branch '{branch}' which does not exist")
                fixes.append("run: cm unlock --force")

    # 2. claims.json worktree 存在性
    claims_path = repo / CM_DIR / "claims.json"
    if claims_path.exists():
        claims = json.loads(claims_path.read_text())
        for fid, feat in claims.get("features", {}).items():
            if feat.get("phase") in ("analyzing", "developing"):
                wt = feat.get("worktree", "")
                if wt and not Path(wt).exists():
                    issues.append(f"Feature {fid}: worktree '{wt}' does not exist")
                    fixes.append(f"run: cm doctor --fix (will reset Feature {fid} to pending)")

    # 3. 残留 worktree 检查
    expected_worktrees = set()
    if claims_path.exists():
        for feat in json.loads(claims_path.read_text()).get("features", {}).values():
            if feat.get("worktree"):
                expected_worktrees.add(feat["worktree"])
    for d in repo.parent.iterdir():
        if d.name.startswith(f"{repo.name}-feature-") and str(d) not in expected_worktrees:
            issues.append(f"orphaned worktree: {d}")
            fixes.append(f"run: cm doctor --fix (will remove {d})")

    # 4. PLAN.md 与 claims.json 一致性
    plan = _parse_plan_md(repo / CM_DIR / "PLAN.md")
    if claims_path.exists():
        claims = json.loads(claims_path.read_text())
        for fid in claims.get("features", {}):
            if fid not in plan:
                issues.append(f"claims.json references Feature {fid} not found in PLAN.md")

    # --fix 模式：自动修复可安全修复的问题
    if getattr(args, 'fix', False) and issues:
        _doctor_auto_fix(repo, issues)
        fixes = ["auto-fixed: " + f for f in fixes]

    return {
        "ok": len(issues) == 0,
        "data": {"issues": issues, "suggested_fixes": fixes}
    }

# ── cm unlock ─────────────────────────────────────────

def cmd_unlock(args) -> dict:
    """释放 workspace 锁。清空 lock.json，不清理 claims.json 和 worktree。

    worktree 清理由 cm submit 负责（正常流程）或 cm doctor --fix 兜底。
    unlock 只做最小操作——释放锁，让其他 session 可以获取。
    """
    repo = _repo_path(args.repo)
    lock_path = repo / CM_DIR / "lock.json"
    def clear_lock(data):
        data.clear()
        return {"ok": True}
    return _atomic_json_update(lock_path, clear_lock)

# ── cm status ─────────────────────────────────────────

def cmd_status(args) -> dict:
    """显示当前锁状态。只读操作。"""
    repo = _repo_path(args.repo)
    lock_path = repo / CM_DIR / "lock.json"
    lock = _atomic_json_read(lock_path)
    if not lock:
        return {"ok": True, "data": {"locked": False}}
    expired = _is_expired(lock)
    return {"ok": True, "data": {
        "locked": True, "expired": expired,
        "branch": lock.get("branch"),
        "locked_by": lock.get("locked_by"),
        "session_phase": lock.get("session_phase"),
        "lease_expires_at": lock.get("lease_expires_at"),
        "session_agents": lock.get("session_agents", []),
    }}

# ── 辅助函数 ─────────────────────────────────────────

def _topo_sort(plan: dict) -> list:
    """按依赖关系拓扑排序 feature id，保证先 merge 基础 feature。"""
    from collections import deque
    in_degree = {fid: 0 for fid in plan}
    adj = {fid: [] for fid in plan}
    for fid, spec in plan.items():
        for dep in spec.get("depends_on", []):
            if dep in plan:
                adj[dep].append(fid)
                in_degree[fid] += 1
    queue = deque(fid for fid, d in in_degree.items() if d == 0)
    result = []
    while queue:
        fid = queue.popleft()
        result.append(fid)
        for nxt in adj[fid]:
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                queue.append(nxt)
    return result

def _generate_pr_body(repo: Path) -> str:
    """从 JOURNAL.md 里程碑条目 + PLAN.md feature 列表生成 PR body。"""
    plan = _parse_plan_md(repo / CM_DIR / "PLAN.md")
    journal_path = repo / CM_DIR / "JOURNAL.md"

    lines = ["## Features\n"]
    for fid in _topo_sort(plan):
        lines.append(f"- **Feature {fid}**: {plan[fid]['title']}")
    lines.append("")

    if journal_path.exists():
        lines.append("## Timeline\n")
        for line in journal_path.read_text().splitlines():
            # 只提取里程碑条目：匹配 "## timestamp [agent] action" 格式中的关键 action
            if re.match(r'^## \d{4}-\d{2}-\d{2}T\d{2}:\d{2} \[.*?\] (done|submit|plan-ready)', line):
                lines.append(line)
        lines.append("")

    return "\n".join(lines)
```

### 7.3 PLAN.md 解析

PLAN.md 的解析是轻量的模式匹配，不需要完整的 markdown parser：

```python
def _parse_plan_md(path: Path) -> dict:
    """解析 PLAN.md，返回 {feature_id: {title, task, depends_on, criteria}}。"""
    if not path.exists():
        return {}
    text = path.read_text()
    features = {}
    # 按 "### Feature N:" 分割
    for match in re.finditer(
        r'### Feature (\d+): (.+?)(?=\n### Feature \d+:|\Z)',
        text, re.DOTALL
    ):
        fid = match.group(1)
        rest = match.group(2)
        title = rest.split('\n')[0].strip()
        # 提取 depends_on
        deps_match = re.search(r'\*\*Depends on\*\*: (.+)', rest)
        deps = []
        if deps_match and deps_match.group(1).strip() not in ('—', '无', 'none', 'None'):
            deps = re.findall(r'Feature (\d+)', deps_match.group(1))
        # 提取 task
        task_match = re.search(r'#### Task\n(.+?)(?=\n####|\Z)', rest, re.DOTALL)
        task = task_match.group(1).strip() if task_match else ""
        # 提取 criteria
        criteria_match = re.search(r'#### Acceptance Criteria\n(.+?)(?=\n####|\n---|\Z)', rest, re.DOTALL)
        criteria = criteria_match.group(1).strip() if criteria_match else ""
        features[fid] = {
            "title": title, "task": task,
            "depends_on": deps, "criteria": criteria,
        }
    return features
```

解析失败的 feature 跳过不崩溃。agent 写坏了 PLAN.md 的某个 section，不影响其他 feature 的解析。

### 7.4 文件操作工具（v4.5 新增，v4.6 分层调整）

文件操作工具是 agent 的基础代码作业能力，弥补 skillkit 宿主中无原生文件访问的缺口。

**v4.6 分层调整**：只读操作（`read`/`find`/`grep`）属于 L0 层，无 lock 时 fallback 到原始仓库根目录。这解决了 "agent 只想读代码却被迫建 session → 建 session 失败 → 死循环" 的问题。写操作（`edit`）仍要求活跃 session + developing feature（L3 层）。

所有文件操作工具自动感知 session worktree——有活跃 session 且有 worktree 时，默认在 worktree 中操作。

#### 工作目录解析

```python
def _resolve_working_dir(args) -> Path:
    """解析工作目录：优先 session worktree，fallback 到 repo 根目录。

    v4.6：只读操作在无 lock 时直接返回 repo 根目录，不 _fail。
    """
    repo = _repo_path(args.repo)  # 不再调 _resolve_locked_repo
    lock = _atomic_json_read(repo / CM_DIR / "lock.json")
    if not lock:
        return repo  # L0 fallback: 无 session 时直接用原始仓库
    session_wt = lock.get("session_worktree")
    # 如果有 feature 参数且有 feature worktree，优先用 feature worktree
    feature_id = getattr(args, "feature", None)
    if feature_id:
        claims = _atomic_json_read(repo / CM_DIR / "claims.json")
        feat = claims.get("features", {}).get(str(feature_id), {})
        if feat.get("worktree"):
            return Path(feat["worktree"])
    # 其次用 session worktree
    if session_wt and Path(session_wt).exists():
        return Path(session_wt)
    return repo
```

#### cm read — 读文件（L0 层，无门槛）

```python
def cmd_read(args) -> dict:
    """读取文件内容。支持行范围，默认全文（上限 2000 行）。

    L0 层工具：无 lock 时 fallback 到 repo 根目录。

    参数：
    - file: 文件路径（绝对路径或相对于 working dir）
    - start_line: 起始行号（1-based，可选）
    - end_line: 结束行号（inclusive，可选）
    """
    repo = _repo_path(args.repo)  # 不再要求 lock
    cwd = _resolve_working_dir(repo, args)  # 有 session 时用 worktree，无则 repo 根
    target = Path(args.file)
    if not target.is_absolute():
        target = cwd / target
    target = target.resolve()

    # 安全检查：禁止读取 repo 外的文件
    if not _is_within_repo(target, repo):
        return {"ok": False, "error": f"path {target} is outside repo"}

    if not target.exists():
        return {"ok": False, "error": f"file not found: {target}"}
    if target.is_dir():
        return {"ok": False, "error": f"{target} is a directory, not a file"}

    lines = target.read_text(errors="replace").splitlines(keepends=True)
    start = max(1, getattr(args, "start_line", 1) or 1)
    end = min(len(lines), getattr(args, "end_line", None) or len(lines))

    MAX_LINES = 2000
    if end - start + 1 > MAX_LINES:
        end = start + MAX_LINES - 1

    selected = lines[start - 1 : end]
    # 带行号输出，与 cat -n 格式对齐
    numbered = "".join(f"{start + i:6d}\t{line}" for i, line in enumerate(selected))

    return {"ok": True, "data": {
        "file": str(target),
        "start_line": start,
        "end_line": end,
        "total_lines": len(lines),
        "content": numbered,
    }}
```

**设计要点**：
- 带行号输出，方便 agent 引用具体行
- 上限 2000 行，防止返回值撑爆 agent context
- 安全检查：不允许读 repo 外的文件
- 自动感知 worktree

#### cm find — 按 glob 查找文件

```python
def cmd_find(args) -> dict:
    """按 glob 模式查找文件。返回匹配的文件路径列表。

    参数：
    - pattern: glob 模式（如 "**/*.py", "src/**/test_*.py"）
    - max_results: 最大返回数（默认 50）
    """
    cwd = _resolve_working_dir(args)
    pattern = args.pattern
    max_results = getattr(args, "max_results", 50) or 50

    matches = sorted(cwd.glob(pattern))
    # 过滤目录，只返回文件
    files = [str(m.relative_to(cwd)) for m in matches if m.is_file()]

    truncated = len(files) > max_results
    files = files[:max_results]

    return {"ok": True, "data": {
        "pattern": pattern,
        "cwd": str(cwd),
        "files": files,
        "count": len(files),
        "truncated": truncated,
    }}
```

**设计要点**：
- 返回相对路径，简洁且方便 `cm read` 直接使用
- 最大 50 个结果，避免全量文件列表撑爆 context
- 不递归隐藏目录（`.git` 等）——glob 天然不匹配 `.*`

#### cm grep — 搜索文件内容

```python
def cmd_grep(args) -> dict:
    """搜索文件内容，返回匹配行。底层调用 ripgrep 或 fallback 到 grep。

    参数：
    - pattern: 正则表达式
    - glob: 文件过滤 glob（可选，如 "*.py"）
    - context: 上下文行数（默认 2）
    - max_results: 最大匹配文件数（默认 20）
    """
    cwd = _resolve_working_dir(args)
    pattern = args.pattern
    file_glob = getattr(args, "glob", None)
    context = getattr(args, "context", 2) or 2
    max_results = getattr(args, "max_results", 20) or 20

    # 优先 rg，fallback grep
    rg = shutil.which("rg")
    cmd = [rg or "grep"]
    if rg:
        cmd += ["-n", f"-C{context}", "--max-count=5",
                f"--max-filesize=1M", "--no-heading"]
        if file_glob:
            cmd += ["--glob", file_glob]
        cmd += [pattern, str(cwd)]
    else:
        cmd += ["-rn", f"-C{context}", pattern, str(cwd)]
        if file_glob:
            cmd += ["--include", file_glob]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    output = result.stdout

    # 截断到合理大小
    MAX_OUTPUT = 10000  # 字符
    truncated = len(output) > MAX_OUTPUT
    if truncated:
        output = output[:MAX_OUTPUT] + "\n...(output truncated)..."

    return {"ok": True, "data": {
        "pattern": pattern,
        "cwd": str(cwd),
        "output": output,
        "truncated": truncated,
    }}
```

**设计要点**：
- 优先用 ripgrep（更快），fallback 到 grep
- 默认 2 行上下文 + 每文件最多 5 匹配，平衡信息量和 context 开销
- 文件大小限制 1M，跳过二进制和大文件

#### cm edit — 精确替换编辑

```python
def cmd_edit(args) -> dict:
    """精确替换编辑文件。受控 patch 模型：old_text 必须精确匹配且唯一。

    参数：
    - file: 文件路径
    - old_text: 要替换的原文（必须在文件中唯一匹配）
    - new_text: 替换后的文本
    仅在 deliver/debug 模式下可用。
    """
    repo = _resolve_locked_repo(args)
    mode = _get_session_mode(repo)
    if mode in READ_ONLY_MODES:
        return {"ok": False, "error": f"edit not allowed in {mode} mode (read-only)"}

    cwd = _resolve_working_dir(args)
    target = Path(args.file)
    if not target.is_absolute():
        target = cwd / target
    target = target.resolve()

    if not _is_within_repo(target, repo):
        return {"ok": False, "error": f"path {target} is outside repo"}
    if not target.exists():
        return {"ok": False, "error": f"file not found: {target}"}

    content = target.read_text()
    old_text = args.old_text
    new_text = args.new_text

    if old_text == new_text:
        return {"ok": False, "error": "old_text and new_text are identical"}

    count = content.count(old_text)
    if count == 0:
        return {"ok": False, "error": "old_text not found in file"}
    if count > 1:
        return {"ok": False, "error": f"old_text matches {count} locations; provide more context to make it unique"}

    new_content = content.replace(old_text, new_text, 1)
    target.write_text(new_content)

    return {"ok": True, "data": {
        "file": str(target),
        "replacements": 1,
    }}
```

**设计要点**：
- 与 Claude Code 的 Edit 工具接口一致：`old_text` + `new_text`
- `old_text` 必须唯一匹配，避免意外批量替换
- 只在 deliver/debug 模式可用，review/analyze 模式禁止
- 不支持创建新**源代码**文件——只编辑已有文件，减少 agent 随意创建文件的风险
- **例外**：CM 元数据文件（`.coding-master/*.md`，包括 PLAN.md、features/*.md）允许通过 `old_text=""` 创建，因为 skillkit agent 无法"直接写文件"，必须通过 `cm edit` 完成 PLAN.md 的 bootstrap

### 7.5 Engine 集成

Engine 层是 CM agent 的**深度分析加速器**——agent 定义 scope，委托 engine 子进程完成分析，只需消化结果、写 report。**v4.5 修正**：engine 从"唯一路径"降为"可选加速器"，失败时 agent 通过文件操作工具（read/grep/find）自行分析。

#### Engine 角色分离

开发和 review 必须使用不同 engine，避免"自己 review 自己"：

| 角色 | 默认 engine | 用途 |
|------|------------|------|
| 开发（deliver/debug） | `claude-code` | 编写代码、深度分析 |
| Review（`cm review`） | `gemini` | 独立代码审查 |

**选择优先级**：`cm review --engine <name>` 参数 > workspace config 中的 `review_engine` > 默认（第一个非开发 engine）。

#### Review Engine 输出约定

review engine 输出自由文本 review 意见，末尾必须包含一个标记行：

```
VERDICT: APPROVED
```
或
```
VERDICT: CHANGES_REQUESTED
```

工具层只解析这一行确定 `review_status`，其余文本作为 `review_output` 存入 evidence。这是最简单、容错最好的方案——不要求 engine 输出结构化 JSON，只 grep 一个标记行。

**解析规则**：
- 从输出末尾向前扫描，找到第一个 `VERDICT:` 行
- 如果找不到标记行，默认为 `changes_requested`（保守处理）
- 标记行之外的内容全部作为 review 正文保留

#### CodingEngine 抽象

```python
class CodingEngine(ABC):
    def name(self) -> str: ...
    def is_available(self) -> bool: ...
    def run(self, prompt, repo_path, *, mode, timeout, max_turns) -> EngineResult: ...

@dataclass
class EngineResult:
    ok: bool              # 引擎是否成功执行
    summary: str          # 引擎分析摘要
    files_analyzed: list  # 读取的文件列表
    files_changed: list   # 修改的文件（deliver 模式）
    findings: list[dict]  # 结构化发现 [{file, lines, severity, description}]
    error: str            # 错误信息
    engine: str           # 引擎名称
    turns_used: int       # 使用的轮次数
```

#### ClaudeCodeEngine 实现

调用 Claude Code CLI 作为子进程：

```python
subprocess.run([
    "claude", "-p", prompt,
    "--allowedTools", allowed_tools,   # 按 mode 限制
    "--output-format", "json",
    "--max-turns", str(max_turns),
], cwd=repo_path, timeout=timeout)
```

**Mode 专属工具权限**：

| Mode | allowedTools | 原因 |
|------|-------------|------|
| review | Read,Glob,Grep | 只读分析 |
| analyze | Read,Glob,Grep | 只读分析 |
| debug | Read,Glob,Grep,Bash | 可能需要运行命令复现 |
| deliver | Read,Edit,Write,Glob,Grep,Bash | 需要修改代码 |

**Mode 专属 prompt 模板**：每种模式有专属系统 prompt，引导引擎输出结构化 JSON findings。prompt 由三部分组成：
1. Mode 模板（review/analyze/debug 各一套，定义角色和输出格式）
2. Scope 描述（从 scope.json 转为人类可读文本）
3. Scope 上下文（diff 文本或文件列表，上限 20KB）

**安全限制**：
- 输出截断 50KB，防止 context 爆炸
- `shutil.which("claude")` 检测可用性
- 默认 timeout 600s，max_turns 30

#### 引擎选择策略

v1 仅实现 ClaudeCodeEngine。引擎通过 `get_engine(name)` 工厂函数获取，`_ENGINES` dict 注册所有可用引擎，便于未来扩展。

#### cmd_engine_run 流程

1. 读 `scope.json` 获取分析范围
2. 根据 mode 选择 prompt 模板，注入 scope 上下文
3. 调用 `Engine.run(prompt, repo_path)`
4. 保存结果到 `.coding-master/engine_result.json`
5. 返回 `EngineResult` 给 agent

#### Engine 降级策略（v4.5 新增）

Engine 失败时（API 限额、CLI 不可用、超时等），`cmd_engine_run` 不应让 agent 失明。返回值中附带降级提示，引导 agent 用文件操作工具自行分析：

```python
if not result.ok:
    data["fallback_hint"] = {
        "message": "Engine failed. Use cm read/grep/find to analyze manually.",
        "suggested_steps": [
            "cm find --pattern '**/*.py' to locate relevant files",
            "cm grep --pattern '<keyword>' to search for code patterns",
            "cm read --file <path> to read specific files",
        ],
        "scope_files": scope.get("files", []),  # 传回 scope 中的目标文件
    }
```

**设计原则**：
- Engine 失败 ≠ 任务失败。agent 有独立的代码阅读能力，只是效率降低。
- `fallback_hint` 传回 scope 中已定义的目标文件，agent 可直接 `cm read` 继续工作。
- 不做自动重试——重试浪费 API 配额且可能再次失败，不如让 agent 切换策略。

---

## 8. 完整 Walkthrough

用一个具体任务跑通四层架构，追踪每一步中每一层的状态变化。

**任务**：重构 inspector 模块，拆分 SessionScanner 和 ReportGenerator。两个 agent 并行开发。

### Step 1: Agent A 锁定 workspace

**Agent A 操作**：`cm lock --repo alfred`

**工具层**：`cmd_lock` 执行：
1. 读 `~/.alfred/coding-master.json` 找到 alfred 的 repo path
2. 检查 `.coding-master/lock.json` → 不存在，可以锁
3. 原子写入 `lock.json`
4. `git worktree add ../alfred-session -b dev/alfred-0308-1000`（在独立目录创建 session worktree，主 repo 不动）
5. 更新 `lock.json` 记录 `session_worktree` 路径
6. 确保 `.gitignore` 包含 `.coding-master/`

**各层状态**：

```
公约层    SKILL.md                        （不变）
计划层    （尚未创建 PLAN.md）
工具层    cmd_lock 执行完毕
数据层    lock.json                       ← 新建
```

```
.coding-master/
├── lock.json           ← {"repo":"alfred","branch":"dev/alfred-0308-1000",
│                           "session_worktree":"../alfred-session",
│                           "locked_by":"dolphin-a","lease_expires_at":"..."}
```

**返回给 Agent A**：`{"ok":true, "data":{"branch":"dev/alfred-0308-1000"}}`

### Step 2: Agent A 分析代码、创建 PLAN.md

**Agent A 操作**：
1. 读 `src/everbot/core/runtime/inspector.py`，分析代码结构
2. 判断需要拆分为 4 个 feature
3. 直接创建 `.coding-master/PLAN.md`（按 SKILL.md 里的模板）

**注意**：Claude Code agent 直接写文件；skillkit agent 通过 `cm edit`（`old_text=""`, `file=".coding-master/PLAN.md"`）创建。CM 元数据文件不受 developing phase guard 限制。

**各层状态**：

```
公约层    SKILL.md                        （不变）
计划层    PLAN.md                         ← 新建
工具层    （未调用）
数据层    lock.json                       （不变）
```

PLAN.md 内容：

```markdown
# Feature Plan

## Origin Task
重构 inspector 模块，拆分 SessionScanner 和 ReportGenerator

## Features

### Feature 1: 提取 SessionScanner 接口
**Depends on**: —

#### Task
将 inspector.py 中的 scan 逻辑提取为独立的 SessionScanner 类。

#### Acceptance Criteria
- [ ] SessionScanner 类存在且有 scan(messages) -> ScanResult 方法
- [ ] 原有测试全部通过
- [ ] 无新增 lint 警告

---

### Feature 2: 实现增量 scan
**Depends on**: Feature 1

#### Task
基于 SessionScanner 接口，实现增量 scan 逻辑。

#### Acceptance Criteria
- [ ] 只处理 watermark 之后的新消息
- [ ] 1000 条消息 scan < 100ms
- [ ] 测试覆盖率 > 90%

---

### Feature 3: 拆分 ReportGenerator
**Depends on**: Feature 1

#### Task
将报告生成逻辑拆分到独立的 ReportGenerator 类。

#### Acceptance Criteria
- [ ] ReportGenerator 类独立存在
- [ ] inspector.py 通过组合调用 Scanner + Generator

---

### Feature 4: 集成测试
**Depends on**: Feature 2, Feature 3

#### Task
补充 Scanner + Generator 联合工作的集成测试。

#### Acceptance Criteria
- [ ] 至少 3 个集成测试场景
- [ ] 覆盖空 session、正常 session、超大 session
```

### Step 2.5: Agent A 审核 PLAN.md

**Agent A 操作**：`_cm_start`（PLAN.md 已存在，自动触发 plan-ready）

**工具层**：`cmd_start` → `cmd_plan_ready` 执行：
1. 读 `.coding-master/PLAN.md`，`_parse_plan_md` 解析出 4 个 feature
2. 检查每个 feature：title ✓，task ✓，acceptance criteria ✓
3. 检查 depends_on 引用合法性：Feature 2 → Feature 1 ✓，Feature 3 → Feature 1 ✓，Feature 4 → Feature 2,3 ✓
4. 检查依赖图无环 ✓
5. 原子更新 lock.json：session_phase `locked` → `reviewed`

**各层状态**：

```
公约层    SKILL.md                        （不变）
计划层    PLAN.md                         （不变，已通过审核）
工具层    cmd_plan_ready 执行完毕
数据层    lock.json                       ← session_phase: reviewed
```

**返回给 Agent A**：`{"ok":true, "data":{"features":4, "plan":["1","2","3","4"]}}`

**设计意义**：这是 PLAN.md 的质量关卡。在此之前 agent 不能 claim 任何 feature。如果 PLAN.md 格式有问题（缺少 AC、依赖引用不存在、有环），工具会拒绝并报告具体问题。对于重要任务，人类可以在此阶段介入审核 PLAN.md 的拆分质量。

### Step 3: Agent A 认领 Feature 1

**Agent A 操作**：`cm claim --feature 1`

**工具层**：`cmd_claim` 执行（崩溃安全的写入顺序）：
1. 检查 session_phase == reviewed 或 working ✓
2. 读 `PLAN.md` → `_parse_plan_md` 解析出 4 个 feature
3. **预检查**（只读）：读 `claims.json` → Feature 1 未被认领 ✓，无依赖 ✓
3. **创建副作用**（可逆）：创建 Feature 1 的独立 branch + worktree + `features/01-scanner-interface.md`
4. **原子提交**（flock）：写入 `claims.json`，再次检查未被抢先 → 写入 `{"1": {"agent":"dolphin-a", "phase":"analyzing", ...}}`

如果在步骤 3 崩溃：claims.json 未更新，feature 仍是 pending，残留 worktree 由 `cm doctor` 清理。
如果在步骤 4 发现被抢先：回滚步骤 3 创建的 worktree。

**各层状态**：

```
公约层    SKILL.md                        （不变）
计划层    PLAN.md                         （不变，规格层不改）
          features/01-scanner-interface.md ← 新建（工作现场）
工具层    cmd_claim 执行完毕
数据层    lock.json                       （不变）
          claims.json                     ← 新建
```

```
.coding-master/
├── lock.json
├── PLAN.md
├── claims.json         ← {"features":{"1":{"agent":"dolphin-a",
│                           "phase":"analyzing","branch":"feat/1-scanner-interface",
│                           "worktree":"../alfred-feature-1","claimed_at":"..."}}}
└── features/
    └── 01-scanner-interface.md  ← 从模板生成，含 Spec + 空 Analysis/Plan/Log
```

### Step 4: Agent B 尝试认领 Feature 3（被阻塞）

**Agent B 操作**：`cm claim --feature 3`

**工具层**：`cmd_claim` 执行：
1. 读 `PLAN.md` → Feature 3 的 Depends on = Feature 1
2. 读 `claims.json` → Feature 1 phase = `analyzing`（不是 done）
3. 返回错误

**各层状态**：全部不变。

**返回给 Agent B**：`{"ok":false, "error":"blocked: Feature 1 is analyzing"}`

Agent B 运行 `cm progress` 查看全局状态和下一步建议。

### Step 5: Agent A 分析 + 开发 + 测试 Feature 1

**Agent A 操作**（analyzing 阶段）：
1. 读 `features/01-scanner-interface.md` 看 Spec 和 Acceptance Criteria
2. 分析代码结构，在 `features/01-scanner-interface.md` 中写 Analysis + Plan
3. `cm dev --feature 1` → claims.json: phase `analyzing` → `developing`

**Agent A 操作**（developing 阶段）：
4. 在 Feature 1 的 worktree 中编辑代码，提取 SessionScanner 类
5. git commit 代码改动
6. `cm test --feature 1` → 通过 → claims.json: developing.test_status = `passed`

**Agent A 操作**（如果测试失败）：
- `cm test` 失败 → claims.json developing.test_status = `failed`，phase 保持 `developing`
- 修代码 → commit → 再次 `cm test` → 循环直到通过

**Agent A 操作**（测试通过后）：
7. 确认 Acceptance Criteria 全部满足，在 `features/01-scanner-interface.md` 中打勾

**关键**：每个阶段的状态转换都通过 cm 命令写入 claims.json，`cm progress` 随时可查当前阶段和下一步指引。

**features/01-scanner-interface.md 最终状态**：

```markdown
# Feature 1: 提取 SessionScanner 接口

## Spec
将 inspector.py 中的 scan 逻辑提取为独立的 SessionScanner 类。

**Acceptance Criteria**:
- [x] SessionScanner 类存在且有 scan(messages) -> ScanResult 方法
- [x] 原有测试全部通过
- [x] 无新增 lint 警告

## Analysis
- scan 逻辑在 inspector.py:88-195，约 100 行
- 依赖 ScanState 和 InspectorConfig
- 提取后 inspector.py 通过 self.scanner.scan() 调用

## Plan
1. [x] 创建 session_scanner.py，定义 SessionScanner 类
2. [x] 将 scan 逻辑从 inspector.py 迁移过去
3. [x] inspector.py 改为调用 SessionScanner
4. [x] 跑测试确认不回归

## Test Results
12/12 passed (0 failed), 2.1s

## Dev Log
- 10:40 测试全过，3 个 criteria 全部满足
- 10:25 迁移完成，inspector.py 改为组合调用
- 10:10 创建 SessionScanner 类，定义 scan() 接口
- 10:00 开始开发
```

### Step 6: Agent A 完成 Feature 1

**Agent A 操作**：`cm done --feature 1`

**工具层**：`cmd_done` 执行：
1. 对 `claims.json` 执行 `_atomic_json_update`（flock 加锁）：
   - 检查 phase == `developing` ✓
   - 检查 test_status == `passed` ✓
   - 检查 test_commit == git HEAD（`a1b2c3d`）✓
   - Feature 1 phase `developing` → `done`
   - 写入 `completed_at`
3. 自动追加 JOURNAL.md：`## 2026-03-08T10:42 [dolphin-a] done feature-1`
4. 检查哪些 feature 被解锁：
   - Feature 2 depends on Feature 1 → Feature 1 已 done → **Feature 2 解锁**
   - Feature 3 depends on Feature 1 → Feature 1 已 done → **Feature 3 解锁**
   - Feature 4 depends on Feature 2, 3 → 还没 done → 仍然 blocked

**各层状态**：

```
公约层    SKILL.md                        （不变）
计划层    PLAN.md                         （不变）
          features/01-scanner-interface.md （Agent A 已更新完毕）
工具层    cmd_done 执行完毕
数据层    claims.json                     ← Feature 1 status → done
```

```json
// claims.json
{
  "features": {
    "1": {
      "agent": "dolphin-a",
      "phase": "done",
      "branch": "feat/1-scanner-interface",
      "worktree": "../alfred-feature-1",
      "claimed_at": "2026-03-08T10:00:00Z",
      "analyzing": {
        "analysis": "done",
        "plan": "done",
        "completed_at": "2026-03-08T10:15:00Z"
      },
      "developing": {
        "started_at": "2026-03-08T10:15:00Z",
        "commit_count": 3,
        "latest_commit": "a1b2c3d",
        "test_status": "passed",
        "test_commit": "a1b2c3d",
        "test_passed_at": "2026-03-08T10:41:00Z",
        "test_output": null
      },
      "completed_at": "2026-03-08T10:42:00Z"
    }
  }
}
```

**返回给 Agent A**：`{"ok":true, "data":{"unblocked":[{"id":"2","title":"实现增量 scan"},{"id":"3","title":"拆分 ReportGenerator"}]}}`

### Step 7: Agent A 认领 Feature 2，Agent B 认领 Feature 3（并行）

**Agent A 操作**：`cm claim --feature 2`
**Agent B 操作**：`cm claim --feature 3`（几乎同时）

**工具层**：两个 `cmd_claim` 几乎同时执行，但 `_atomic_json_update` 用 flock 串行化：

Agent A 的 `cmd_claim`：
1. 预检查：Feature 2 未被认领，depends on Feature 1 = done ✓
2. 创建 Feature 2 的 worktree（基点：Feature 1 的 branch，继承其代码改动）
3. 创建 `features/02-incremental-scan.md`
4. flock 加锁 → 再次确认未被抢先 → 写入 claims.json → 释放 flock

Agent B 的 `cmd_claim`（几乎同时）：
1. 预检查：Feature 3 未被认领，depends on Feature 1 = done ✓
2. 创建 Feature 3 的 worktree（基点：Feature 1 的 branch）
3. 创建 `features/03-report-generator.md`
4. flock 加锁 → 再次确认未被抢先 → 写入 claims.json → 释放 flock

flock 在步骤 4 串行化，保证不竞态。步骤 2-3 并行执行互不影响。

**各层状态**：

```
公约层    SKILL.md                           （不变）
计划层    PLAN.md                            （不变）
          features/01-scanner-interface.md    （完成）
          features/02-incremental-scan.md     ← Agent A 的新工作现场
          features/03-report-generator.md     ← Agent B 的新工作现场
工具层    两个 cmd_claim 都成功
数据层    claims.json                        ← Feature 2 + 3 都是 analyzing
```

```json
// claims.json
{
  "features": {
    "1": {"agent": "dolphin-a", "phase": "done", ...},
    "2": {"agent": "dolphin-a", "phase": "analyzing", "branch": "feat/2-incremental-scan", "worktree": "../alfred-feature-2", "claimed_at": "2026-03-08T10:43:00Z"},
    "3": {"agent": "dolphin-b", "phase": "analyzing", "branch": "feat/3-report-generator", "worktree": "../alfred-feature-3", "claimed_at": "2026-03-08T10:43:01Z"}
  }
}
```

**关键点**：flock 保证了即使两个 agent 同时 claim，也不会出现竞态——一个写完另一个才能读。

### Step 8: Agent A 和 Agent B 并行开发

**Agent A** 在 `features/02-incremental-scan.md` 里记录分析、计划、开发进展。
**Agent B** 在 `features/03-report-generator.md` 里记录分析、计划、开发进展。

两个 agent 编辑的是不同文件，且运行在不同 worktree 中，不会污染彼此的代码、git index 和测试产物。各自在自己的 worktree 中按需 `cm test --repo alfred` 跑测试。

（开发过程与 Step 5 类似，省略）

### Step 9: Agent B 先完成 Feature 3

**Agent B 操作**：`cm done --feature 3`

**工具层**：
1. claims.json: Feature 3 → done
2. 检查解锁：Feature 4 depends on Feature 2 + 3 → Feature 2 还是 developing → **Feature 4 仍然 blocked**

**返回给 Agent B**：`{"ok":true, "data":{"unblocked":[]}}`

Agent B 此时没有可认领的 feature，等待 Agent A 完成 Feature 2。

### Step 10: Agent A 完成 Feature 2

**Agent A 操作**：`cm done --feature 2`

**工具层**：
1. claims.json: Feature 2 → done
2. 检查解锁：Feature 4 depends on Feature 2 + 3 → 两者都 done → **Feature 4 解锁**

**返回给 Agent A**：`{"ok":true, "data":{"unblocked":[{"id":"4","title":"集成测试"}]}}`

```json
// claims.json
{
  "features": {
    "1": {"agent": "dolphin-a", "phase": "done", ...},
    "2": {"agent": "dolphin-a", "phase": "done", "completed_at": "2026-03-08T11:20:00Z"},
    "3": {"agent": "dolphin-b", "phase": "done", "completed_at": "2026-03-08T11:15:00Z"},
  }
}
```

### Step 11: Agent A 认领并完成 Feature 4

**Agent A 操作**：
1. `cm claim --feature 4` → 成功（status: `analyzing`）
2. 分析 + `cm dev --feature 4`（phase: `developing`）
3. 开发集成测试，commit
4. `cm test --feature 4` → 通过（developing.test_status: `passed`）
5. `cm done --feature 4`（phase: `done`）

### Step 12: 查看进度

**Agent A 操作**：`cm progress`

**工具层**：读 lock.json + claims.json + PLAN.md，展示两级状态 + 分步指引：

```json
{
  "ok": true,
  "data": {
    "session_phase": "working",
    "session_steps": [],
    "total": 4, "done": 4,
    "features": [
      {"id": "1", "phase": "done", "agent": "dolphin-a", "action_steps": ["✓ 已完成"]},
      {"id": "2", "phase": "done", "agent": "dolphin-a", "action_steps": ["✓ 已完成"]},
      {"id": "3", "phase": "done", "agent": "dolphin-b", "action_steps": ["✓ 已完成"]},
      {"id": "4", "phase": "done", "agent": "dolphin-a", "action_steps": ["✓ 已完成"]}
    ],
    "suggestions": ["所有 feature 已完成，运行 cm integrate 进行集成验证"]
  }
}
```

### Step 13: 集成验证

**Agent A 操作**：`cm integrate`

**工具层**：`cmd_integrate` 执行：
1. 检查所有 feature done ✓
2. 切到 dev branch，按依赖拓扑序 merge feature branches（1 → 2 → 3 → 4）
3. 在 dev branch 上跑全量测试 → 通过 ✓
4. 原子更新 lock.json：session_phase `working` → `integrating`

**返回给 Agent A**：`{"ok":true, "data":{"test_output":"24/24 passed, 0 failed"}}`

**如果集成测试失败**（假设 Feature 2 和 3 的代码合并后有冲突）：
1. `cm integrate` 返回失败 + 测试输出摘要
2. Agent A 运行 `cm reopen --feature 2`（phase: done → developing，test_status → pending）
3. Agent A 在 Feature 2 的 worktree 中修复问题，commit
4. `cm test --feature 2` → 通过
5. `cm done --feature 2`
6. 重新 `cm integrate`

### Step 14: 提交

**Agent A 操作**：`cm submit --repo alfred --title "refactor: split inspector into Scanner + Generator"`

**工具层**：`cmd_submit` 执行（幂等，崩溃后可安全重跑）：
1. 检查 session_phase == integrating ✓（merge 已在 cm integrate 中完成）
2. `git add -A :(exclude).coding-master` → `git commit`（working tree clean 则跳过）
3. `git push -u origin dev/alfred-0308-1000`（远端一致则 no-op）
4. `gh pr create --title "..." --body "..."`（PR 已存在则跳过）
5. 自动执行 `cmd_unlock`（失败仅警告，不阻塞成功返回）

**最终文件系统状态**：

```
.coding-master/
└── （空，或仅保留本地归档文件；不进入 git）
```

lock.json、claims.json、feature locks 和 worktrees 已清理；`.coding-master/` 不进入 git 历史。

### Walkthrough 验证清单

| 检查项 | 结果 |
|--------|------|
| Agent 只接触计划层（MD）和工具层？ | ✓ 从未直接读写 JSON |
| JSON 只被工具读写？ | ✓ lock.json 和 claims.json 都通过 cm 命令操作 |
| PLAN.md 创建后未被修改？ | ✓ 只在 Step 2 创建，之后只读 |
| PLAN.md 审核后才能 claim？ | ✓ Step 2.5 cm plan-ready 审核通过后才进入 reviewed |
| 每个 feature MD 只有一个 owner？ | ✓ 01/02/04 归 Agent A，03 归 Agent B |
| 并发认领正确串行化？ | ✓ Step 7 flock 保证了原子性 |
| 依赖阻塞正确？ | ✓ Step 4 Feature 3 被阻塞，Step 6 解锁 |
| 级联解锁正确？ | ✓ Feature 1 done → 解锁 2,3；Feature 2+3 done → 解锁 4 |
| cm done 检查测试状态？ | ✓ Step 6 cmd_done 检查 test_status=passed 且 test_commit=HEAD，不自己跑测试 |
| cm test 写入结构化状态？ | ✓ Step 5 cm test 将 test_status + test_commit 写入 claims.json |
| 集成验证在 submit 之前？ | ✓ Step 13 cm integrate merge + 全量测试通过后才能 submit |
| 集成失败可修复？ | ✓ cm reopen 重新打开 feature → 修复 → cm test → cm done → 重试 cm integrate |
| JOURNAL.md 有完整时间线？ | ✓ lock/plan-ready/claim/done/integrate/submit 关键事件均记录 |
| 工具调用次数合理？ | ✓ 全程：1 lock + 1 plan-ready + 4 claim + N test + 4 done + 1 integrate + 1 submit = ~13+N |
| claim 写入顺序正确？ | ✓ 先创建 worktree/MD，最后原子写入 claims.json |
| worktree 基点正确？ | ✓ Feature 2/3 从 Feature 1 的 branch 创建，继承代码改动 |
| integrate + submit 幂等？ | ✓ 每一步都检查是否已完成，崩溃后重跑安全 |
| integrate merge 按拓扑序？ | ✓ `_topo_sort` 保证先 merge 基础 feature，冲突时自动 abort |
| session worktree 隔离主 repo？ | ✓ `cm lock` 用 `git worktree add` 创建独立目录，主 repo 工作区不受影响 |
| dev branch 无直接 commit？ | ✓ 开发全在 feature worktree 中，dev branch 只做基线和汇总 |
| agent identity 可区分？ | ✓ 来源于 session id 或 hostname-pid fallback |
| `_atomic_json_update` 失败不写入？ | ✓ updater 返回 `ok:false` 时恢复快照 |

### 10.2 Review Mode Walkthrough

**任务**：Review alfred 项目最近 3 次提交的代码变更。

#### Step 1: Agent 锁定 workspace（只读）

**操作**：`cm lock --repo alfred --mode review`

**工具层**：`cmd_lock` 创建只读锁，不创建 session worktree，不创建 dev branch。

**返回**：`{"ok": true, "data": {"branch": "main", "read_only": true}}`

#### Step 2: Agent 定义 scope

**操作**：`cm scope --diff HEAD~3..HEAD --goal "review code quality and security"`

**工具层**：`cmd_scope` 写入 `scope.json`。

#### Step 3: Agent 委托 engine 分析

**操作**：`cm engine-run`

**工具层**：`cmd_engine_run` 执行：
1. 读 `scope.json` → diff range `HEAD~3..HEAD`
2. 构建 review 模式 prompt（含 diff 文本，上限 20KB）
3. 调用 `ClaudeCodeEngine.run(prompt, repo, mode="review")`
4. Claude Code CLI 子进程在 repo 中分析代码，返回结构化 findings
5. 保存结果到 `engine_result.json`

**返回**：`{"ok": true, "data": {"summary": "...", "findings": [...], "files_analyzed": [...]}}`

#### Step 4: Agent 写 report

Agent 消化 engine 结果，生成人类可读的 review 报告。

**操作**：`cm report --content '# Code Review Report\n\n## Summary\n...\n\n## Findings\n...'`

#### Step 5: Agent 解锁

**操作**：`cm unlock`

**总工具调用**：5 次（lock → scope → engine-run → report → unlock），对比之前 30-80 次手动 read/grep。

---

## 9. 崩溃恢复与鲁棒性

### 10.1 故障场景与恢复策略

| 崩溃点 | 后果 | 恢复方式 |
|--------|------|----------|
| `cm lock`：lock.json 写入后，session worktree 创建前被 kill | lock.json 残留，workspace 被锁 | lease 过期后自动释放；或 `cm doctor --fix` 清理 |
| `cm lock`：session worktree 创建失败 | 工具自动回滚 lock.json | 无需恢复 |
| `cm lock`：session worktree 创建后，lock.json 更新 session_worktree 前被 kill | 残留 session worktree | `cm doctor --fix` 检测并清理残留 session worktree |
| `cm claim`：worktree 创建后，claims.json 写入前崩溃 | 残留 worktree，但 feature 仍是 pending | `cm doctor --fix` 清理残留 worktree；重新 `cm claim` 即可 |
| `cm claim`：claims.json 写入时被抢先 | 工具自动回滚 worktree | 无需恢复，agent 选其他 feature |
| `cm test`：测试通过后，claims.json 更新前崩溃 | test_status 未更新（仍是 pending 或旧值） | 重新 `cm test` 即可（幂等，会重跑测试并写入状态） |
| `cm done`：claims.json 更新前崩溃 | feature phase 仍是 developing，test_status 不受影响 | 重新 `cm done` 即可（只检查状态，不跑测试） |
| `cm done`：claims.json 更新后，JOURNAL 追加失败 | JOURNAL 缺条目 | 功能不受影响，仅日志缺失 |
| `cm plan-ready`：检查通过后、lock.json 更新前崩溃 | session_phase 仍是 locked | 重新 `cm plan-ready`（幂等，重新检查 + 更新） |
| `cm integrate`：merge 成功后、测试前崩溃 | dev branch 已有 merge commit | 重新 `cm integrate`（会重新 merge + 测试；merge 为 no-op 因已合并） |
| `cm integrate`：测试通过后、lock.json 更新前崩溃 | session_phase 仍是 working | 重新 `cm integrate`（会重跑测试，幂等） |
| `cm integrate`：merge 冲突 | merge 被中止，dev branch 状态不变 | 工具自动 `merge --abort`，返回冲突详情；`cm reopen` 对应 feature → 修复 → `cm done` → 重试 |
| `cm integrate`：测试失败 | dev branch 上 merge 被回滚（reset） | 返回失败详情；`cm reopen` 对应 feature → 修复 → `cm done` → 重试 |
| `cm reopen`：claims.json 更新后、lock.json 更新前崩溃 | feature 已回到 developing，但 session_phase 可能仍是 integrating | `cm progress` 会检测到不一致；`cm doctor --fix` 可修复 |
| `cm submit`：commit 后、push 前崩溃 | 本地有 commit，远端没有 | 重新 `cm submit`（push 幂等） |
| `cm submit`：push 后、PR 前崩溃 | 远端有代码，无 PR | 重新 `cm submit`（检测到 PR 不存在则创建） |
| `cm submit`：PR 后、unlock 前崩溃 | PR 已创建，lock 残留 | `cm submit` 返回成功 + 警告；`cm doctor --fix` 清理 lock |

### 10.2 设计原则：崩溃安全

**写入顺序原则**：先做可逆的副作用，最后做原子提交。

```
可逆副作用（创建文件/worktree）
        ↓
    原子提交（flock + JSON 写入）  ← 提交点
        ↓
    非关键操作（JOURNAL 追加等）   ← 失败不影响正确性
```

- 提交点之前崩溃 → 状态未变，残留由 `cm doctor` 清理
- 提交点之后崩溃 → 状态已提交，非关键操作丢失可接受

**幂等原则**：所有工具在崩溃后重跑都是安全的。

- `cm done`：重跑只检查 developing 子状态（test_status == passed && test_commit == git HEAD），不跑测试；满足则标记 done
- `cm submit`：每一步检查是否已完成，跳过已完成的步骤

### 9.3 `cm doctor` 检查清单

| 检查项 | 自动修复 |
|--------|----------|
| lock.json 引用的 branch 不存在 | 清空 lock.json |
| lock.json 的 session_worktree 不存在 | 清空 lock.json |
| lock lease 已过期 | 提示 `cm renew` 或 `cm unlock` |
| claims.json 中 analyzing/developing feature 的 worktree 不存在 | 重置为 pending |
| 存在残留 session worktree 但 lock.json 中无记录 | 删除残留 session worktree |
| 存在残留 feature worktree 但 claims.json 中无记录 | 删除残留 feature worktree |
| 孤立的 dev/* 或 feat/* 分支（已 merge 或无 session） | 删除孤立分支 |
| PLAN.md 中的 feature ID 与 claims.json 不一致 | 报告不一致，不自动修复 |
| PLAN.md 解析失败（格式错误） | 报告解析错误位置 |

### 9.4 并发安全约束

**单机约束**：并发安全依赖 `flock`，仅保证同一台机器上的多 agent 原子性。跨机器协作（如通过 NFS 共享 repo）不在当前设计范围内。

**flock 保护范围**：

| 资源 | 保护方式 |
|------|----------|
| lock.json | `_atomic_json_update`（flock） |
| claims.json | `_atomic_json_update`（flock） |
| JOURNAL.md | `_append_journal`（flock + O_APPEND） |
| PLAN.md | 单次写入，之后只读，无需保护 |
| features/XX.md | 单 owner，无需保护 |
| session worktree | session 独立目录（`../repo-session`），主 repo 不动 |
| feature worktree | 每个 feature 独立目录（`../repo-feature-N`），无冲突 |

---

## 10. 对比总结

### 10.1 文件数量与形态

| | 现在 | MD 驱动 |
|---|------|---------|
| **Python 代码** | ~4800 行（8 模块 + engine） | ~400 行（1 文件） |
| **状态文件** | feature_plan.json + criteria.json × N + verification.json × N + lock JSON + session.json | lock.json + claims.json + PLAN.md + features/*.md |
| **结构化** | 全 JSON | 只有锁和认领是 JSON |
| **Agent 可读** | 需要工具转换 | 直接读 MD |
| **并发安全** | 无（依赖单 agent） | flock 原子操作 |

### 10.2 MD vs JSON 边界判定

| 判定维度 | → JSON | → MD |
|----------|--------|------|
| 需要原子读写？ | 是（锁、认领） | 否 |
| 多方并发写？ | 是（多 agent 竞争认领） | 否（单 owner） |
| 主要消费者是程序？ | 是（状态检查、依赖判断） | 否（agent 读写） |
| 需要精确枚举状态？ | 是（pending/analyzing/developing/done） | 否（自由文本） |

用一句话说：**竞争写的用 JSON，独占写的用 MD。**

---

## 11. 测试方案

### 11.1 测试分层

```
┌────────────────────────────────────────────────┐
│  E2E 测试（完整 walkthrough）                      │
│  验证：lock → plan → claim → dev → done → submit │
├────────────────────────────────────────────────┤
│  崩溃恢复测试                                     │
│  验证：每个崩溃点 + cm doctor 修复                  │
├────────────────────────────────────────────────┤
│  并发测试                                         │
│  验证：多进程竞争 claim、JOURNAL 并发追加            │
├────────────────────────────────────────────────┤
│  幂等测试                                         │
│  验证：每个工具连续执行两次，第二次为 no-op 或安全     │
├────────────────────────────────────────────────┤
│  单元测试                                         │
│  验证：原子 JSON 操作、PLAN.md 解析、slugify 等      │
└────────────────────────────────────────────────┘
```

### 11.2 单元测试

#### 10.2.1 `_atomic_json_update`

| 用例 | 输入 | 预期 |
|------|------|------|
| 文件不存在时创建 | 空路径 + updater | 文件被创建，内容为 updater 写入的 JSON |
| 空文件时初始化 | 空文件 + updater | 初始化为 `{}` 后执行 updater |
| 正常读写 | 已有 JSON + updater | updater 收到已有数据，写回修改后的数据 |
| updater 抛异常 | updater raises | 文件内容不变（flock 释放，不写入） |
| updater 返回 ok:false | updater 修改了 data 但返回失败 | 文件内容恢复 deepcopy 快照，不写入修改 |
| updater 返回 ok:false 且未修改 data | updater 只读 data 后返回失败 | 文件内容不变（快照对比相等，无多余写操作） |
| JSON 格式损坏 | 文件内容为 `{broken` | 降级为 `{}`，不崩溃 |

#### 10.2.2 `_parse_plan_md`

| 用例 | 输入 | 预期 |
|------|------|------|
| 标准格式 | 4 个 feature 的 PLAN.md | 解析出 4 个 feature，各字段正确 |
| 文件不存在 | 不存在的路径 | 返回 `{}` |
| 空文件 | 空内容 | 返回 `{}` |
| 单 feature 无依赖 | `### Feature 1: xxx\n**Depends on**: —` | `{"1": {title, depends_on: []}}` |
| 多依赖 | `**Depends on**: Feature 2, Feature 3` | `depends_on: ["2", "3"]` |
| 标题含特殊字符 | `### Feature 1: 重构 inspector (v2)` | 正确提取标题，不崩溃 |
| 部分格式错误 | Feature 2 缺少 `#### Task` | Feature 2 的 task 为空，Feature 1/3 正常解析 |
| 中间 feature 格式损坏 | Feature 2 完全乱码 | Feature 1 和 3 正常解析，Feature 2 跳过 |

#### 10.2.3 `_slugify`

| 用例 | 输入 | 预期 |
|------|------|------|
| 英文标题 | `"Scanner Interface"` | `"scanner-interface"` |
| 中文标题 | `"提取扫描接口"` | 非空字符串（拼音或 hash fallback） |
| 空字符串 | `""` | fallback 到 `"feature-{id}"` |
| 特殊字符 | `"fix: bug #123"` | `"fix-bug-123"` 或类似 |
| 超长标题 | 100 字符 | 截断到 30 字符 |

#### 10.2.4 `_append_journal`

| 用例 | 输入 | 预期 |
|------|------|------|
| 文件不存在时创建 | 空路径 | 文件被创建，包含一条 entry |
| 追加到已有内容 | 已有 3 条 entry | 新 entry 追加到末尾，旧内容不变 |
| 空 message | action="claim", message="" | 只有 `## timestamp [agent] claim\n`，无多余空行 |

#### 10.2.5 `_check_lease`

| 用例 | 输入 | 预期 |
|------|------|------|
| lease 未过期 | expires_at = now + 1h | `{"ok": True}` |
| lease 已过期 | expires_at = now - 1m | `{"ok": False, "error": "lease expired..."}` |
| lock.json 不存在 | 无文件 | `{"ok": False, "error": "no active lock"}` |

#### 10.2.6 `_topo_sort`

| 用例 | 输入 | 预期 |
|------|------|------|
| 无依赖 | `{1: {deps:[]}, 2: {deps:[]}}` | `["1", "2"]`（任意顺序） |
| 线性依赖 | `1→2→3` | `["1", "2", "3"]` |
| 菱形依赖 | `1→{2,3}→4` | 1 在 2,3 前，2,3 在 4 前 |
| 单 feature | `{1: {deps:[]}}` | `["1"]` |

#### 10.2.7 `cmd_test` 测试状态写入

| 用例 | 输入 | 预期 |
|------|------|------|
| 测试通过 | worktree clean, tests pass | claims.json: test_status="passed", test_commit=HEAD |
| 测试失败 | worktree clean, tests fail | claims.json: test_status="failed", test_commit=HEAD, test_passed_at 被清除 |
| worktree 不 clean | 有未 commit 的改动 | 拒绝，返回 "commit changes before testing" |
| feature 不是 developing | phase=done | 拒绝 |
| 连续两次测试 | 第一次 fail，改代码 commit，第二次 pass | test_status 从 failed → passed，test_commit 更新为新 HEAD |

#### 10.2.8 `cmd_done` 测试状态检查

| 用例 | 输入 | 预期 |
|------|------|------|
| 测试通过且 commit 匹配 | test_status=passed, test_commit=HEAD | 成功标记 done |
| 未测试 | test_status=pending | 拒绝，"run cm test first" |
| 测试失败 | test_status=failed | 拒绝，"last test failed" |
| 测试通过但代码已变更 | test_status=passed, test_commit ≠ HEAD | 拒绝，"code changed after last test" |
| 不是 developing | phase=done 或 pending | 拒绝 |

#### 10.2.9 `_resolve_agent`

| 用例 | 输入 | 预期 |
|------|------|------|
| 有 --agent 参数 | `args.agent = "dolphin-a"` | `"dolphin-a"` |
| 无 --agent 参数 | `args.agent = None` | `"{hostname}-{pid}"` 格式 |

#### 10.2.10 `cmd_plan_ready`

| 用例 | 输入 | 预期 |
|------|------|------|
| 正常 PLAN.md | 4 个完整 feature | session_phase → reviewed |
| PLAN.md 不存在 | 无文件 | 拒绝，"PLAN.md not found" |
| Feature 缺 Task | Feature 2 无 Task section | 拒绝，报告具体问题 |
| Feature 缺 AC | Feature 1 无 Acceptance Criteria | 拒绝，报告具体问题 |
| 依赖引用不存在 | depends on Feature 99 | 拒绝，"Feature 99 does not exist" |
| 依赖图有环 | 1→2→3→1 | 拒绝，"cycle" |
| session 不是 locked | session_phase=working | 拒绝 |
| 幂等：已经 reviewed | session_phase=reviewed | 成功（no-op） |

#### 10.2.11 `cmd_integrate`

| 用例 | 输入 | 预期 |
|------|------|------|
| 全部 done + 测试通过 | 4 features all done | session_phase → integrating |
| 有 feature 未完成 | Feature 2 is developing | 拒绝，"Feature 2 is developing" |
| merge 冲突 | Feature 2 和 3 有冲突 | 拒绝，merge --abort，返回冲突详情 |
| 集成测试失败 | merge 成功但测试 fail | 拒绝，回滚 merge，返回测试输出 |
| 幂等：已经 integrating | session_phase=integrating | 成功（重新 merge+测试） |

#### 10.2.12 `cmd_reopen`

| 用例 | 输入 | 预期 |
|------|------|------|
| 正常 reopen | feature phase=done | phase → developing, test_status → pending |
| feature 不是 done | phase=developing | 拒绝 |
| feature 不存在 | Feature 99 | 拒绝 |
| session_phase 回退 | session=integrating | session → working |

### 11.3 并发测试

#### 10.3.1 多进程竞争 claim

```python
def test_concurrent_claim_same_feature():
    """10 个进程同时 cm claim --feature 1，只有 1 个成功。"""
    # Setup: 创建 PLAN.md（1 个 feature），初始化空 claims.json
    results = parallel_run(10, ["cm", "claim", "--feature", "1", "--agent", f"agent-{i}"])
    success = [r for r in results if r["ok"]]
    failed = [r for r in results if not r["ok"]]
    assert len(success) == 1
    assert len(failed) == 9
    # 验证 claims.json 最终状态一致
    claims = read_json("claims.json")
    assert claims["features"]["1"]["phase"] == "analyzing"
    assert claims["features"]["1"]["agent"] == success[0]["agent"]
```

#### 10.3.2 多进程并发认领不同 feature

```python
def test_concurrent_claim_different_features():
    """3 个进程分别 claim Feature 1/2/3（无依赖），全部成功。"""
    # Setup: PLAN.md 有 3 个无依赖 feature
    results = parallel_run(3, [
        ["cm", "claim", "--feature", "1"],
        ["cm", "claim", "--feature", "2"],
        ["cm", "claim", "--feature", "3"],
    ])
    assert all(r["ok"] for r in results)
    claims = read_json("claims.json")
    assert len(claims["features"]) == 3
    assert all(f["phase"] == "analyzing" for f in claims["features"].values())
```

#### 10.3.3 JOURNAL.md 并发追加

```python
def test_concurrent_journal_append():
    """10 个进程同时追加 JOURNAL.md，所有条目都不丢失。"""
    parallel_run(10, lambda i: _append_journal(repo, f"agent-{i}", "test", f"entry {i}"))
    content = read_file("JOURNAL.md")
    for i in range(10):
        assert f"entry {i}" in content
```

### 11.4 崩溃恢复测试

每个测试模拟在特定步骤崩溃，验证系统状态和 `cm doctor` 的修复能力。

#### 10.4.1 claim 崩溃：worktree 已创建，claims.json 未写入

```python
def test_claim_crash_after_worktree():
    """模拟 claim 在创建 worktree 后、写入 claims.json 前崩溃。"""
    # Setup
    setup_locked_repo_with_plan()

    # 模拟崩溃：手动创建 worktree 但不更新 claims.json
    create_worktree("../repo-feature-1", "feat/1-scanner-interface")
    write_file("features/01-scanner-interface.md", "...")

    # 验证状态：claims.json 无记录，feature 仍可认领
    result = run("cm claim --feature 1")
    assert result["ok"]  # 可以正常认领（claims.json 里没有记录）

    # 或者用 doctor 清理残留后认领
    doctor = run("cm doctor --repo test")
    assert "orphaned worktree" in str(doctor["data"]["issues"])
    run("cm doctor --repo test --fix")
    # 残留 worktree 已清理
```

#### 10.4.2 claim 崩溃：claims.json 写入时被抢先

```python
def test_claim_race_loses():
    """Agent A 预检查通过，但 Agent B 先完成写入，Agent A 发现被抢先后回滚。"""
    setup_locked_repo_with_plan()

    # Agent B 先成功 claim
    run("cm claim --feature 1 --agent agent-b")

    # Agent A 在步骤 3（worktree 已创建）后尝试写入 claims.json
    # 模拟：手动创建 worktree，然后调用 claim（会在 flock 内发现已被认领）
    result = run("cm claim --feature 1 --agent agent-a")
    assert not result["ok"]
    assert "race" in result["error"] or "already claimed" in result["error"]
    # Agent A 的 worktree 应被回滚
    assert not Path("../repo-feature-1-agent-a").exists()
```

#### 10.4.3 done 崩溃：测试通过后 claims.json 未更新

```python
def test_done_crash_after_tests():
    """模拟 done 在测试通过后、更新 claims.json 前崩溃。重新 done 应成功。"""
    setup_claimed_feature(feature_id="1")

    # 第一次 done：成功（正常流程）
    # 模拟崩溃：claims.json 仍是 developing
    # 这等价于直接重新调用 done
    result = run("cm done --feature 1")
    assert result["ok"]

    # 再次 done：feature 已经是 done，应报错但不破坏状态
    result2 = run("cm done --feature 1")
    assert not result2["ok"]
    assert "already done" in result2["error"]
```

#### 10.4.4 submit 崩溃：push 后 PR 前

```python
def test_submit_crash_after_push():
    """模拟 submit 在 push 后、PR 创建前崩溃。重新 submit 应创建 PR。"""
    setup_all_features_done()

    # 模拟：手动 push，但不创建 PR
    run_git(["push", "-u", "origin", branch])

    # 重新 submit：应检测到已 push，跳过 commit/push，创建 PR
    result = run("cm submit --title 'test'")
    assert result["ok"]
    # PR 应存在
    pr = run_gh(["pr", "view", branch])
    assert pr.returncode == 0
```

#### 10.4.5 submit 崩溃：PR 后 unlock 前

```python
def test_submit_crash_after_pr():
    """模拟 submit 在 PR 创建后、unlock 前崩溃。"""
    setup_all_features_done()

    # 模拟：手动 push + 创建 PR，但不 unlock
    run_git(["push", "-u", "origin", branch])
    run_gh(["pr", "create", "--title", "test", "--body", "test"])

    # 重新 submit：应检测到 PR 已存在，跳过 push/PR，执行 unlock
    result = run("cm submit --title 'test'")
    assert result["ok"]
    # lock 应已释放
    assert not Path(".coding-master/lock.json").exists() or read_json("lock.json") == {}
```

### 11.5 幂等测试

每个工具连续执行两次，验证第二次行为正确。

```python
class TestIdempotency:
    def test_lock_twice(self):
        """第二次 lock 应返回 already locked。"""
        r1 = run("cm lock --repo test")
        assert r1["ok"]
        r2 = run("cm lock --repo test")
        assert not r2["ok"]
        assert "already locked" in r2["error"]

    def test_claim_twice(self):
        """第二次 claim 同一 feature 应返回 already claimed。"""
        run("cm claim --feature 1")
        r2 = run("cm claim --feature 1")
        assert not r2["ok"]
        assert "already claimed" in r2["error"]

    def test_test_twice(self):
        """第二次 cm test 覆盖第一次的结果（幂等）。"""
        run("cm test --feature 1")
        r2 = run("cm test --feature 1")
        assert r2["ok"]
        # test_commit 应为当前 HEAD（未变）

    def test_done_twice(self):
        """第二次 done 应返回 already done。"""
        run("cm test --feature 1")  # 先测试
        run("cm done --feature 1")
        r2 = run("cm done --feature 1")
        assert not r2["ok"]
        assert "already done" in r2["error"]

    def test_done_without_test(self):
        """未测试直接 done 应拒绝。"""
        run("cm claim --feature 1")
        do_dev(feature=1)
        r = run("cm done --feature 1")
        assert not r["ok"]
        assert "cm test" in r["error"]

    def test_plan_ready_twice(self):
        """第二次 plan-ready 应为 no-op（幂等）。"""
        run("cm plan-ready")
        r2 = run("cm plan-ready")
        assert r2["ok"]  # 已经 reviewed，幂等返回成功

    def test_claim_before_plan_ready(self):
        """plan-ready 前 claim 应被拒绝。"""
        # session_phase = locked
        r = run("cm claim --feature 1")
        assert not r["ok"]
        assert "plan-ready" in r["error"]

    def test_integrate_twice(self):
        """第二次 integrate 应重新跑测试（幂等）。"""
        run("cm integrate")
        r2 = run("cm integrate")
        assert r2["ok"]  # 重新 merge（no-op）+ 测试

    def test_submit_before_integrate(self):
        """integrate 前 submit 应被拒绝。"""
        r = run("cm submit --title 'test'")
        assert not r["ok"]
        assert "integrate" in r["error"]

    def test_submit_twice(self):
        """第二次 submit 应为 no-op（PR 已存在，commit 无变化）。"""
        run("cm submit --title 'test'")
        r2 = run("cm submit --title 'test'")
        assert r2["ok"]  # 成功但所有步骤都跳过

    def test_reopen_not_done(self):
        """reopen 非 done 的 feature 应拒绝。"""
        r = run("cm reopen --feature 1")  # feature 1 is developing
        assert not r["ok"]
        assert "expected done" in r["error"]

    def test_renew_after_unlock(self):
        """unlock 后 renew 应返回 no active lock。"""
        run("cm unlock")
        r = run("cm renew")
        assert not r["ok"]
        assert "no active lock" in r["error"]

    def test_doctor_on_clean_state(self):
        """干净状态下 doctor 应返回无问题。"""
        r = run("cm doctor --repo test")
        assert r["ok"]
        assert len(r["data"]["issues"]) == 0
```

### 11.6 cm doctor 测试

验证 doctor 能检测并修复每种不一致状态。

```python
class TestDoctor:
    def test_detect_expired_lease(self):
        """检测 lease 过期。"""
        write_json("lock.json", {"lease_expires_at": "2020-01-01T00:00:00Z", ...})
        r = run("cm doctor --repo test")
        assert any("expired" in i for i in r["data"]["issues"])

    def test_detect_orphaned_worktree(self):
        """检测残留 worktree。"""
        os.makedirs("../repo-feature-99")
        r = run("cm doctor --repo test")
        assert any("orphaned" in i for i in r["data"]["issues"])

    def test_fix_orphaned_worktree(self):
        """--fix 自动删除残留 worktree。"""
        os.makedirs("../repo-feature-99")
        run("cm doctor --repo test --fix")
        assert not Path("../repo-feature-99").exists()

    def test_detect_missing_worktree(self):
        """检测 claims.json 引用的 worktree 不存在。"""
        write_json("claims.json", {"features": {"1": {
            "phase": "developing", "worktree": "../nonexistent"
        }}})
        r = run("cm doctor --repo test")
        assert any("does not exist" in i for i in r["data"]["issues"])

    def test_fix_missing_worktree(self):
        """--fix 将丢失 worktree 的 feature 重置为 pending。"""
        write_json("claims.json", {"features": {"1": {
            "phase": "developing", "worktree": "../nonexistent"
        }}})
        run("cm doctor --repo test --fix")
        claims = read_json("claims.json")
        assert claims["features"]["1"]["phase"] == "pending"

    def test_detect_branch_missing(self):
        """检测 lock.json 引用的 branch 不存在。"""
        write_json("lock.json", {"branch": "nonexistent-branch", ...})
        r = run("cm doctor --repo test")
        assert any("does not exist" in i for i in r["data"]["issues"])

    def test_detect_plan_claims_mismatch(self):
        """检测 claims.json 引用了 PLAN.md 中不存在的 feature。"""
        write_plan(features=[1, 2])
        write_json("claims.json", {"features": {"3": {"phase": "developing"}}})
        r = run("cm doctor --repo test")
        assert any("not found in PLAN" in i for i in r["data"]["issues"])
```

### 11.7 E2E 测试

#### 10.7.1 单 Agent 完整流程

```python
def test_e2e_single_agent():
    """单 agent 完成 lock → plan → plan-ready → claim → dev → done → integrate → submit 全流程。"""
    repo = create_test_repo()

    # 1. lock
    r = run(f"cm lock --repo {repo}")
    assert r["ok"]
    assert Path(repo / ".coding-master/lock.json").exists()

    # 2. 创建 PLAN.md（agent 直接写）
    write_plan(repo, features=[
        {"id": 1, "title": "Add foo", "depends_on": [], "task": "Add foo()", "criteria": "- [ ] foo() exists"},
    ])

    # 2.5. 审核 PLAN.md
    r = run("cm plan-ready")
    assert r["ok"]
    assert read_json("lock.json")["session_phase"] == "reviewed"

    # 3. claim（审核通过后才能 claim）
    r = run("cm claim --feature 1")
    assert r["ok"]
    wt = r["data"]["worktree"]
    feature_md = r["data"]["feature_md"]
    assert Path(wt).exists()
    assert Path(feature_md).exists()

    # 4. 分析（在 feature MD 中写 Analysis + Plan）
    write_analysis_and_plan(feature_md)
    r = run("cm dev --feature 1")
    assert r["ok"]

    # 5. 开发（在 worktree 里修改代码）
    write_file(Path(wt) / "foo.py", "def foo(): return 42")
    run_git(["add", "foo.py"], cwd=wt)
    run_git(["commit", "-m", "add foo"], cwd=wt)

    # 6. 测试
    r = run("cm test --feature 1")
    assert r["data"]["test_passed"]

    # 7. done
    r = run("cm done --feature 1")
    assert r["ok"]

    # 6. integrate（集成验证）
    r = run("cm integrate")
    assert r["ok"]
    assert read_json("lock.json")["session_phase"] == "integrating"

    # 7. submit
    r = run(f"cm submit --repo {repo} --title 'feat: add foo'")
    assert r["ok"]

    # 验证最终状态
    assert not Path(repo / ".coding-master/lock.json").exists() or read_json("lock.json") == {}
    # PR 已创建（mock 或真实 gh）
```

#### 10.7.2 多 Agent 并行流程

```python
def test_e2e_multi_agent_parallel():
    """两个 agent 并行开发有依赖关系的 4 个 feature。

    模拟 Walkthrough (§7) 的完整流程。
    """
    repo = create_test_repo()

    # Agent A: lock
    run("cm lock --repo test --agent agent-a")

    # Agent A: 创建 PLAN.md（4 features，1→{2,3}→4）
    write_plan(repo, features=[
        {"id": 1, "title": "Extract Scanner", "depends_on": []},
        {"id": 2, "title": "Incremental Scan", "depends_on": ["1"]},
        {"id": 3, "title": "Split Reporter", "depends_on": ["1"]},
        {"id": 4, "title": "Integration Tests", "depends_on": ["2", "3"]},
    ])

    # Agent A: 审核 PLAN.md
    run("cm plan-ready")

    # Agent B: 尝试 claim Feature 3（应被阻塞）
    r = run("cm claim --feature 3 --agent agent-b")
    assert not r["ok"]
    assert "blocked" in r["error"]

    # Agent A: claim + 完成 Feature 1
    run("cm claim --feature 1 --agent agent-a")
    do_dev(feature=1)  # 在 worktree 里写代码
    r = run("cm done --feature 1 --agent agent-a")
    assert r["ok"]
    assert any(u["id"] == "2" for u in r["data"]["unblocked"])
    assert any(u["id"] == "3" for u in r["data"]["unblocked"])

    # Agent A + B: 并行 claim Feature 2 和 3
    r2 = run("cm claim --feature 2 --agent agent-a")
    r3 = run("cm claim --feature 3 --agent agent-b")
    assert r2["ok"] and r3["ok"]

    # 验证 worktree 基点：Feature 2/3 应基于 Feature 1 的 branch
    # (具体验证方式取决于实现)

    # Agent B 先完成 Feature 3
    do_dev(feature=3)
    r = run("cm done --feature 3 --agent agent-b")
    assert r["ok"]
    assert len(r["data"]["unblocked"]) == 0  # Feature 4 还被 Feature 2 阻塞

    # Agent A 完成 Feature 2
    do_dev(feature=2)
    r = run("cm done --feature 2 --agent agent-a")
    assert r["ok"]
    assert any(u["id"] == "4" for u in r["data"]["unblocked"])

    # Agent A: claim + 完成 Feature 4
    run("cm claim --feature 4 --agent agent-a")
    do_dev(feature=4)
    run("cm done --feature 4 --agent agent-a")

    # 检查进度
    progress = run("cm progress")
    assert progress["data"]["done"] == 4
    assert progress["data"]["developing"] == 0

    # integrate（集成验证）
    r = run("cm integrate")
    assert r["ok"]

    # submit
    r = run("cm submit --title 'refactor: split inspector'")
    assert r["ok"]
```

#### 10.7.3 崩溃后恢复的 E2E 流程

```python
def test_e2e_crash_recovery():
    """模拟完整流程中多次崩溃，每次通过 doctor + 重试恢复。"""
    repo = create_test_repo()

    # 正常 lock
    run("cm lock --repo test")
    write_plan(repo, features=[{"id": 1, "title": "Add foo", "depends_on": []}])

    # 模拟 claim 崩溃（残留 worktree）
    os.makedirs(f"../{repo.name}-feature-1")
    write_file(".coding-master/features/01-add-foo.md", "...")
    # claims.json 未更新 → feature 仍是 pending

    # doctor 检测到残留
    r = run("cm doctor --repo test")
    assert any("orphaned" in i for i in r["data"]["issues"])

    # doctor 修复
    run("cm doctor --repo test --fix")

    # plan-ready + 正常重新 claim
    run("cm plan-ready")
    r = run("cm claim --feature 1")
    assert r["ok"]

    # 正常完成
    do_dev(feature=1)
    run("cm done --feature 1")
    run("cm submit --title 'feat: add foo'")
```

### 11.8 测试基础设施

```python
# tests/conftest.py

import pytest, tempfile, shutil, subprocess
from pathlib import Path

@pytest.fixture
def test_repo(tmp_path):
    """创建一个临时 git repo 用于测试。"""
    repo = tmp_path / "test-repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=repo, check=True, capture_output=True)
    # 注册到 coding-master 配置
    config = {"repos": {"test-repo": str(repo)}}
    write_json(Path("~/.alfred/coding-master.json").expanduser(), config)
    yield repo
    # cleanup: 删除所有 feature worktree
    for d in tmp_path.iterdir():
        if d.name.startswith("test-repo-feature-"):
            shutil.rmtree(d)

@pytest.fixture
def locked_repo(test_repo):
    """创建并锁定一个测试 repo。"""
    run(f"cm lock --repo test-repo")
    yield test_repo

def parallel_run(n, cmd_fn):
    """并行运行 n 个子进程，返回结果列表。"""
    import multiprocessing
    with multiprocessing.Pool(n) as pool:
        return pool.map(cmd_fn, range(n))
```

### 11.9 测试覆盖矩阵

| 测试类别 | 用例数 | 覆盖目标 |
|----------|--------|----------|
| 单元测试 | ~57 | 每个内部函数的正常/异常路径（含 cmd_test、cmd_done、cmd_plan_ready、cmd_integrate、cmd_reopen、_topo_sort、_resolve_agent、updater 回滚） |
| 并发测试 | ~5 | flock 原子性、竞态条件 |
| 崩溃恢复测试 | ~14 | §8.1 中每个崩溃点（含 cm test、cm integrate、cm reopen 崩溃） |
| 幂等测试 | ~13 | 每个工具的重复执行（含 plan-ready 幂等、integrate 幂等、claim before plan-ready、submit before integrate、reopen not done） |
| doctor 测试 | ~7 | §8.3 中每个检查项 |
| E2E 测试 | ~3 | 单 agent / 多 agent / 崩溃恢复 |
| **合计** | **~99** | |

**优先级**：
1. **P0**（必须在 Phase 1 完成）：单元测试 + 幂等测试 — 保证基本正确性
2. **P1**（Phase 1 完成前）：并发测试 + 崩溃恢复测试 — 保证鲁棒性
3. **P2**（Phase 2 验证）：E2E 测试 — 保证端到端流程

## 12. 迁移策略

### Phase 1：新建 + 并行
- 实现 tools.py（lock, unlock, plan-ready, claim, dev, test, done, reopen, integrate, progress, submit, journal, renew, doctor, status）
- 重写 SKILL.md 为公约模式
- 完成 P0 测试（单元 + 幂等）和 P1 测试（并发 + 崩溃恢复）
- 旧 dispatch.py 保留

### Phase 2：切换验证
- 在真实任务中验证 MD 驱动流程
- 重点验证：多 agent 并行认领、PLAN.md 解析容错
- 完成 P2 测试（E2E）

### Phase 3：删除旧代码
- 删除 dispatch.py, workspace.py, config_manager.py, feature_manager.py, test_runner.py, git_ops.py, env_probe.py, repo_target.py, engine/
- 删除对应测试
- ~4400 行代码移除

---

## 13. 设计原则

1. **四层架构** — 公约（SKILL.md）→ 计划（MD）→ 工具（Python）→ 数据（JSON），上层依赖下层
2. **agent 只接触上两层** — 读写 MD + 调用工具，永远不碰 JSON（就像用户不直接写数据库）
3. **需要表达的上浮，需要保证的下沉** — agent 理解的用 MD，程序保证的用 JSON
4. **公约不可变** — SKILL.md 是人类定义的宪法，agent 不得修改；违规由 inspector 检测纠正（公约约束而非技术强制）
5. **工具只做机械活** — 不做编排、不做分析、不做开发。`_cm_next` 是唯一例外：它编排机械步骤的自动推进，但创造性工作（写代码）仍由 agent 完成
6. **两级状态机 + 双关卡** — session 级（locked → reviewed → working → integrating → done）+ feature 级（pending → analyzing → developing → done）；Plan review 是入口关卡（防低质量拆分），Integration 是出口关卡（防合并回归）
6b. **两层工具架构 + 断点模式** — Agent 只接触 7 个工具（`_cm_next` + `_cm_edit` + read/find/grep + status + doctor），内部 15+ 个工具由 `_cm_next` 自动调用。状态机对 agent 不可见，`_cm_next` 自动推进机械步骤，只在需要创造力的断点停下来。Agent 的工作流简化为：调 `_cm_next` → 做系统说的事 → 再调 `_cm_next`
7. **测试绑定 commit** — `cm test` 将测试结果（含输出摘要）写入 developing 子状态并绑定 commit SHA；代码变更后 test_commit ≠ latest_commit，`cm done` 拒绝，必须重新测试
8. **双层质量循环** — 内层 test 循环解决"代码能不能跑"，外层 review 循环（独立 engine）解决"代码写得好不好"；review 打回触发新一轮 test 循环；`cm done` 要求双重 gate（test passed + review approved）均不 stale
9. **三级测试** — feature 级 `cm test` 验证单个 feature；feature 级 `cm review` 独立 engine 审查代码质量；session 级 `cm integrate` 合并所有 feature 后跑全量测试，防止 feature 间交互导致的回归
10. **每个文件有且只有一个 owner** — 消除多方写同一文件的冲突（JOURNAL.md 例外：flock + append-only，无冲突）
11. **先副作用后提交** — 先创建可逆的副作用（worktree/文件），最后原子写入 JSON；崩溃时状态未变，残留由 doctor 清理
12. **工具幂等** — 所有工具崩溃后重跑安全，不会产生重复状态或副作用
13. **Lease 防竞态** — lock 有过期时间，长任务需 renew；过期后操作被拒绝，避免多方同时写
14. **自愈能力** — `cm doctor` 检测并修复所有已知的不一致状态，是崩溃恢复的最后防线
15. **单机约束** — 并发安全依赖 flock，仅保证同一台机器上的多 agent 原子性
16. **Dev branch 纯基线** — dev branch 在 session 期间不接受直接 commit，只做初始基线和最终 merge 汇总点
17. **失败不写入** — `_atomic_json_update` 在 updater 返回 `ok:false` 时恢复快照，防止意外的 data 修改泄露到文件。注意 `cm test` 的 updater 始终返回 `ok:true`（无论测试是否通过），因为测试失败结果本身需要持久化到 claims.json 供接力 agent 读取
17. **Session/Feature 职责分离** — session 级排他锁防多 plan 冲突，feature 级 claim + worktree 隔离防代码冲突，不需要 feature 级文件锁
18. **指引可操作化** — `cm progress` 输出分步操作列表（action_steps），每步都是可直接执行的指令，接力 agent 无需额外探索即可接手工作
19. **数据层即事实** — lock.json/claims.json 是跨 turn、跨会话的唯一事实来源；所有命令先读数据层再决策，不依赖 git branch 列表或提示词传递上下文
20. **Lease 自动续期** — `_check_lease` 发现过期时自动续期而非拒绝操作，避免长任务因 lease 超时而中断；竞态安全由 flock 保证
21. **模式隔离** — read-only overlay session（review/analyze）不修改 lock.json，不影响正在进行的 write session；`cm unlock` 拒绝清除未完成的 write session（需 `--force` 或先 `cm submit`）
22. **Evidence-driven 验证** — `evidence/N-verify.json` 记录结构化测试结果（pass/fail/skipped + 输出摘要），供接力 agent 和 integration 阶段引用
23. **Delegation 硬关卡** — `delegation/N-delegation.json` 记录子任务委派，feature 完成前必须验证所有 delegation 已回收
24. **Session Worktree 隔离** — `cm lock` 通过 `git worktree add` 在独立目录创建 session worktree，主 repo 工作区不受任何 session 操作影响。所有 session 级 git 操作（integrate merge、submit commit/push）在 session worktree 中执行。`cm submit` 完成后清理 session worktree。避免了 checkout 切分支导致用户未提交修改丢失的严重问题
25. **Hints 闭环** — 所有 hints/errors/next_action 中引用的工具必须存在于 skillkit 注册表中（`TestHintToolConsistency` 守护测试强制）。v5.0 中注册表只有 7 个工具，hints 只应引用这 7 个
26. **系统编排 + 断点创造** — 状态机由 `_cm_next` 自动推进（系统编排），agent 只在创造性断点停下来（写 plan、写 analysis、写代码、修 bug）。Agent 不需要知道 session_phase、feature_phase 等概念
27. **断点契约** — `_cm_next` 的每个断点返回统一结构：`breakpoint` 类型 + `instruction` 自然语言指引 + `context` 必要上下文（feature spec、worktree 路径、test output 等）。Agent 只需按 instruction 操作
