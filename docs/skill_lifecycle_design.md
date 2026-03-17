# 技能生命周期管理系统设计文档

## 1. 概述

技能生命周期管理系统（Skill Lifecycle Management，简称 SLM）旨在将技能从"静态配置"升级为"可迭代演化"的状态。

**核心问题：** 现有技能一旦编写完成便固化不变，缺乏评估、更新、版本管理和淘汰机制，无法根据实际使用效果持续优化。

**目标：** 构建一套闭环系统，使技能能够持续被评估、更新、上线验证，并在整个对话生命周期中不断演化提升。

## 2. 系统架构

### 2.1 功能流程

```
┌──────────────────────────────────────────────────────────┐
│                    技能生命周期                           │
│                                                          │
│   使用中的技能                                           │
│       │                                                  │
│       ▼                                                  │
│   ┌─────────┐    触发条件     ┌──────────────┐          │
│   │ 评估系统 │ ─────────────► │  判断与决策   │          │
│   └─────────┘                └──────┬───────┘          │
│                                     │                    │
│                        ┌────────────┴────────────┐      │
│                        ▼                         ▼      │
│                  ┌──────────┐           ┌──────────────┐ │
│                  │ 开发新技能│           │ 更新现有技能  │ │
│                  └────┬─────┘           └──────┬───────┘ │
│                       │                        │         │
│                       └───────────┬────────────┘         │
│                                   ▼                      │
│                          ┌─────────────────┐             │
│                          │   上线验证流程   │             │
│                          └────────┬────────┘             │
│                                   │                      │
│                          通过 ◄───┤───► 失败             │
│                           │                │             │
│                           ▼                ▼             │
│                       全量上线          回滚/废弃         │
└──────────────────────────────────────────────────────────┘
```

### 2.2 进程模型与现有系统关系

SLM **不引入新的常驻进程**，完全寄生在现有运行时之上，通过明确的接触面与现有系统解耦。

#### 现有进程模型（背景）

```
进程 1: EverBot Daemon（单进程，纯 asyncio，无线程）
├── asyncio 事件循环
│   ├── Scheduler 协程
│   │   └── HeartbeatRunner（per agent）
│   │       ├── inline tasks ── 共享主会话，合并到 heartbeat turn
│   │       └── isolated tasks ── 独立 session 文件 + 独立 agent 实例
│   │                             （仍在同一进程内，协程级隔离）
│   ├── Telegram 通道（async websocket）
│   └── Job 清理循环
│
进程 2: Web Server（可选，独立 FastAPI 进程）
│   REST API + WebSocket，提供 Web UI
│
进程 3+: 用户交互（CLI 命令 或 Web UI）
    everbot CLI / 浏览器 → 与 Daemon/Web Server 通信
```

**技能加载**：Agent 启动时扫描 `skills/` 目录，读取 `SKILL.md` frontmatter 注册可用技能。技能在会话中被激发时，完整指令注入当前 turn 执行。
```

#### SLM 各模块的运行位置

| SLM 模块 | 所在进程 | 运行方式 | 说明 |
|----------|---------|---------|------|
| **日志采集** | 进程 1（Daemon） | SkillContext 后置逻辑，随技能执行协程运行 | 技能激发完成后异步写 JSONL，不阻塞主流程 |
| **LLM Judge 评分** | 进程 1（Daemon） | isolated task 协程，由 Scheduler 调度 | 读取 `skill_logs/`，批量评分，写入 `eval_report.json` |
| **决策引擎** | 进程 1（Daemon） | isolated task 协程，由 Scheduler 调度 | 读取评估报告，判断是否需要更新/回滚，产出决策记录 |
| **灰度观察** | 进程 1（Daemon） | isolated task 协程，由 Scheduler 调度 | 定期检查新版本的 segment 聚合评分，触发阈值判断 |
| **技能生成** | 进程 3（用户交互） | 人工触发，在用户会话中执行 | LLM 生成草稿 + 人工审核，不适合全自动 |
| **版本发布/回滚** | 进程 3（用户交互） | CLI 脚本或人工操作 | 覆盖 SKILL.md + 更新 `.eval/` 元数据，纯文件操作 |

#### 与现有系统的接触面

SLM 与现有运行时只有**三个接触点**，其余完全解耦：

```
现有系统                          SLM（离线管理层）
──────────                       ─────────────────

SKILL.md ◄──── 唯一写入点 ────── 版本发布/回滚
（loader 读取）                   （覆盖文件内容）

SkillContext ──── 数据流出 ──────► skill_logs/*.jsonl
（技能执行完成后）                  （Evaluation Segment）

HEARTBEAT.md ◄── 注册 SLM 任务 ── LLM Judge / 决策引擎 / 灰度观察
（Scheduler 读取）                 （作为 isolated task 调度）
```

**解耦原则**：
- **SLM 不修改 loader**：loader 只认 SKILL.md，SLM 通过覆盖文件内容间接影响 loader，不侵入加载逻辑
- **SLM 不修改 Scheduler**：SLM 的后台任务作为普通 isolated task 注册到 HEARTBEAT.md，由现有 Scheduler 统一调度
- **SLM 不修改会话管理**：日志采集通过 SkillContext 后置逻辑旁路写入，不修改会话流程
- **`.eval/` 目录对运行时不可见**：即使整个 `.eval/` 被删除，技能正常运行不受影响

## 3. 核心模块

### 3.1 评估系统

#### 3.1.1 数据采集

对每次技能激发，记录一个**评估片段（Evaluation Segment）**：

```
{
  skill_id: string,          // 技能标识
  skill_version: string,     // 版本号（来自 SKILL.md frontmatter 中的 version 字段，会话启动时随 skill 内容一起加载并固化到会话上下文，不从 .eval/current.json 读取）
  triggered_at: timestamp,
  context_before: string,    // 激发前的上下文（触发前 1 轮对话）
  skill_output: string,      // 技能处理内容
  context_after: string,     // 激发后的用户反应（触发后 1 轮对话）
  session_id: string
}
```

**下文信号** 是最重要的隐式反馈来源，包括：
- 用户是否让 Claude 重做
- 用户是否表达不满
- 任务是否顺利完成

#### 3.1.2 评估维度

| 维度 | 说明 | 计算方式 |
|------|------|----------|
| **严重问题率** | 触发后导致明显错误的比例 | 出错片段数 / 总片段数 |
| **满意度分** | 用户对技能处理结果的满意程度 | LLM Judge 对下文信号打分，0-1 |

**LLM Judge 评分逻辑：**
- 输入：上文 + 技能处理 + 下文
- 输出：{ has_critical_issue: bool, satisfaction: float, reason: string }
- 对 N 个片段取平均，得到该版本的综合评分

#### 3.1.3 触发条件

评估任务在以下情况触发：
- **动态阈值**：新版本上线初期阈值较低（如每 10 次激发），稳定运行后逐步放宽（如每 50 次激发）
- 定期兜底评估（如每周）
- 手动触发

---

### 3.2 判断与决策

根据评估结果，决定下一步行动：

**自动决策（基于评估指标）：**

```
if 样本量 < 20 且出现 critical issue:
    → 暂停放量，人工复核后决定回滚或继续
elif 样本量 ≥ 20 且严重问题率 > 阈值（如 10%）:
    → 自动回滚到上一稳定版本
elif 满意度分 < 下限（如 0.6）:
    → 触发"更新现有技能"流程
else:
    → 无需操作，继续观察
```

**人工触发：**

- 开发新技能：由用户主动提出需求（"我需要一个处理 X 场景的技能"），不依赖自动评估

---

### 3.3 技能生成

#### 3.3.1 更新现有技能

输入：
- 当前技能内容
- 评估报告（低分片段 + 失败原因）

LLM 生成改进方案，产出新版本草稿，供人工审核或直接进入验证流程。

#### 3.3.2 开发新技能

输入：
- 未被现有技能覆盖的使用场景描述
- 相关上下文片段示例

LLM 提出技能思路（ideas），经人工确认后，生成新技能草稿。

---

### 3.4 上线验证流程

新版本（或新技能）上线前，经过分阶段验证：

#### 阶段一：离线评估（沙盒测试）

- 构造历史片段集作为测试集
- 用新版本重新处理，对比 LLM Judge 评分
- 必须通过基线分数才能进入在线阶段

#### 阶段二：分阶段灰度验证

新版本上线后，每次激发自然产生 Evaluation Segment（含 `context_before` + `skill_output` + `context_after`），由 LLM Judge 逐条评分。以离线回放为主要验证手段，在线观察为辅；旧版本历史聚合分仅作弱基线参考，用于发现明显退化，不用于得出严格优劣结论：

| 阶段 | 激发次数窗口 | 通过条件 | 失败处理 |
|------|------------|---------|---------|
| 密集观察 | 前 5 次 | 无 critical issue | 出现 critical 时暂停放量，**人工复核确认**后决定回滚或继续（样本量不足以支撑自动决策） |
| 中等观察 | 前 20 次 | 严重问题率 ≤ 5%（人工复核放行的事件不计入） | 超过阈值则自动回滚（≥ 20 次样本量可支撑自动判断） |
| 扩大观察 | 前 50 次 | 无明显退化（满意度分未大幅低于旧版本聚合分） | 疑似退化则延长观察窗口或人工复核，不自动回滚 |
| 全量上线 | 50 次以上 | 严重问题率持续达标，满意度分无明显退化 | 人工复核后决定是否继续或回滚 |

**最小样本量保护**：自动回滚仅在样本量 ≥ 20 时启用。低于此阈值的阶段（密集观察期），critical issue 触发人工复核而非自动回滚，以避免单点噪声导致误判。经人工复核放行的 critical issue 事件不计入后续阶段的自动回滚分母，避免"人工已判定可接受但规则仍强制回滚"的矛盾。

**满意度对比定位**：旧版本历史聚合分仅作弱基线参考，用于发现明显退化趋势，不作为自动回滚的硬门槛。满意度疑似退化时延长观察窗口或触发人工复核，由人工决定是否继续。

**新技能（无旧版本）的验证**：没有历史 segments 可对比，通过离线沙盒评估 + 人工判断过关后直接进入密集观察阶段，仅考核严重问题率，满意度分在积累足够 segments 后作为后续迭代的基线。

---

### 3.5 版本管理

版本管理利用现有 loader 的多目录优先级机制，不修改 loader，不修改 repo 内的 skill 文件。

#### Skill 加载优先级（现有机制）

```
优先级从高到低（first-match-wins）：
1. ~/.alfred/agents/{agent}/skills/{skill_id}/SKILL.md  ← agent 专属
2. ~/.alfred/skills/{skill_id}/SKILL.md                  ← 全局用户目录
3. {repo}/skills/{skill_id}/SKILL.md                     ← 仓库内置（git-tracked，只读）
```

Loader 按此顺序扫描，同名 skill 只加载第一个匹配项。

#### SLM 的版本管理策略

**核心思路**：repo 内的 skill 是只读基线，SLM 的所有版本变更都写入 `~/.alfred/skills/`（全局用户目录），通过优先级覆盖 repo 基线。

```
{repo}/skills/{skill_id}/
  SKILL.md                          ← 只读基线，SLM 不修改，git-tracked

~/.alfred/skills/{skill_id}/
  SKILL.md                          ← SLM 管理的当前生效版本（覆盖 repo 基线）
  .eval/                            ← 纯离线产物，loader 不扫描
    current.json                    ← 指针：当前版本号 + 稳定版本号
    versions/
      v1.0/
        skill.md                    ← 该版本的 SKILL.md 快照
        metadata.json               ← 该版本的状态、评估摘要
        eval_report.json            ← 该版本的评估报告
      v1.1/
        skill.md
        metadata.json
        eval_report.json
      ...
```

**发布新版本**：将新 SKILL.md 写入 `~/.alfred/skills/{skill_id}/SKILL.md`，下次会话启动时 loader 会优先加载此文件，覆盖 repo 基线。

**回滚到稳定版**：将 `.eval/versions/v{stable}/skill.md` 覆盖回 `~/.alfred/skills/{skill_id}/SKILL.md`。

**回滚到 repo 基线**：删除 `~/.alfred/skills/{skill_id}/SKILL.md`，loader 自动回退到 repo 内的只读版本。

`current.json` 只做指针，内容极简：
```json
{
  "current_version": "2.0",
  "stable_version": "1.1",
  "repo_baseline": true
}
```

`repo_baseline` 为 `true` 时，表示 stable 版本就是 repo 内的原始版本（即"删除覆盖层"即可回滚）。

#### 运行时真相 vs 离线分析产物

| 类别 | 文件 | 位置 | 读取者 |
|------|------|------|--------|
| **运行时真相** | `SKILL.md`（含 frontmatter `version` 字段） | `~/.alfred/skills/` 或 `{repo}/skills/`（按优先级） | loader（会话启动时扫描） |
| **运行时真相** | `.reflection_state.json` | 工作区根目录 | scanner（已有机制） |
| **离线分析产物** | `.eval/*` | `~/.alfred/skills/{skill_id}/.eval/` | 评估系统、回滚脚本 |
| **离线分析产物** | `skill_logs/*.jsonl` | `~/.alfred/skill_logs/` | 评估系统 |

**核心原则**：loader 只读 `SKILL.md`，绝不读 `.eval/`。`.eval/` 整个目录删除不影响技能运行。

#### 版本号嵌入 SKILL.md

版本号作为 SKILL.md frontmatter 的一部分，会话启动时随 skill 内容一起被 loader 加载并固化到会话上下文中：

```markdown
---
name: example-skill
version: "2.0"
description: ...
---
（技能内容）
```

repo 基线中的 SKILL.md 可以不含 `version` 字段（视为 `"baseline"`），SLM 首次接管时标记版本号。

这确保 Evaluation Segment 中的 `skill_version` 始终反映当前会话实际运行的版本，不受回滚操作影响。

#### 回滚语义

回滚不是实时熔断，而是分为两个独立动作：

**动作一：阻止新会话加载问题版本**

视回滚目标分两种情况：

- **回滚到历史稳定版**：将 `.eval/versions/v{stable}/skill.md` 覆盖回 `~/.alfred/skills/{skill_id}/SKILL.md`
- **回滚到 repo 基线**（`repo_baseline: true`）：删除 `~/.alfred/skills/{skill_id}/SKILL.md`，loader 自动回退到 `{repo}/skills/` 中的只读版本

两种情况都需要更新 `current.json` 并将问题版本 `metadata.json` 标记为 `suspended`。

以上为顺序文件写入（非原子操作）。崩溃恢复规则：**以 loader 实际加载的 SKILL.md frontmatter 中的 `version` 为准**，如果与 `current.json` 不符则修正 `current.json`。

**动作二：当前活跃会话的隔离**

现有 loader 在会话启动时扫描，不支持运行中热更新。因此回滚后当前活跃会话仍使用问题版本内容。SOP 要求：
- 回滚完成后提示用户开启新会话以使回滚生效
- 当前会话产生的 Evaluation Segment 仍会被正确记录到问题版本名下（因为 `skill_version` 来自会话启动时固化的 frontmatter，不受回滚操作影响）

#### 状态与转换

```
draft → testing → active → deprecated
                    ↓ (回滚)
                 suspended → testing (修复后重新验证)
                           → deprecated (放弃修复)
```

每个版本目录下的 `metadata.json` 只描述该版本自身：
```json
{
  "version": "2.0",
  "created_at": "...",
  "status": "active",      // draft / testing / active / suspended / deprecated
  "verification_phase": "full",
  "eval_summary": {
    "critical_issue_rate": 0.02,
    "satisfaction_score": 0.81
  }
}
```

#### 回滚操作 SOP

当严重问题率超过阈值或灰度验证失败时，执行以下回滚流程：

1. 从 `~/.alfred/skills/{skill_id}/.eval/current.json` 读取 `stable_version` 和 `repo_baseline`
2. 切换生效版本：
   - 若 `repo_baseline: true`：删除 `~/.alfred/skills/{skill_id}/SKILL.md`，loader 回退到 repo 基线
   - 若 `repo_baseline: false`：将 `.eval/versions/v{stable}/skill.md` 覆盖回 `~/.alfred/skills/{skill_id}/SKILL.md`
3. 更新 `current.json` 的 `current_version`
4. 在问题版本的 `.eval/versions/v{问题版本}/metadata.json` 中标记 `suspended`，记录回滚原因
5. 生成回滚事件日志
6. 提示用户开启新会话以使回滚生效
7. 后续可对 `suspended` 版本修复后重新进入 `testing`，或标记为 `deprecated` 放弃

**崩溃恢复**：以 loader 实际加载的 SKILL.md frontmatter version 为准，修正 `current.json`。

---

## 4. 数据流

```
对话发生
    │
    ▼
技能激发 → 记录 Evaluation Segment → 存储到 skill_logs/
    │
    ▼（达到阈值或定时触发）
LLM Judge 批量评分
    │
    ▼
生成 eval_report.json
    │
    ▼
决策引擎判断是否需要更新/新增
    │
    ▼
生成新版本草稿 → [人工审核] → 进入验证流程
    │
    ▼
分阶段灰度验证 → 全量上线 or 回滚
```

---

## 5. 与外部系统集成

### 5.1 日志采集集成

技能激发后的 Evaluation Segment 采集通过 `SkillContext` 的后置逻辑实现：
- 技能执行完成后，在 `SkillContext` 内将上下文（context_before/after）和技能输出异步写入 `skill_logs/`
- 写入操作不阻塞主流程，不修改现有技能执行路径
- 适用于 HeartbeatRunner 调度的反射技能和用户会话中触发的交互技能

---

## 6. 实施路线图

### MVP（第一阶段）
- [ ] 技能激发日志记录（Evaluation Segment 采集）
  - 存储路径：`skill_logs/{skill_id}.jsonl`，每行一个 Evaluation Segment（JSON 格式）
  - 清理策略：保留最近 90 天或最近 500 条（以先到为准）
- [ ] LLM Judge 离线评分脚本
- [ ] 评估报告生成
- [ ] 版本目录结构 + metadata 规范

### 第二阶段
- [ ] 决策引擎（基于评估结果触发更新流程）
- [ ] LLM 辅助生成新版本草稿
- [ ] 离线沙盒测试框架

### 第三阶段
- [ ] 灰度验证自动化（阶段推进、阈值判断、自动回滚）
- [ ] 可观测性 Dashboard（评分趋势、版本对比）

---

## 7. 关键设计决策

| 问题 | 决策 | 理由 |
|------|------|------|
| 满意度如何获取 | 使用 LLM Judge 对下文信号评分 | 用户量小，无法依赖显式评分；隐式信号更自然 |
| 在线验证方式 | 基于 Evaluation Segment 的分阶段灰度验证 | 每个 segment 自带 context_before/after，无需流量分组；以离线回放为主、在线观察为辅，历史聚合分仅作弱基线参考，用于发现明显退化，不用于得出严格优劣结论 |
| 新版本生成方式 | LLM 生成 + 人工审核 | 全自动风险高，保留人工把关 |
| 版本存储位置 | `~/.alfred/skills/` 覆盖层 + `.eval/` 子目录 | 利用 loader 现有的 first-match-wins 优先级机制，repo skill 只读不动，SLM 变更写入用户目录覆盖，回滚可直接删除覆盖层回退到 repo 基线 |
| 版本号来源 | 嵌入 SKILL.md frontmatter | 会话启动时随 skill 内容固化，回滚/指针变更不会污染当前会话的评估日志 |
| 回滚生效时机 | 下次会话启动 | 现有 loader 不支持热更新，回滚拆为"阻止新会话加载"+"当前会话隔离"两个动作，不假装有实时熔断能力 |
