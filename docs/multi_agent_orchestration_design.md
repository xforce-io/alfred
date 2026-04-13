# Multi-Agent Orchestration Design

> **Version**: 0.9.0
> **Date**: 2026-03-02
> **Status**: Draft
> **Author**: xupeng

---

## Table of Contents

1. [Background & Motivation](#1-background--motivation)
2. [Design Goals & Principles](#2-design-goals--principles)
3. [Architecture Overview](#3-architecture-overview)
4. [Core Concepts](#4-core-concepts)
5. [Workflow Engine](#5-workflow-engine)
6. [Workflow Configuration & SKILL Integration](#6-workflow-configuration--skill-integration)
7. [Error Recovery & Context Management](#7-error-recovery--context-management)
8. [Observability](#8-observability)
9. [Example: Bugfix Workflow](#9-example-bugfix-workflow)
10. [Example: Feature-Dev Workflow](#10-example-feature-dev-workflow)
11. [Migration Path](#11-migration-path)
12. [Future Work](#12-future-work)
13. [Appendix](#appendix)

---

## 1. Background & Motivation

### 1.1 现状

当前 Alfred 的执行模型是**单轮制**：每次 TurnOrchestrator.run_turn() 执行一次 LLM 交互循环，受固定的工具调用预算和超时约束。面对复杂工程任务（代码重构、多文件修改、测试-修复循环），这种模式有根本性的局限：

- **工具预算不足**：HEARTBEAT_POLICY 10 次、CHAT_POLICY/JOB_POLICY 20 次，无法完成需要上百次工具调用的任务
- **无阶段管理**：没有 research → plan → implement → verify 的结构化流程
- **无递归分解**：复杂任务无法拆分为子任务，每个子任务走自己的完整流程

### 1.2 SKILL 的灵活性 ≠ 确定性

当前 SKILL 体系（SOP markdown + dispatch 脚本）解决了**灵活性**问题——LLM 能做的事情变多了。但灵活性本身不等于任务能**完成**。

以某个技能的 `sop-feature-dev.md` 为例，SOP 写得很详细（调研、规划、实现、测试、提交），但在单轮执行下：

```
LLM 读 SOP → 写了几个文件 → 工具预算用完 → 结束
                                  ↑ 测试没跑，没验证，半成品
```

LLM 并不是不知道该跑测试——SOP 里写了。问题是**框架没有结构性保证**让它走到验证这一步。这是 instruction following 的固有局限：你可以告诉 LLM "写完要测试"，但你无法确保它在 20 次工具调用内既写完代码又跑完测试。

**没有 workflow 的 SKILL 是"知道怎么做"；有 workflow 的 SKILL 是"能做完并且做对"。**

### 1.3 Workflow：可验证、闭环、可递归

**目标**：在 Alfred 框架层构建**通用的 workflow 编排能力**，使 SKILL 从"灵活的知识库"升级为"确定性的执行引擎"。

Workflow 解决三个根本问题：

**验证闭环消除"自以为完成"**。没有 verify 阶段，LLM 写完代码就声称 done——它没有动机怀疑自己。有了 implement⟷verify 循环，"完成"的定义从 LLM 的主观判断变成了客观信号。

验证的强度取决于**验证标准的来源**，按可靠性从高到低：

| 验证来源 | 说明 | 适用场景 |
|---------|------|---------|
| **已有测试套件** | 项目既有的 pytest/CI，不依赖 LLM 发明 | bugfix（已有测试在跑或用户提供复现步骤） |
| **外部工具信号** | 类型检查通过、linter 无报错、构建成功 | 所有编码场景 |
| **plan 阶段定义的验收标准** | plan artifact 明确列出 case（经 checkpoint 人工确认），implement 据此写测试 | feature-dev（plan 用 lead 模型 + 人工审核） |
| **implement 自己写的测试** | LLM 同时写代码和测试 | 快速原型（风险：测试可能和代码犯同一个错） |

**设计原则：verify 阶段应尽量依赖 LLM 之外的客观信号**。框架通过 `verification_cmd` 支持外部验证——框架直接执行命令判定 pass/fail，LLM 不参与。

**阶段边界创造结构化决策点**。每个阶段边界是信息压缩点和决策校验点：research 的 artifact 迫使 LLM 先把调研结论写下来而不是边调研边写代码；plan 的 checkpoint 让用户可以在投入大量工具调用之前修正方向。

**递归分解让复杂度可控**。复杂任务可串行拆分为子 TaskSession，每个子任务在独立上下文中完成自己的 implement⟷verify 循环。

> 同样的 research→plan→implement⟷verify 骨架不绑定编码领域，但 v1 聚焦编码场景。

---

## 2. Design Goals & Principles

### Goals

| # | Goal | Description |
|---|------|-------------|
| G1 | **结构化闭环** | 框架保证 research→plan→implement⟷verify 完整走完，不依赖 LLM instruction following |
| G2 | **外循环回退** | verify 反复失败时可回退到 plan 甚至 checkpoint 等人工介入，而非无限重试同一个错误方案 |
| G3 | **SKILL 声明式** | SKILL 通过 workflow YAML 声明流程，通过 SOP markdown 提供领域知识 |
| G4 | **渐进式采用** | 现有 SKILL 不受影响，复杂 SKILL 可选择性启用多阶段能力 |
| G5 | **资源可控** | 总工具调用、总时间、循环次数均有上限，防止失控 |
| G6 | **可观测** | 结构化日志覆盖全生命周期，关键路径有 metrics，支持事后诊断 |

### Principles

1. **TurnOrchestrator 不变**：它仍然是单轮执行引擎，新的编排层建立在它之上
2. **框架驱动结构**：阶段顺序、退出条件、工具白名单、预算、回退——由框架代码强制执行，不依赖 LLM instruction following
3. **LLM 驱动内容**：阶段内做什么、artifact 产出什么、子任务如何拆分——由 LLM 决策
4. **失败是常态**：工具执行失败、阶段未达标是正常情况，框架提供结构化的恢复路径（含回退）
5. **Markdown 作为通用语**：阶段间 artifact 传递、错误上下文注入均使用结构化 Markdown

---

## 3. Architecture Overview

### 3.1 Layer Diagram

```
┌──────────────────────────────────┐
│          Channel Layer           │
│        (Web / Telegram / CLI)    │
└──────────────┬───────────────────┘
               │
┌──────────────▼───────────────────┐
│        Scheduling Layer          │
│    (HeartbeatRunner / Scheduler) │
└──────────────┬───────────────────┘
               │
┌──────────────▼───────────────────┐
│    TaskSession Layer  [NEW]      │
│                                  │
│  TaskSession                     │
│    ├── Phase (串行阶段)           │
│    ├── PhaseGroup (循环阶段)      │
│    └── 子 TaskSession (串行递归)  │
│                                  │
└──────────────┬───────────────────┘
               │ (直接调用)
┌──────────────▼───────────────────┐
│      TurnOrchestrator Layer      │
│   (单轮 LLM 交互，工具调用)       │
└──────────────┬───────────────────┘
               │
┌──────────────▼───────────────────┐
│        Dolphin Agent Layer       │
│     (LLM Provider, Tool Exec)   │
└──────────────────────────────────┘
```

### 3.2 Key Changes

**新增 TaskSession Layer**，位于 Scheduling Layer 和 TurnOrchestrator Layer 之间：

- **Phase**：独立阶段，有自己的 system prompt、工具白名单、预算
- **PhaseGroup**：implement⟷verify 紧密循环，含外循环回退机制
- **子 TaskSession**：复杂子任务可串行生成子 TaskSession，拥有独立的 phase 流程和上下文

现有的简单任务（inline heartbeat、普通 chat）仍然直接走 TurnOrchestrator，不经过 TaskSession。

TaskSession 直接调用现有模块（TurnOrchestrator、AgentFactory、SessionManager）。待接口稳定后再考虑抽象 Port/Adapter（见 [Future Work](#11-future-work)）。

---

## 4. Core Concepts

### 4.1 执行单元

```
TaskSession（递归单元）
  │  拥有完整的 phase 流程（research → plan → implement⟷verify）
  │  可串行生成子 TaskSession（递归分解复杂子任务）
  │
  ├── Phase（阶段单元）
  │     多轮 turn，有 artifact 产出
  │     每个 Phase 有独立的 system prompt、工具白名单、预算
  │
  └── PhaseGroup（循环单元）
        显式声明 action_phase、verify_phase、可选 setup_phase（见 4.4）
        setup_phase 在首次进入和回退重入时执行一次（如编写测试骨架）
        循环直到 verify 通过或达到 max_iterations
        循环耗尽时可回退到前置 Phase（外循环）
```

### 4.2 框架 vs LLM 职责矩阵

| 维度 | 框架强制（代码） | LLM 决策（prompt） |
|------|-----------------|-------------------|
| Phase 顺序和流转 | ✓ | |
| PhaseGroup 循环和退出 | ✓ | |
| PhaseGroup 耗尽后回退 | ✓ | |
| 工具可用范围 | ✓ (hard filter) | |
| 预算和超时 | ✓ | |
| 递归深度限制 | ✓ | |
| cancel / pause 响应 | ✓ | |
| **是否启动 workflow** | ✓ (框架 hint) + LLM 判断 | **✓ (SKILL 上下文中判断)** |
| **选择哪个 workflow** | | **✓ (从 SKILL 声明的列表中选)** |
| 阶段内做什么 | | ✓ |
| 子 TaskSession 何时派发 | | ✓ |
| artifact 内容 | | ✓ |

### 4.3 外循环回退

这是本设计区别于简单线性 pipeline 的关键。单靠 implement⟷verify 内循环无法处理**方案本身错误**的情况：

```
内循环（PhaseGroup）：
  implement → verify → 失败 → 改代码 → verify → ...
  解决的是：实现有 bug

外循环（Phase 回退）：
  plan → implement⟷verify 耗尽 → 回退 plan → 新方案 → implement⟷verify
  解决的是：方案不对、遗漏了依赖、拆分有问题

最外层（Checkpoint）：
  外循环回退次数耗尽 → checkpoint 暂停，等用户介入
  解决的是：需求不清楚、超出 LLM 能力
```

**回退判定**：

| 触发条件 | 回退目标 | 注入上下文 |
|---------|---------|-----------|
| PhaseGroup 达到 max_iterations | 回退到 `rollback_target` 指定的 Phase（由 YAML 显式声明） | 所有 verify 失败摘要（含每轮单行历史 + 最后一轮完整输出） |
| LLM 在 implement 中显式声明方案不可行 | 回退到 `rollback_target` | LLM 的理由 + 已尝试的实现 |
| 回退次数达到 max_rollback_retries | Checkpoint 暂停 | 完整失败历史，等待用户输入 |

### 4.4 PhaseGroup 角色声明

PhaseGroup 显式声明 `action_phase` 和 `verify_phase`，而非依赖 phases 列表的隐式顺序：

```yaml
- group: implement_verify
  action_phase: implement      # 显式声明：循环重入时注入失败上下文的目标
  verify_phase: verify         # 显式声明：判定 pass/fail 的阶段
  setup_phase: write_tests     # 可选：首次进入时执行一次（如编写测试骨架）
  max_iterations: 5
  on_exhausted: rollback       # rollback | abort | checkpoint
  rollback_target: plan        # on_exhausted=rollback 时必填，指向 TaskSession 顶层的 Phase
  phases:
    - name: write_tests        # setup_phase：仅首次进入和回退重入时执行
      ...
    - name: implement
      ...
    - name: verify
      ...
```

**YAML 加载时校验规则**：
1. `action_phase` 和 `verify_phase` 必须引用 `phases` 列表中存在的 phase name
2. `verify_phase` 必须配置 `verification_cmd` 或 `verify_protocol`（见 5.3）
3. `phases` 列表至少包含 `action_phase` 和 `verify_phase` 对应的 2 个元素
4. **Phase 模式互斥**：配置了 `verification_cmd` 的 phase 为"纯命令模式"，`instruction_ref` / `max_turns` / `allowed_tools` 等 LLM 相关字段将被忽略并输出 WARNING 日志。配置了 `instruction_ref` 的 phase 为"LLM 驱动模式"。两者不能同时为空
5. **验证配置互斥**：同一 Phase 不能同时配置 `verification_cmd` 和 `verify_protocol`，YAML 校验时报错
6. 若配置了 `rollback_target`，该值必须引用 TaskSession 顶层 `phases` 列表中存在的 Phase name，且该 Phase 必须位于当前 PhaseGroup 之前
7. 若配置了 `setup_phase`，该值必须引用 `phases` 列表中存在的 phase name，且不能与 `action_phase` 或 `verify_phase` 相同
8. **`phases` 列表仅作为配置容器**：执行顺序由 `setup_phase` → `action_phase` → `verify_phase` 声明决定，与 `phases` 列表中的书写顺序无关

校验失败时在 workflow 加载阶段（而非运行时）报错，提供明确的 YAML 路径和错误原因。

### 4.5 Artifact 产出协议

Phase 间通过 artifact 传递信息，框架需要一个确定性的机制从 LLM 输出中提取 artifact。

#### 产出方式：结构化标签

框架在每个 LLM 驱动 Phase 的 system prompt 末尾追加：

```
在本阶段工作完成后，你必须用以下标签输出阶段产出：
<phase_artifact>
你的产出内容（Markdown 格式）
</phase_artifact>
```

#### 提取规则

```python
def _extract_artifact(phase_name: str, llm_output: str, last_assistant_text: str) -> str:
    """从 Phase 最终 LLM 输出中提取 artifact

    Args:
        llm_output: Phase 的完整 LLM 输出（含工具调用结果）
        last_assistant_text: 最后一条 assistant message 的纯文本（不含 tool results）
    """
    match = re.search(r"<phase_artifact>(.*?)</phase_artifact>", llm_output, re.DOTALL)
    if match:
        return match.group(1).strip()
    # fallback：未找到标签时，取最后一条 assistant message 的纯文本。
    # 不用完整 llm_output——它可能包含大量工具调用结果（文件内容等），
    # 作为 artifact 传给下游 Phase 会引入大量噪音。
    # artifact 不像 verify 那样需要严格的 pass/fail 判定，
    # 丢失标签不应导致 Phase 失败，但记录 WARNING 日志。
    logger.warning("workflow.artifact.tag_missing", extra={
        "phase": phase_name,
        "fallback": "last_assistant_text",
    })
    return _truncate(last_assistant_text, max_chars=4000)
```

#### 纯命令模式的 artifact

配置了 `verification_cmd` 的 Phase（如 verify），其 artifact 为命令的 stdout + stderr 输出（已在 `_run_verification_cmd` 中截断到 4000 字符）。不涉及 LLM，不需要标签提取。

#### 与 verify_result 标签的关系

verify phase 可能同时产出 `<verify_result>` 和 `<phase_artifact>`。两者职责不同：

- `<verify_result>`: 判定 pass/fail（框架控制流用）
- `<phase_artifact>`: 阶段产出内容（下游 Phase 用）

LLM 驱动的 verify phase 中，如果只配置了 `verify_protocol: "structured_tag"` 而未产出 `<phase_artifact>`，框架将 verify_result 标签的内容作为该 Phase 的 artifact。

---

## 5. Workflow Engine

### 5.1 Data Models

```python
@dataclass
class VerificationCmdConfig:
    """外部验证命令的完整配置"""
    cmd: str                             # 命令模板，支持 $SKILL_DIR 等变量
    timeout_seconds: int = 120           # 命令超时，防止 hang
    working_dir: Optional[str] = None    # 工作目录，默认为项目根目录
    env: dict[str, str] = field(default_factory=dict)  # 额外环境变量

@dataclass
class PhaseConfig:
    name: str
    # ---- 二选一：LLM 驱动 或 纯命令验证 ----
    # 模式 A: LLM 驱动（需要 instruction_ref）
    instruction_ref: Optional[str] = None   # 引用 md 文件（如 references/sop-bugfix.md）
    max_turns: int = 10
    max_tool_calls: int = 50
    timeout_seconds: int = 300
    turn_policy: str = "job"                # 映射到现有 TurnPolicy 预设
    model: Optional[str] = None             # Phase 级模型指定（如 "gpt-4o", "claude-sonnet"）
    #   None = 继承 agent 默认模型。允许按 Phase 角色分配不同模型：
    #   - research/plan: 用强推理模型（深度分析，调用次数少，成本可控）
    #   - implement: 用高性价比模型（机械执行，调用次数多）
    checkpoint: bool = False
    completion_signal: str = "llm_decision" # llm_decision | max_turns | timeout
    #   ↑ 三个退出条件（llm_decision / max_turns / timeout）同时生效，任一触发即退出。
    #     completion_signal 决定 Phase 的退出 status：
    #
    #     退出原因              | completion_signal 值 | Phase status
    #     LLM 主动结束          | llm_decision         | completed
    #     LLM 主动结束          | max_turns            | partial（LLM 没跑满，可能遗漏工作）
    #     达到 max_turns        | llm_decision         | exceeded（LLM 没来得及收尾）
    #     达到 max_turns        | max_turns            | completed
    #     达到 timeout          | timeout              | completed
    #     达到 timeout          | 其他                  | exceeded
    #
    #     status 影响：
    #     - completed → 正常产出 artifact，流转到下一 Phase
    #     - partial   → 产出 artifact + WARNING 日志，正常流转（保守起见不阻断）
    #     - exceeded  → 触发 on_failure 策略（abort / skip / retry）
    input_artifacts: list[str] = field(default_factory=list)
    allowed_tools: Optional[list[str]] = None   # None = 全部可用
    on_failure: str = "abort"               # abort | skip | retry
    #   - abort: Phase 失败 → workflow 失败
    #   - skip: Phase 失败 → 跳过，artifact 为空字符串。
    #     下游 Phase 的 input_artifacts 引用该 artifact 时获得空值 + WARNING 日志。
    #     适用于非关键 Phase（如可选的 research）。
    #   - retry: Phase 失败 → 重试，最多 max_retries 次
    max_retries: int = 1
    # 模式 B: 纯命令验证（框架直接执行，绕过 LLM）
    verification_cmd: Optional[VerificationCmdConfig] = None
    # 模式 A 的补充：LLM 驱动验证时的判定协议（见 5.3）
    verify_protocol: Optional[str] = None   # "structured_tag" | None
    #   ⚠️ verification_cmd 和 verify_protocol 互斥：
    #   同时配置两者时 YAML 校验报错（Phase 不能既走命令验证又走 LLM 验证）。

@dataclass
class PhaseGroupConfig:
    name: str
    action_phase: str                       # 循环重入时注入失败上下文的目标 phase
    verify_phase: str                       # 判定 pass/fail 的 phase
    setup_phase: Optional[str] = None       # 可选：首次进入 PhaseGroup 时执行一次的 phase
    #   典型用途：根据 plan 的验收标准编写测试骨架（验证基础设施准备）。
    #   setup_phase 只在首次进入和外循环回退重入时执行，不参与内循环。
    #   必须引用 phases 列表中存在的 phase name。
    max_iterations: int = 5
    phases: list[PhaseConfig] = field(default_factory=list)
    on_exhausted: str = "rollback"          # rollback | abort | checkpoint
    rollback_target: Optional[str] = None   # on_exhausted="rollback" 时必填
    #   显式指定回退到 TaskSession 顶层的哪个 Phase（如 "plan"）。
    #   不用 "retry_plan" 这种硬编码名称，因为回退目标不一定叫 plan。
    #   YAML 校验：on_exhausted="rollback" 时 rollback_target 不能为空。

@dataclass
class TaskSessionConfig:
    phases: list[Union[PhaseConfig, PhaseGroupConfig]]
    total_timeout_seconds: int = 1800
    total_max_tool_calls: int = 200
    max_recursive_depth: int = 2
    max_child_sessions: int = 5
    max_rollback_retries: int = 2               # 外循环回退次数上限（全局，不绑定特定 Phase 名称）
    # 子 TaskSession 全局安全阀（v1 预算独立，但仍需防失控）
    total_max_child_tool_calls: int = 400   # 所有子 session 工具调用累计上限
    #   v1 中子 session 预算独立于父级，但父级跟踪所有子 session 的累计
    #   工具调用。达到此上限时拒绝 spawn 新子 session，已运行的子 session
    #   不受影响（等其自然结束或自身预算耗尽）。
    #
    #   ⚠️ v1 限制：安全阀检查发生在 spawn 前，基于已完成子 session 的累计值。
    #   v1 串行执行不受影响，但 Future Work 的并行子 TaskSession 场景下，
    #   正在运行的子 session 消耗未计入，需引入实时共享计数器。

@dataclass
class TaskSessionState:
    session_id: str
    task_id: str
    depth: int = 0
    current_phase_index: int = 0
    rollback_retry_count: int = 0
    status: str = "pending"             # pending | running | paused | done | failed
    artifacts: dict[str, str] = field(default_factory=dict)
    total_tool_calls_used: int = 0
    child_session_count: int = 0
    total_child_tool_calls: int = 0     # 所有子 session 累计工具调用（安全阀）
```

### 5.2 TaskSession — 多阶段串行推进（含回退）

```python
class TaskSession:
    def __init__(self, config: TaskSessionConfig, state: TaskSessionState,
                 turn_orchestrator, agent_factory, session_manager): ...

    async def run(self) -> AsyncIterator[TaskSessionEvent]:
        self.state.status = "running"
        try:
            while self.state.current_phase_index < len(self.config.phases):
                step = self.config.phases[self.state.current_phase_index]

                # ---- Phase 边界检查点 ----
                if self.cancel_event.is_set():
                    self.state.status = "cancelled"
                    return

                if isinstance(step, PhaseGroupConfig):
                    try:
                        async for event in self._run_phase_group(step):
                            yield event
                    except PhaseGroupExhaustedError as e:
                        # 外循环回退
                        async for event in self._handle_exhausted(step, e):
                            yield event
                        # continue 跳过下方的 current_phase_index += 1，
                        # 因为 _handle_exhausted 已将 index 回退到 rollback_target
                        continue
                else:
                    async for event in self._run_phase(step):
                        yield event

                self.state.current_phase_index += 1

            self.state.status = "done"
        except BudgetExhaustedError:
            self.state.status = "failed"

    async def _handle_exhausted(self, group, error):
        """PhaseGroup 耗尽时的外循环回退"""
        if group.on_exhausted == "rollback":
            self.state.rollback_retry_count += 1
            if self.state.rollback_retry_count > self.config.max_rollback_retries:
                # 回退也耗尽，checkpoint 暂停
                yield TaskSessionEvent("checkpoint",
                    f"回退到 {group.rollback_target} 已达 {self.config.max_rollback_retries} 次上限，请人工介入")
                await self._wait_for_resume()
            else:
                # 回退到 rollback_target phase，注入失败上下文
                target_index = self._find_phase_index(group.rollback_target)
                self.state.current_phase_index = target_index
                self.state.artifacts["__retry_context"] = error.failure_summary
        elif group.on_exhausted == "checkpoint":
            yield TaskSessionEvent("checkpoint", "implement⟷verify 循环耗尽")
            await self._wait_for_resume()
        else:  # abort
            raise
```

### 5.3 PhaseGroup — implement⟷verify 循环

#### 验证判定协议

PhaseGroup 通过显式声明的 `verify_phase` 确定判定阶段，支持两种验证模式：

| 模式 | 配置 | pass/fail 判定 | 说明 |
|------|------|---------------|------|
| **外部验证** | `verification_cmd` | exit code 0 = pass, 非 0 = fail | LLM 不参与，无法"放水"，**优先推荐** |
| **LLM 结构化标签** | `verify_protocol: "structured_tag"` | LLM 输出包含 `<verify_result>PASS</verify_result>` 或 `<verify_result>FAIL: 原因</verify_result>` | 适用于无法自动化验证的场景 |

**LLM 结构化标签协议**：

当 `verify_protocol: "structured_tag"` 时，框架在 verify phase 的 system prompt 末尾追加：

```
在验证完成后，你必须输出验证结论标签：
- 通过：<verify_result>PASS</verify_result>
- 失败：<verify_result>FAIL: 具体失败原因</verify_result>
```

框架从 verify phase 的最终 LLM 输出中提取 `<verify_result>` 标签：
- 提取到 `PASS` → 通过
- 提取到 `FAIL: ...` → 失败，`...` 部分作为失败上下文
- **未提取到标签** → 视为失败（保守策略），失败上下文为 "verify phase 未产出结论标签"

这避免了依赖模糊的语义判断（如分析 LLM 是否"听起来觉得通过了"），判定逻辑是确定性的字符串匹配。

#### setup_phase — 验证基础设施准备

**问题**：`verification_cmd` 依赖可执行的测试，但新 feature 开发时测试还不存在。如果让 implement 阶段同时写代码和测试，存在**同源风险**——代码和测试可能犯同一个错（LLM 对需求的误解同时体现在实现和测试中）。

**方案**：PhaseGroup 支持可选的 `setup_phase`，在首次进入循环前执行一次，专门根据 plan 的验收标准编写测试骨架。

```
验证标准的来源链：

用户需求 → plan（经 checkpoint 人工确认）
  → setup_phase: write_tests（LLM 角色="验收者"，只看 plan，写测试骨架）
  → action_phase: implement（LLM 角色="开发者"，写实现代码让测试通过）
  → verify_phase: verify（框架跑 pytest，纯机械判定）
```

**认知分离**：write_tests 和 implement 在不同的 Phase 上下文中执行——write_tests 只看 plan artifact（不知道实现细节），implement 只看 plan + 失败上下文（不修改测试）。虽然都是 LLM 写的，但角色分离降低了同源风险。

**执行时机**：
- 首次进入 PhaseGroup → 执行 setup_phase
- 内循环（implement⟷verify 重试）→ **不执行** setup_phase（测试骨架不变，只改实现）
- 外循环回退重入 → **重新执行** setup_phase（新 plan 的验收标准可能不同，需要新测试）

**三种验证策略的推荐场景**：

| 策略 | 配置 | 适用场景 | 可靠性 |
|------|------|---------|--------|
| 已有测试 | `verification_cmd` 直接跑 pytest，无 setup_phase | Bugfix、重构（项目已有测试覆盖） | 最高 |
| setup_phase 写测试 | `setup_phase` + `verification_cmd` | Feature-dev（plan 定义验收标准，checkpoint 确认） | 高（认知分离） |
| implement 自写测试 | `verification_cmd` 跑 pytest，无 setup_phase | 快速原型（无 checkpoint，可接受同源风险） | 中 |
| LLM 自判 | `verify_protocol: "structured_tag"` | 无法自动化验证的场景（如文档质量） | 低 |

> Plan phase 的 SOP 应要求 LLM 在 artifact 中明确**验证策略**：依赖已有测试（列出测试文件）/ 需要新写测试（列出测试用例）/ 仅依赖外部工具信号。这迫使 LLM 在 plan 阶段就思考"怎么证明做完了"，而非留到 implement 阶段再说。

#### 两层验证模式（单元 + 集成）

当任务拆分为多个子任务时，需要**两层验证**：每个子任务通过自己的单元测试，所有子任务完成后还需要通过整体的集成测试。

当前设计天然支持这种模式，两层验证分属不同的 PhaseGroup：

```
两层验证的执行结构：

顶层 TaskSession
  plan → 拆子任务 + 定义集成验收标准

  PhaseGroup (顶层，verify = 集成测试):
    setup_phase: write_integration_tests (根据 plan 写集成测试)
    implement:
      → spawn 子 TaskSession(子任务 1)
          PhaseGroup (子级，verify = 单元测试):
            setup_phase: write_unit_tests
            implement⟷verify (单元测试)  ← 第一层验证
      → spawn 子 TaskSession(子任务 2)
          ...
      → spawn 子 TaskSession(子任务 N)
          ...
    integration_verify: pytest tests/integration/  ← 第二层验证
```

| 层级 | verify 载体 | 验证范围 | 失败时 |
|------|------------|---------|--------|
| 子任务 | 子 TaskSession 内部的 PhaseGroup | 单元测试（该子任务的正确性） | 子 TaskSession 内部 implement⟷verify 循环修复 |
| 整体 | 顶层 PhaseGroup 的 verify_phase | 集成测试（子任务间的兼容性） | 顶层 implement⟷verify 循环修复 |

**集成失败时的修复路径**：

集成测试失败后，失败上下文（如 "注册模块的 session 格式和登录模块不一致"）注入下一轮顶层 implement。此时 LLM 应**直接修代码**而非重新 spawn 所有子 TaskSession——子任务各自的单元测试已通过，问题出在接口兼容性。

这依赖 LLM 根据失败上下文做出正确判断（直接修 vs 重新 spawn），当前设计不在框架层强制区分"首次实现"和"修复迭代"。实践中，失败上下文的注入（"集成测试失败：session 格式不一致"）足以引导 LLM 做局部修复而非全量重做。

> 完整的两层验证 example 见 [§10. Example: Feature-Dev Workflow](#10-example-feature-dev-workflow)。

#### verification_cmd 执行规则

```python
async def _run_verification_cmd(self, cmd_config: VerificationCmdConfig) -> CmdResult:
    """框架直接执行验证命令，不经过 LLM"""
    env = {
        **os.environ,
        "SKILL_DIR": self._skill_dir,           # SKILL 根目录绝对路径
        "PROJECT_DIR": self._project_dir,        # 项目根目录
        "WORKFLOW_SESSION_ID": self.state.session_id,
        **cmd_config.env,                        # YAML 中声明的额外变量
    }

    try:
        result = await asyncio.wait_for(
            run_subprocess(
                cmd_config.cmd,
                cwd=cmd_config.working_dir or self._project_dir,
                env=env,
            ),
            timeout=cmd_config.timeout_seconds,
        )
        return CmdResult(
            exit_code=result.returncode,
            output=_truncate(result.stdout + result.stderr, max_chars=4000),
        )
    except asyncio.TimeoutError:
        return CmdResult(
            exit_code=1,
            output=f"verification_cmd 超时 ({cmd_config.timeout_seconds}s): {cmd_config.cmd}",
        )
```

**关键规则**：
- `$SKILL_DIR`、`$PROJECT_DIR` 等变量在执行时展开，不在 YAML 加载时
- 命令超时 = verify 失败（非 workflow 失败），进入正常的内循环重试
- stdout + stderr 合并截断到 4000 字符，作为失败上下文注入下一轮 implement

#### 循环逻辑

```python
async def _run_phase_group(self, group: PhaseGroupConfig):
    # ---- 从 phases 配置容器中按声明名称查找，执行顺序由声明字段决定 ----
    action_config = self._find_phase_by_name(group.phases, group.action_phase)
    verify_config = self._find_phase_by_name(group.phases, group.verify_phase)
    failure_history: list[str] = []  # 每轮失败的单行摘要（用于 iteration 3+ 注入）

    # ---- setup_phase：首次进入 PhaseGroup 时执行一次 ----
    # 典型场景：根据 plan 的验收标准编写测试骨架，使后续 verify 有测试可跑。
    # 外循环回退重入时也会重新执行（新 plan → 新测试）。
    if group.setup_phase:
        setup_config = self._find_phase_by_name(group.phases, group.setup_phase)
        async for event in self._run_phase(setup_config):
            yield event

    # ---- 循环阶段：显式按 action → verify 声明顺序执行 ----
    # 不遍历 phases 列表——phases 仅作为配置容器，
    # 执行顺序完全由 action_phase / verify_phase 声明决定，
    # 消除 YAML 中 phases 书写顺序导致的歧义。

    for iteration in range(1, group.max_iterations + 1):

        # ---- Step 1: action_phase ----
        retry_context = None
        if iteration > 1:
            retry_context = self.state.artifacts.get(f"{group.name}__last_failure")

        # 上下文管理：决定是否保留前一轮对话历史
        # - iteration 1: 全新对话（Phase 边界清空）
        # - iteration 2: 保留 iteration 1 的对话历史（LLM 能看到自己上轮写了什么），
        #   但前提是 iteration 1 的对话历史不超过 INHERIT_TOKEN_LIMIT（默认 32000 tokens）。
        #   如果 iteration 1 有大量工具调用导致历史过长，直接走 clean 模式。
        # - iteration 3+: 清空历史，重新注入 instruction + artifacts + 失败上下文
        #   （防止 token 膨胀，见 7.3 PhaseGroup 上下文膨胀防护）
        INHERIT_TOKEN_LIMIT = 32000
        can_inherit = (iteration == 2
                       and self._estimate_history_tokens() < INHERIT_TOKEN_LIMIT)
        context_mode = "inherit" if can_inherit else "clean"

        async for event in self._run_phase(
            action_config,
            retry_context=retry_context,
            context_mode=context_mode,
            failure_history=failure_history if iteration > 2 else None,
        ):
            yield event

        # ---- Step 2: verify_phase ----
        if verify_config.verification_cmd:
            # 纯命令模式：框架直接执行，LLM 不参与
            result = await self._run_verification_cmd(verify_config.verification_cmd)
            self.state.artifacts[verify_config.name] = result.output
            passed = (result.exit_code == 0)
        else:
            # LLM 驱动模式
            async for event in self._run_phase(verify_config, context_mode="clean"):
                yield event
            passed = _extract_verify_result(
                self.state.artifacts[verify_config.name],
                verify_config.verify_protocol,
            )

        if passed:
            return  # 循环成功退出

        # 记录失败上下文供下一轮 action_phase 使用
        last_failure = self.state.artifacts[verify_config.name]
        self.state.artifacts[f"{group.name}__last_failure"] = last_failure
        failure_history.append(
            f"iteration {iteration}: {_summarize_one_line(last_failure)}"
        )

    # 循环耗尽，抛出异常触发外循环回退
    raise PhaseGroupExhaustedError(
        group=group.name,
        iterations=group.max_iterations,
        failure_summary=self.state.artifacts[f"{group.name}__last_failure"],
        failure_history=failure_history,  # 完整历史，供外循环回退注入
    )


def _extract_verify_result(artifact: str, protocol: Optional[str]) -> bool:
    """从 verify phase 的 artifact 中提取 pass/fail 判定"""
    if protocol == "structured_tag":
        # 提取 <verify_result>...</verify_result> 标签
        match = re.search(r"<verify_result>(.*?)</verify_result>", artifact, re.DOTALL)
        if match is None:
            return False  # 保守策略：未找到标签 = 失败
        return match.group(1).strip().upper().startswith("PASS")
    else:
        # 无协议 = 无法判定 = 始终失败（强制配置 verification_cmd 或 verify_protocol）
        raise ConfigError(
            f"verify phase 必须配置 verification_cmd 或 verify_protocol，"
            f"否则无法判定 pass/fail"
        )
```

### 5.4 子 TaskSession — 串行递归

LLM 在 implement 阶段调用 `spawn_task_session(description, workflow)` 生成子 TaskSession。v1 中子 TaskSession **串行执行**，共享父级工作目录。

```python
# 框架工具：spawn_task_session
async def handle_spawn_task_session(
    self,
    description: str,
    workflow: str,
    context: dict[str, str] | None = None,  # 父级传递给子 session 的初始 artifacts
):
    # 校验递归深度
    if self.state.depth >= self.config.max_recursive_depth:
        return "递归深度已达上限，请直接在当前上下文中完成"

    if self.state.child_session_count >= self.config.max_child_sessions:
        return "子会话数量已达上限"

    # 全局安全阀：所有子 session 累计工具调用不能超限
    if self.state.total_child_tool_calls >= self.config.total_max_child_tool_calls:
        return f"子会话累计工具调用已达上限 ({self.config.total_max_child_tool_calls})"

    # 加载子 workflow 配置
    child_config = self._load_workflow(workflow)
    child_state = TaskSessionState(
        session_id=create_workflow_session_id(...),
        task_id=self.state.task_id,
        depth=self.state.depth + 1,
    )

    # 父级 context 注入子 session 的 artifacts 命名空间，
    # 使子 workflow 的 input_artifacts 引用（如 input_artifacts: [plan]）
    # 能从 artifacts["plan"] 取到父级传递的内容。
    if context:
        child_state.artifacts.update(context)

    # 串行执行子 TaskSession
    child = TaskSession(child_config, child_state, ...)
    async for event in child.run():
        yield event

    self.state.child_session_count += 1
    self.state.total_child_tool_calls += child.state.total_tool_calls_used
    return child.state.artifacts.get("summary", "子任务完成")
```

LLM 调用示例：

```python
spawn_task_session(
    description="实现用户注册模块：User 模型加 password_hash，POST /api/auth/register",
    workflow="implement-verify",
    context={
        # 从父级 plan artifact 中提取该子任务的相关部分
        "plan": "子任务 1：用户注册\n- User 模型增加 password_hash 字段...",
    }
)
```

子 TaskSession 通常只需 implement⟷verify 循环（不需要 research/plan），通过引用轻量 workflow 配置实现。`context` 参数将父级信息注入子 session 的 artifact 命名空间，使子 workflow 的 `input_artifacts` 声明能正常解析。

> **预算会计**（v1 简化实现）：v1 中子 TaskSession 的每轮预算独立于父级，但父级通过 `total_max_child_tool_calls` 设置所有子 session 的**累计工具调用上限**作为全局安全阀。精细的预算会计（子级上限取 min(自身预算, 父级剩余)）待 v2 设计。

### 5.5 Event Flow

TaskSession 复用已有的三条事件路径，不引入新的事件基础设施：

```
TaskSession 关键节点:
  ├── inject_history_message()   → 主 session 对话历史（LLM 下次对话可见）
  ├── deposit_mailbox_event()    → mailbox 缓冲（用户重连时 drain）
  └── events.emit()             → 全局事件总线（实时推送 Web/Telegram）
```

**事件分级**：

| 事件 | deliver | requires_action | 说明 |
|------|---------|-----------------|------|
| Checkpoint 等待审批 | `True` | `True` | 必须通知，等用户回复 |
| Phase 完成 / Verify pass/fail | `True` | `False` | 里程碑 |
| Workflow 完成/失败 | `True` | `False` | 最终结果，三条路径全走 |
| 内部进度（iteration 计数等） | `False` | `False` | 仅日志 |

Channel 层需扩展 `source_type` 白名单以接收 `workflow` 和 `workflow_checkpoint` 事件。

### 5.6 Human Intervention

复用已有的 `cancel_event` + `agent.interrupt()` / `resume_with_input()` 机制：

| 干预 | 机制 | 生效时机 |
|------|------|---------|
| **Cancel** | `cancel_event` 穿透到所有 `run_turn()` 调用 | 当前 turn 内（秒级） |
| **Pause** | Phase 边界检查 agent 状态 | 当前 Phase 结束后 |
| **Inject** | 用户消息作为下一 Phase 的额外 context | 当前 Phase 结束后 |
| **Checkpoint** | `resume_session(approve/modify/reject)` | 立即（workflow 已暂停） |

Checkpoint 完整流程：plan Phase 完成 → 产出 artifact → 暂停 + 通知用户 → 用户 approve（继续）/ modify（重跑 plan）/ reject（终止）。

**并发 workflow 路由**：用户可能同时有多个活跃 workflow（如一个在 checkpoint 等待，另一个在运行）。resume 消息通过 `session_id` 路由到正确的 TaskSession。Channel 层在 checkpoint 通知中附带 `session_id`，用户回复时通过 reply 关联或显式指定 session_id 来定位目标 workflow。v1 中每个 agent 同一时间最多一个活跃 workflow，避免路由歧义。

### 5.7 State Persistence — 崩溃可恢复

#### 问题

TaskSessionState（含 artifacts、rollback_retry_count、子任务状态）如果只存在内存，进程崩溃后无法恢复——plan 产出的任务拆分、已完成的子任务、失败上下文全部丢失，只能从头来。

#### 最小持久化（Phase 1 必须）

Phase 1 虽然不实现完整的崩溃恢复，但 **checkpoint 暂停**要求最小持久化——Telegram 等异步 channel 中用户可能几小时后才回复，期间进程可能重启。

**Phase 1 持久化范围**：仅在 checkpoint 暂停时写入 state.json（status=paused），进程重启后可恢复到暂停点等待用户回复。非 checkpoint 状态下进程崩溃 = workflow 失败，需要用户重新触发。

**Phase 1 checkpoint 超时**：`checkpoint_timeout_seconds`（默认 86400 = 24 小时），超时自动标记 workflow 为 `timeout_expired`，并生成 report（含"已修改但未验证的文件"列表，通过 `git diff --name-only` 检测）。用户可据此手动处理残留的代码修改。

超时后的 workflow 保留 state.json（status=timeout_expired），支持手动恢复：
```bash
alfred workflow resume <session_id> --force
```
`--force` 表示忽略超时状态强制恢复。这适用于 Telegram 等异步场景下用户可能较长时间后才回复的情况。无 `--force` 时对 timeout_expired 状态的 resume 会提示并拒绝。

#### 完整持久化（Phase 2）

**存储路径**：

```
.alfred/sessions/{session_id}/
  ├── state.json                    # TaskSessionState 全量快照
  ├── artifacts/
  │   ├── research.md               # research 阶段产出
  │   ├── plan.md                   # plan 阶段产出（含任务拆分列表）
  │   └── implement_verify__last_failure.md
  └── children/
      ├── {child_session_id_1}/     # 子 TaskSession（同样结构）
      └── {child_session_id_2}/
```

**持久化时机**（Phase 边界写入，不在 Phase 内部写）：

| 时机 | 写入内容 |
|------|---------|
| Phase 完成 | state.json + 该 Phase 的 artifact 文件 |
| PhaseGroup iteration 边界 | state.json + 失败上下文 |
| 外循环回退 | state.json（更新 rollback_retry_count、current_phase_index） |
| Checkpoint 暂停 | state.json（status=paused） |
| Workflow 结束（done/failed） | state.json（最终状态） |

**state.json 示例**：

```json
{
  "session_id": "workflow_coder__bugfix__20260302_abc123",
  "task_id": "task_456",
  "depth": 0,
  "current_phase_index": 2,
  "rollback_retry_count": 1,
  "status": "running",
  "total_tool_calls_used": 87,
  "child_session_count": 0,
  "artifact_refs": {
    "research": "artifacts/research.md",
    "plan": "artifacts/plan.md",
    "implement_verify__last_failure": "artifacts/implement_verify__last_failure.md"
  }
}
```

**关键设计**：artifacts 存为独立 md 文件而非 JSON 内嵌字符串。原因：
1. plan artifact 可能很长（含任务列表、验收标准、依赖关系），独立文件方便人工查看和调试
2. 失败上下文包含测试输出等多行文本，md 格式天然适合
3. 恢复时按需加载，不需要一次读全部 artifact

**恢复逻辑**：

```python
class TaskSession:
    @classmethod
    async def resume(cls, session_id: str, ...) -> "TaskSession":
        """从最近的 Phase 边界恢复执行"""
        state = load_state(f".alfred/sessions/{session_id}/state.json")
        # 从 current_phase_index 继续（而非从头开始）
        # artifacts 按需从文件加载
        session = cls(config, state, ...)
        return session
```

进程启动时扫描 `.alfred/sessions/` 下 status 为 `running` 或 `paused` 的 session，提示用户是否恢复。

---

## 6. Workflow Configuration & SKILL Integration

### 6.1 目录结构

现有 SKILL 结构 + 新增 workflows 目录：

```
skills/<skill-name>/
  ├── SKILL.md                        # 领域知识 + 意图路由表
  ├── scripts/                        # 工具脚本（已有）
  ├── references/                     # SOP 指令文档（已有）
  │   ├── sop-quick-queries.md
  │   ├── sop-bugfix-workflow.md
  │   ├── sop-feature-dev.md
  │   └── sop-write-tests.md         # [NEW] setup_phase 用，根据 plan 写测试骨架
  └── workflows/                      # [NEW] 纯 YAML
      ├── feature-dev.yaml            # 含 setup_phase（write_tests）
      ├── bugfix.yaml                 # 无 setup_phase（依赖已有测试）
      └── implement-verify.yaml       # 轻量 workflow，供子 TaskSession
```

### 6.2 Workflow YAML 示例

```yaml
# workflows/bugfix.yaml
name: bugfix
description: Bug 修复

phases:
  - name: research
    instruction_ref: references/sop-bugfix-workflow.md
    max_turns: 5
    max_tool_calls: 30
    allowed_tools: [_bash, _read_file, _read_folder]

  - name: plan
    instruction_ref: references/sop-bugfix-workflow.md
    max_turns: 3
    checkpoint: true
    allowed_tools: [_read_file, _read_folder]
    input_artifacts: [research]

  - group: implement_verify
    action_phase: implement
    verify_phase: verify
    max_iterations: 5
    on_exhausted: rollback
    rollback_target: plan               # 显式声明回退到哪个 Phase
    phases:
      - name: implement
        instruction_ref: references/sop-bugfix-workflow.md
        max_turns: 15
        max_tool_calls: 60
        allowed_tools: [_bash, _python, _read_file, _read_folder]
        input_artifacts: [plan]
      - name: verify
        # 纯命令模式：无需 instruction_ref/max_turns 等 LLM 字段
        verification_cmd:
          cmd: "python $SKILL_DIR/scripts/dispatch.py test"
          timeout_seconds: 120

total_timeout_seconds: 900
total_max_tool_calls: 150
max_rollback_retries: 2
```

```yaml
# workflows/feature-dev.yaml — 两层验证 workflow
# 顶层 verify 跑集成测试，子 TaskSession 内部 verify 跑单元测试
name: feature-dev
description: 新功能开发（两层验证：单元 + 集成）

phases:
  - name: research
    instruction_ref: references/sop-feature-dev.md
    max_turns: 5
    max_tool_calls: 30
    allowed_tools: [_bash, _read_file, _read_folder]

  - name: plan
    instruction_ref: references/sop-feature-dev.md
    max_turns: 3
    checkpoint: true                    # 人工确认任务拆分 + 集成验收标准
    allowed_tools: [_read_file, _read_folder]
    input_artifacts: [research]

  - group: implement_verify
    action_phase: implement
    verify_phase: integration_verify
    setup_phase: write_integration_tests  # 根据 plan 的集成验收标准写集成测试
    max_iterations: 5
    on_exhausted: rollback
    rollback_target: plan
    phases:
      - name: write_integration_tests
        instruction_ref: references/sop-write-tests.md
        max_turns: 5
        max_tool_calls: 30
        allowed_tools: [_bash, _python, _read_file, _read_folder]
        input_artifacts: [plan]         # 只看 plan（集成验收标准）
      - name: implement
        instruction_ref: references/sop-feature-dev.md
        max_turns: 15
        max_tool_calls: 60
        allowed_tools: [_bash, _python, _read_file, _read_folder]
        input_artifacts: [plan]
        # LLM 在此阶段 spawn 子 TaskSession（各自有单元测试 verify）
      - name: integration_verify
        # 纯命令模式：跑 setup_phase 生成的集成测试
        verification_cmd:
          cmd: "pytest tests/integration/ -x --tb=short"
          timeout_seconds: 180

total_timeout_seconds: 1200
total_max_tool_calls: 200
max_rollback_retries: 2
```

```yaml
# workflows/implement-verify.yaml — 轻量 workflow，供子 TaskSession
name: implement-verify
description: 实现+验证循环（子任务用，含可选的测试编写）

phases:
  - group: implement_verify
    action_phase: implement
    verify_phase: verify
    setup_phase: write_unit_tests       # 可选：为子任务写单元测试
    max_iterations: 3
    on_exhausted: abort
    phases:
      - name: write_unit_tests
        instruction_ref: references/sop-write-tests.md
        max_turns: 3
        max_tool_calls: 20
        allowed_tools: [_bash, _python, _read_file, _read_folder]
        # input_artifacts 由 spawn 时的 context 注入（父级 plan 的子任务描述）
      - name: implement
        instruction_ref: references/sop-feature-dev.md
        max_turns: 10
        max_tool_calls: 40
        allowed_tools: [_bash, _python, _read_file, _read_folder]
      - name: verify
        # 纯命令模式：跑该子任务的单元测试
        verification_cmd:
          cmd: "pytest tests/unit/ -x --tb=short -k $TEST_PATTERN"
          timeout_seconds: 120

total_timeout_seconds: 600
total_max_tool_calls: 80
```

### 6.3 SKILL 触发

SKILL 通过两层机制决定执行路径：

#### 层 1：操作目录（Operation Catalog）

SKILL.md 不再按 SOP 分类做意图路由，而是列出所有可用操作，按是否需要 workspace lock 分两类：

| 类型 | 命令 | 说明 | 典型场景 |
|------|------|------|---------|
| 只读（无锁） | `quick-status --repos` | git 状态 | "看下状态" |
| 只读（无锁） | `quick-test --repos` | 跑测试 | "跑下测试" |
| 只读（无锁） | `quick-find --repos` | 搜索代码 | "找下这个函数" |
| 只读（无锁） | `analyze --repos` | 引擎深度分析 | "review 下代码" |
| 写入（需锁） | `workspace-check` | 获取工作区锁 | 所有写操作前置 |
| 写入（需锁） | `develop` | 引擎写代码 | "修一下这个 bug" |
| 写入（需锁） | `test` | 工作区内跑测试 | 开发后验证 |
| 写入（需锁） | `submit-pr` | 提交 PR | "提交 pr" |
| 写入（需锁） | `release` | 释放锁 | 写操作结束后必须 |

LLM 根据用户意图自主选择需要的操作并组合执行，不强制走完某个预定义流程。

#### 层 2：复杂度判断 — 单轮 vs Workflow

LLM 根据上下文判断复杂度：

- **单步/简单操作** → 直接执行对应命令（不需要 SOP 或 workflow）
  - 例："提交 pr" → `workspace-check` + `submit-pr` + `release`
  - 例："跑下测试" → `quick-test --repos`
  - 例："看看有什么问题" → `analyze --repos`

- **多步复杂任务** → 启动 workflow
  - 例："修一下登录 500 错误" → `start_workflow("bugfix", ctx)`
  - 例："加个用户认证模块" → `start_workflow("feature-dev", ctx)`

判断标准（在 SKILL.md 中以自然语言告知 LLM）：
  - 需要 research + plan + implement + verify 闭环 → workflow
  - 预计超过 20 次工具调用 → workflow
  - 涉及多文件修改且需要测试验证 → workflow
  - 其余 → 单轮直接执行

#### SOP 文件的角色变化

SOP 从"必须加载的执行流程"变为"可选参考文档"：
- workflow 的 `instruction_ref` 引用 SOP 作为 Phase 指令（不变）
- 单轮执行时，LLM 可选择加载 SOP 作为参考，但不强制
- SOP 文件内容不变，只是使用方式从"强制 → 可选"

#### SKILL.md 示例

```markdown
## Available Operations

### 只读操作（无需 workspace lock）
| 命令 | 说明 |
|------|------|
| `quick-status --repos` | 查看 git 状态 |
| `quick-test --repos` | 快速跑测试 |
| `quick-find --repos` | 搜索代码 |
| `analyze --repos` | 深度分析/review |

### 写入操作（需要 workspace lock）
执行顺序：workspace-check → 写入操作 → release
| 命令 | 说明 |
|------|------|
| `workspace-check` | 获取工作区锁（写操作前必须） |
| `develop` | 引擎写代码 |
| `test` | 工作区内跑测试 |
| `submit-pr` | 提交 PR |
| `release` | 释放工作区锁（写操作后必须） |

### 复杂度判断：直接执行 vs 启动 Workflow
根据用户意图判断复杂度，选择执行方式：
- **直接组合操作**：单步操作、明确指令、不需要 research-plan-implement-verify 闭环
- **启动 workflow**（`start_workflow("bugfix"|"feature-dev", ctx)`）：
  需要多文件修改 + 测试验证、预计超过 20 次工具调用、需要完整闭环

### 参考文档（可选加载）
| 文档 | 用途 |
|------|------|
| `references/sop-bugfix.md` | bugfix 流程参考 |
| `references/sop-feature-dev.md` | feature 开发流程参考 |
```

### 6.4 与现有执行路径的兼容

| 现有路径 | 变化 |
|---------|------|
| Chat (CHAT_POLICY, 20 calls, 600s) | **不变** |
| Heartbeat inline (HEARTBEAT_POLICY, 10 calls, 120s) | **不变** |
| Isolated job (JOB_POLICY, 20 calls, 600s) | **不变** |
| **Workflow** (NEW) | 多阶段执行，通过 `start_workflow` 触发 |

---

## 7. Error Recovery & Context Management

### 7.1 三级错误恢复

```
Level 1: Turn 级（已有，TurnOrchestrator 处理）
  └── 工具调用失败 → 错误回传给 LLM → LLM 自行修正

Level 2: PhaseGroup 级（内循环）
  └── verify 失败 → 注入失败上下文 → 回到 implement → 达到 max_iterations → 触发 Level 3

Level 3: Phase 级（外循环）
  └── PhaseGroup 耗尽 → 回退到 plan（注入失败摘要）→ plan 回退耗尽 → checkpoint 暂停等人工介入
```

### 7.2 失败上下文注入（Error as Prompt）

**核心原则**：无差别重启几乎一定犯同样的错。重试时必须注入先验失败信息。

**内循环注入**：PhaseGroup 循环重入时，框架自动在 action_phase 阶段 prompt 前注入上次 verify 的失败输出（测试报错、linter 输出等）。

**外循环注入**：回退到 plan 时，框架注入所有 implement⟷verify 迭代的失败摘要，使 LLM 制定新方案时知道哪些路径已尝试过。

```markdown
<!-- 框架注入的回退上下文 -->
## 前次方案失败摘要

方案：在 session_handler.py 加 mutex lock
尝试 5 轮 implement⟷verify，均未通过。最后一次 verify 输出：

```
FAILED test_concurrent_login - deadlock detected after 30s timeout
```

请制定新方案，避免锁机制。
```

### 7.3 Context Management

#### Artifact 注入方式

Phase 的 `input_artifacts` 声明其依赖的前序 artifact。框架在 Phase 启动时，将 artifact 内容作为 **user message** 注入到 LLM 对话的开头（在 instruction 之后、用户任务描述之前），格式：

```markdown
<!-- 以下是前序阶段的产出，供本阶段参考 -->

## research 阶段产出

{research artifact 内容}

## plan 阶段产出

{plan artifact 内容}

---
```

**Artifact 长度控制**：单个 artifact 注入上限 **4000 tokens**（约 3000 字中文）。超限时框架自动截断并附加提示 `[... 已截断，完整内容见 .alfred/sessions/{id}/artifacts/{name}.md]`。LLM 可通过 `_read_file` 工具读取完整版本。

#### Phase 边界上下文压缩

| 边界 | 策略 | 实现 | 理由 |
|------|------|------|------|
| Phase 结束 | 丢弃对话历史，保留 artifact | 下一 Phase 从空对话开始，artifact 通过 input_artifacts 注入 | Phase 间通过 artifact 传递信息，不需要原始对话 |
| PhaseGroup 内循环 | 三阶段策略（见下文详述） | iteration 1: 全新对话；iteration 2: 条件延续 iteration 1 的对话（`context_mode=inherit`，需历史 token 量 < 阈值）；iteration 3+: 清空历史重建（`context_mode=clean`）+ 注入 `failure_history` 摘要 | 平衡 LLM 对已写代码的感知与 token 预算 |
| 子 TaskSession | 独立上下文 | 不污染父级 token 空间，完成后返回 Markdown 摘要 | 子任务自包含 |
| Phase 内 | 复用现有 history compaction 机制 | 单轮内已有的压缩策略 | 不引入新机制 |

**PhaseGroup 上下文膨胀防护**：

5 轮 implement⟷verify 循环中，每轮的工具调用输出可能累积大量 token。防护策略通过 `_run_phase` 的 `context_mode` 参数实现（见 5.3 循环逻辑代码）：

1. **iteration 1**：全新对话（`context_mode=clean`），Phase 边界标准行为
2. **iteration 2**：条件延续 iteration 1 的对话历史（`context_mode=inherit`）——LLM 能看到自己上轮写了什么代码和工具调用，避免重复劳动。**前提**：iteration 1 的对话历史不超过 `INHERIT_TOKEN_LIMIT`（默认 32000 tokens）；如果 iteration 1 工具调用密集导致历史过长，直接退化为 `clean` 模式
3. **iteration 3+**：清空历史对话（`context_mode=clean`），重新注入：
   - action_phase 的 instruction + input_artifacts
   - `__last_failure` 失败上下文（最近一轮的 verify 完整输出）
   - `failure_history`（前几轮的单行失败摘要，如 "iteration 1: test_login FAILED - assertion error on line 42"）

这保证即使循环 5 轮，action_phase 看到的上下文量级稳定在 **instruction + artifact + 1 轮完整失败输出 + N 行历史摘要**。

> **为什么 iteration 2 保留而 3+ 清空**：iteration 1→2 是最常见的修复路径（首次实现有小 bug，看着上轮代码微调即可）。如果 2 轮都没修好，说明问题不简单，此时保留 2 轮的完整对话反而是噪音——不如让 LLM 从干净上下文出发，带着失败摘要重新思考。
>
> **为什么 iteration 2 有 token 阈值**：如果 iteration 1 涉及大量文件读取和工具调用（如 60 次），完整对话历史可能接近模型上下文上限。在这种情况下保留历史不仅没有帮助，反而压缩了 LLM 的推理空间。阈值确保 inherit 只在历史量合理时生效。

---

## 8. Observability

### 8.1 结构化日志

TaskSession 全生命周期使用结构化日志，统一前缀 `workflow.`：

```python
# 日志示例
logger.info("workflow.phase.start", extra={
    "session_id": "workflow_coder__bugfix__20260302_abc123",
    "phase": "implement",
    "phase_index": 2,
    "iteration": 3,              # PhaseGroup 内的轮次
    "total_tool_calls_used": 87,
    "budget_remaining": 63,
})

logger.info("workflow.verify.result", extra={
    "session_id": "...",
    "phase": "verify",
    "iteration": 3,
    "passed": False,
    "verification_mode": "cmd",  # cmd | structured_tag
    "cmd_exit_code": 1,
    "cmd_duration_ms": 4523,
})

logger.info("workflow.rollback", extra={
    "session_id": "...",
    "from_phase": "implement_verify",
    "to_phase": "plan",
    "rollback_retry_count": 2,
    "reason": "phase_group_exhausted",
})
```

**日志级别约定**：

| 事件 | 级别 | 说明 |
|------|------|------|
| Phase/PhaseGroup 开始/结束 | INFO | 里程碑 |
| verify pass/fail | INFO | 循环关键判定 |
| 外循环回退 | WARNING | 非预期路径 |
| checkpoint 暂停 | WARNING | 需人工介入 |
| 预算耗尽 / workflow 失败 | ERROR | 终态异常 |
| iteration 内部进度 | DEBUG | 开发调试用 |

### 8.2 Metrics

通过已有的 metrics 基础设施（如有）或简单计数器暴露：

| Metric | 类型 | 说明 |
|--------|------|------|
| `workflow_total` | Counter | workflow 启动总数（按 workflow name 分组） |
| `workflow_completed` | Counter | 成功完成数 |
| `workflow_failed` | Counter | 失败数（按 reason 分组：budget_exhausted / plan_exhausted / cancelled） |
| `workflow_duration_seconds` | Histogram | 端到端耗时 |
| `workflow_tool_calls_total` | Histogram | 工具调用总数 |
| `workflow_verify_iterations` | Histogram | PhaseGroup 实际迭代次数 |
| `workflow_rollback_retries` | Histogram | 外循环回退次数 |

### 8.3 诊断接口

提供命令行诊断能力（Phase 1 即实现）：

```bash
# 查看活跃 workflow 状态
alfred workflow status

# 输出示例：
# SESSION                                    STATUS    PHASE           ITER  TOOLS  ELAPSED
# workflow_coder__bugfix__20260302_abc123     running   implement(3/5)  3     87     2m15s
# workflow_coder__feature__20260302_def456    paused    plan(checkpoint) -    42     5m30s

# 查看已完成 workflow 的 report
alfred workflow report <session_id>
```

### 8.4 Completion Report

Workflow 结束时（无论 done / failed / cancelled），框架自动生成结构化 report。Report 有两个用途：**用户可见的执行总结** + **事后诊断的完整记录**。

#### Report 结构

```python
@dataclass
class WorkflowReport:
    # ---- 基础信息 ----
    session_id: str
    workflow_name: str
    task_description: str           # 用户原始任务描述
    status: str                     # done | failed | cancelled
    started_at: datetime
    finished_at: datetime
    duration_seconds: float

    # ---- 资源消耗 ----
    total_tool_calls: int
    tool_calls_budget: int          # 预算上限（方便对比）
    total_llm_tokens: int           # input + output tokens 总和
    child_sessions_spawned: int
    total_child_tool_calls: int     # 子 session 累计工具调用

    # ---- Phase 执行轨迹 ----
    phase_trace: list[PhaseTraceEntry]

    # ---- 子任务摘要（有子 TaskSession 时） ----
    child_session_traces: list[ChildSessionTraceEntry] = field(default_factory=list)

    # ---- 最终产出 ----
    final_artifact: Optional[str]   # 最终 artifact 摘要（如有）
    files_modified: list[str]       # git diff 检测到的变更文件列表

    # ---- 失败信息（仅 failed 时） ----
    failure_reason: Optional[str]   # budget_exhausted | plan_exhausted | cancelled | error
    failure_detail: Optional[str]   # 最后一次 verify 输出 或 异常信息
    failure_suggestion: Optional[str] = None  # 失败时的改进建议
    #   来源：框架在 workflow 失败时，将失败上下文注入一次额外的 LLM 调用
    #   （system prompt: "根据以下失败历史，给出简要改进建议"），
    #   产出 1-3 句话的建议。若 LLM 调用失败则为 None（不阻断 report 生成）。


@dataclass
class PhaseTraceEntry:
    phase_name: str
    phase_type: str                 # "phase" | "phase_group"
    started_at: datetime
    finished_at: datetime
    duration_seconds: float
    tool_calls_used: int
    iterations: Optional[int]       # PhaseGroup 才有
    verify_results: list[VerifyTraceEntry]  # 每次 verify 的 pass/fail 记录
    rollback_triggered: bool        # 是否触发了外循环回退
    artifact_summary: Optional[str] # artifact 的前 200 字摘要


@dataclass
class VerifyTraceEntry:
    iteration: int
    passed: bool
    verification_mode: str          # "cmd" | "structured_tag"
    duration_ms: int
    output_summary: str             # 验证输出前 500 字符


@dataclass
class ChildSessionTraceEntry:
    """子 TaskSession 的执行摘要，嵌入父级 report"""
    session_id: str
    description: str                # spawn 时的任务描述
    workflow_name: str
    status: str                     # done | failed
    tool_calls_used: int
    verify_iterations: int          # PhaseGroup 实际迭代次数
    duration_seconds: float
```

#### 持久化

Report 在 workflow 结束时写入 `.alfred/sessions/{session_id}/report.md`（人可读）和 `report.json`（程序可读）：

```
.alfred/sessions/{session_id}/
  ├── state.json
  ├── report.json                   # 结构化 report（WorkflowReport 序列化）
  ├── report.md                     # 人可读 Markdown report
  ├── artifacts/
  │   └── ...
  └── children/
      └── ...
```

#### 用户可见的 Markdown Report

Workflow 结束时，框架生成 Markdown report 并通过三条事件路径送达用户：

```markdown
## Workflow Report: bugfix

**状态**: ✅ 完成
**耗时**: 2m15s
**工具调用**: 35 / 150

### 执行轨迹

| Phase | 耗时 | 工具调用 | 结果 |
|-------|------|---------|------|
| research | 28s | 8 | ✅ 完成 |
| plan | 12s | 3 | ✅ 完成 (checkpoint approved) |
| implement⟷verify | 1m35s | 24 | ✅ 通过 (2 轮) |

### verify 历史

| 轮次 | 结果 | 摘要 |
|------|------|------|
| 1 | ❌ | pytest: 17/21 passed, 4 failed (test_session_refresh, ...) |
| 2 | ✅ | pytest: 21/21 passed |

### 变更文件

- `src/auth/session_handler.py`
- `src/auth/auth_middleware.py`
- `tests/test_concurrent_login.py` (新增)

### 产出摘要

修复了 session 过期处理的竞态条件：token 刷新改用乐观锁，auth middleware 增加 401 重试。
新增并发登录测试覆盖该场景。
```

失败场景的 report：

```markdown
## Workflow Report: bugfix

**状态**: ❌ 失败 (plan 回退耗尽)
**耗时**: 8m42s
**工具调用**: 148 / 150

### 执行轨迹

| Phase | 耗时 | 工具调用 | 结果 |
|-------|------|---------|------|
| research | 30s | 8 | ✅ 完成 |
| plan (第 1 次) | 15s | 3 | ✅ → mutex lock 方案 |
| implement⟷verify (第 1 次) | 3m20s | 52 | ❌ 5 轮未通过 |
| plan (第 2 次, 回退) | 18s | 4 | ✅ → read-write lock 方案 |
| implement⟷verify (第 2 次) | 4m10s | 61 | ❌ 5 轮未通过 |
| → checkpoint 暂停 | - | - | ⏸️ 等待人工介入 |

### 失败分析

两次方案均未能解决并发死锁问题：
- 方案 1 (mutex lock): deadlock detected after 30s timeout
- 方案 2 (read-write lock): write starvation under high concurrency

### 建议

请检查是否可以通过应用层避免并发刷新（如 single-flight 模式），
或考虑切换到无锁的 token rotation 策略。
```

#### Report 生成时机

| 触发 | 动作 |
|------|------|
| `status = done` | 生成 report → 写文件 → 三条路径送达（history + mailbox + emit） |
| `status = failed` | 同上，report 含失败分析 |
| `status = cancelled` | 生成简化 report（已完成 phase 的轨迹 + 取消点） |
| checkpoint 暂停 | **不生成 report**（workflow 未结束），但暂停事件中包含当前进度摘要 |

#### Report 中的"变更文件"检测

框架在 workflow 开始时记录 `git rev-parse HEAD`，结束时执行 `git diff --name-only {start_commit} HEAD` 获取变更文件列表。如果不在 git 仓库中则跳过此字段。

---

## 9. Example: Bugfix Workflow

**场景**：用户说 "登录接口偶尔返回 500"

### 执行流程

```
research (1 turn, ~8 tool calls):
  LLM 读日志 → 读 session_handler.py → 读 auth_middleware.py
  → Artifact: "session 过期处理存在竞态，两个请求同时刷新 token 时后者覆盖前者"

plan (1 turn, ~3 tool calls, checkpoint):
  LLM 产出方案：
    1. 改 session_handler.py：token 刷新加乐观锁
    2. 改 auth_middleware.py：重试一次 on 401
    3. 验收标准：现有 20 个测试全过 + 并发测试不 flake
  → 用户 approve ✓

implement⟷verify:
  iteration 1:
    implement (6 tool calls): 改两个文件，写并发测试
    verify: pytest → 17/21 passed, 4 failed
      → 注入失败输出到下一轮 implement

  iteration 2:
    implement (4 tool calls): 修 edge case（token 过期时间边界）
    verify: pytest → 21/21 passed ✓

  → 循环成功退出

Workflow 完成:
  → inject_history_message(): 摘要注入主 session
  → deposit_mailbox_event(): 缓冲离线通知
  → events.emit(): 实时推送
```

### 资源消耗

```
Total: ~35 tool calls (预算 150)
Total time: ~2 分钟
```

### 如果方案错了会怎样

```
假设 plan 选了 mutex lock 方案，implement⟷verify 循环 5 次都死锁：

  plan (mutex lock 方案) → implement⟷verify × 5 全失败
    → on_exhausted: rollback (target: plan)
    → 回退到 plan，注入："mutex lock 方案导致死锁，5 轮未解决"

  plan (改用乐观锁方案) → implement⟷verify × 2 → passed ✓

如果第二次 plan 又失败：
  → rollback_retry_count = 2 = max_rollback_retries
  → checkpoint 暂停，通知用户："两次方案均失败，请人工介入"
```

---

## 10. Example: Feature-Dev Workflow

**场景**：用户说 "给系统加用户认证模块，支持注册、登录和权限校验"

这个 example 展示**两层验证模式**：子任务各自通过单元测试（第一层），全部完成后通过集成测试（第二层）。

### Workflow YAML

使用 `feature-dev.yaml`（见 6.2），关键配置：
- plan 阶段有 checkpoint（人工确认任务拆分和验收标准）
- 顶层 PhaseGroup 有 `setup_phase: write_integration_tests`（写集成测试）
- implement 阶段内 LLM spawn 子 TaskSession，每个子 TaskSession 使用 `implement-verify.yaml` 轻量 workflow

### 执行流程

```
research (2 turns, ~12 tool calls):
  LLM 读项目结构 → 读现有 auth 相关代码 → 读 ORM 模型 → 读 API 路由
  → Artifact:
    "项目使用 FastAPI + SQLAlchemy，无现有认证模块。
     已有 User 模型（仅 id/name/email），无 password 字段。
     API 路由在 src/api/routes/ 下按模块组织。
     已有 pytest 基础设施和 test DB fixture。"

plan (1 turn, ~3 tool calls, checkpoint):
  LLM 产出方案：
    任务拆分：
      1. 用户注册：User 模型加 password_hash，POST /api/auth/register
      2. 用户登录：JWT token 签发，POST /api/auth/login
      3. 权限校验：auth middleware，保护已有 /api/users 端点
    集成验收标准：
      - 注册 → 登录 → 拿 token 访问受保护端点 → 200
      - 无 token 访问受保护端点 → 401
      - 过期 token → 401
    验证策略：需要新写测试（单元 + 集成）
  → 用户 approve ✓（确认拆分合理、验收标准完整）

setup_phase: write_integration_tests (1 turn, ~5 tool calls):
  LLM 只看 plan artifact，写集成测试骨架：
    → tests/integration/test_auth_flow.py
      - test_register_login_access: 注册 → 登录 → 访问 → 200
      - test_no_token_rejected: 无 token → 401
      - test_expired_token_rejected: 过期 token → 401
  此时测试全部 FAIL（实现代码还没写），这是预期行为。

implement⟷verify (顶层 PhaseGroup):
  iteration 1:
    implement (3 tool calls + 3 个子 TaskSession):

      → spawn 子 TaskSession("用户注册", workflow="implement-verify")
          子 setup_phase: write_unit_tests (3 tool calls)
            → tests/unit/test_register.py (5 个用例)
          子 implement⟷verify:
            iteration 1:
              implement (8 tool calls): 改 User 模型 + 写 register endpoint
              verify: pytest tests/unit/test_register.py → 3/5 passed
            iteration 2:
              implement (3 tool calls): 修密码 hash 和重复 email 校验
              verify: pytest tests/unit/test_register.py → 5/5 passed ✓

      → spawn 子 TaskSession("用户登录", workflow="implement-verify")
          子 setup_phase: write_unit_tests (3 tool calls)
            → tests/unit/test_login.py (4 个用例)
          子 implement⟷verify:
            iteration 1:
              implement (6 tool calls): 写 login endpoint + JWT 签发
              verify: pytest tests/unit/test_login.py → 4/4 passed ✓

      → spawn 子 TaskSession("权限校验", workflow="implement-verify")
          子 setup_phase: write_unit_tests (3 tool calls)
            → tests/unit/test_permission.py (3 个用例)
          子 implement⟷verify:
            iteration 1:
              implement (5 tool calls): 写 auth middleware + 装饰已有路由
              verify: pytest tests/unit/test_permission.py → 3/3 passed ✓

    integration_verify: pytest tests/integration/ → FAILED
      test_register_login_access: FAILED
        "login endpoint 返回的 token 格式是 {"token": "xxx"}，
         但 auth middleware 期望 Header 格式 Bearer xxx"
      → 注入失败上下文到下一轮 implement

  iteration 2:
    implement (2 tool calls):
      LLM 看到集成失败原因 → 不需要重新 spawn 子 TaskSession
      直接修 auth middleware：兼容 {"token": "xxx"} 响应格式
    integration_verify: pytest tests/integration/ → 3/3 passed ✓

  → 循环成功退出

Workflow 完成:
  → 生成 report + 三条路径送达
```

### 两层验证的作用

```
如果没有集成测试（只靠子任务的单元测试）：
  三个子任务各自 ✓，但实际组合后 token 格式不一致 → 用户拿到的是半成品

有了集成测试：
  子任务各自 ✓ → 集成测试 ✗（token 格式问题）→ 自动修复 → 集成测试 ✓
  用户拿到的是验证过的完整功能
```

### 资源消耗

```
顶层:
  research:          ~12 tool calls
  plan:              ~3  tool calls
  write_integ_tests: ~5  tool calls
  implement iter 1:  ~3  tool calls (spawn 调度)
  implement iter 2:  ~2  tool calls (修兼容性)
  verify × 2:        ~0  tool calls (纯命令)
子任务:
  注册: setup ~3 + implement ~11 + verify ~0 = ~14
  登录: setup ~3 + implement ~6  + verify ~0 = ~9
  权限: setup ~3 + implement ~5  + verify ~0 = ~8

Total: ~56 tool calls (预算 200)
子任务累计: ~31 tool calls (安全阀 400)
Total time: ~4 分钟
```

### 如果集成方案有问题会怎样

```
假设 plan 的拆分有问题（权限校验应该在网关层而非应用层）：

  plan (应用层 middleware 方案) → implement⟷verify × 5
    子任务各自通过，但集成测试始终失败（middleware 无法拦截静态资源请求）
    → on_exhausted: rollback (target: plan)
    → 回退到 plan，注入：
      "应用层 middleware 方案无法覆盖静态资源路径。
       5 轮集成测试均失败：test_static_resource_protected FAILED"

  plan (改为网关层 auth 方案) → setup_phase 重新执行（新方案 → 新集成测试）
    → implement⟷verify × 2 → 集成测试通过 ✓

注意：外循环回退重入时 setup_phase 重新执行——
新 plan 的架构方案变了，验收标准和测试都需要重写。
```

### Completion Report

```markdown
## Workflow Report: feature-dev

**状态**: 完成
**耗时**: 4m12s
**工具调用**: 56 / 200 (子任务累计: 31)

### 执行轨迹

| Phase | 耗时 | 工具调用 | 结果 |
|-------|------|---------|------|
| research | 35s | 12 | 完成 |
| plan | 15s | 3 | 完成 (checkpoint approved) |
| write_integration_tests (setup) | 18s | 5 | 完成 |
| implement⟷verify | 3m04s | 36 | 通过 (2 轮) |

### 子任务

| 子任务 | 单元测试轮次 | 工具调用 | 结果 |
|--------|------------|---------|------|
| 用户注册 | 2 轮 | 14 | 通过 |
| 用户登录 | 1 轮 | 9 | 通过 |
| 权限校验 | 1 轮 | 8 | 通过 |

### 集成 verify 历史

| 轮次 | 结果 | 摘要 |
|------|------|------|
| 1 | FAIL | test_register_login_access: token 格式不一致 |
| 2 | PASS | 3/3 passed |

### 变更文件

- `src/models/user.py` (修改: 增加 password_hash 字段)
- `src/api/routes/auth.py` (新增)
- `src/middleware/auth.py` (新增)
- `src/api/routes/users.py` (修改: 增加 auth 装饰器)
- `tests/unit/test_register.py` (新增)
- `tests/unit/test_login.py` (新增)
- `tests/unit/test_permission.py` (新增)
- `tests/integration/test_auth_flow.py` (新增)
```

---

## 11. Migration Path

### Phase 1: 核心闭环

**目标**：TaskSession + Phase + PhaseGroup + verification_cmd + 外循环回退

- TaskSession.run()：Phase 串行推进 + PhaseGroup 循环
- PhaseGroup：implement⟷verify 内循环 + 耗尽回退 + setup_phase 支持
- PhaseGroup YAML 校验：action_phase / verify_phase / setup_phase 引用检查、verify 配置检查、Phase 模式互斥检查、verification_cmd/verify_protocol 互斥检查、rollback_target 引用检查
- verification_cmd：框架直接执行外部验证（含超时、环境变量）
- verify_protocol: "structured_tag"：LLM 驱动验证的判定协议
- allowed_tools 硬过滤（Phase 切换时动态注册/注销工具）
- Workflow YAML 加载 + `start_workflow` 框架工具
- cancel_event 穿透
- 基本事件传出（events.emit + inject_history_message）
- **最小持久化**：checkpoint 暂停时写 state.json，支持进程重启后恢复等待
- 结构化日志 + `alfred workflow status` 诊断命令
- **Completion Report**：workflow 结束时自动生成 Markdown report + report.json，通过事件路径送达用户
- Artifact 注入（input_artifacts → user message）+ 长度截断
- **PhaseGroup 上下文膨胀防护**：iteration 2 条件 inherit（token 阈值）、iteration 3+ 清空重建 + failure_history 摘要注入

**验收**：bugfix workflow 端到端跑通——research→plan→implement⟷verify 完整流程，verify 失败正确回到 implement，PhaseGroup 耗尽正确回退到 rollback_target。PhaseGroup iteration 2 在历史 token 量 < 阈值时保留上轮对话、超阈值或 iteration 3+ 清空重建。setup_phase 在首次进入和回退重入时正确执行、内循环不执行。checkpoint 暂停后进程重启可恢复等待状态（含 timeout_expired 的 --force resume）。workflow 结束后用户收到包含执行轨迹、verify 历史、变更文件的 report。

### Phase 2: 人机交互 + 完整持久化 + 递归

- Checkpoint approve/modify/reject 完整流程
- Pause + inject 指令
- **完整状态持久化**（`.alfred/sessions/`，Phase 边界写入）+ 崩溃恢复（`TaskSession.resume()`）
- 子 TaskSession 串行生成（spawn_task_session 工具）
- Workflow 结果走三条路径送达（history + mailbox + emit）
- Channel 层渲染 checkpoint 为交互式 UI
- Metrics 暴露

**验收**：feature-dev workflow 含 checkpoint + 子 TaskSession 端到端跑通；进程中途 kill 后可从最近 Phase 边界恢复；用户可 pause/cancel 运行中的 workflow。

### Phase 3: 持续优化

- Workflow 触发 hint rules（框架侧启发式建议）
- Phase 级模型配置（`model` 字段，按角色分配不同模型）
- 更多 SKILL 接入 workflow

**验收**：长任务（>100 tool calls）不因 token 膨胀而质量下降；不同 Phase 可使用不同模型。

---

## 12. Future Work

以下能力在 v1 不实现，待核心闭环验证后按需引入：

### 适用规模与扩展路径

**v1 适用规模**：单 feature / bugfix / 中等重构，约 50-300 次工具调用。不适用于"从零构建子系统"级别的巨大任务（如开发一个编译器）。

**巨大任务的差距**：需要层级式 plan（顶层 plan 只拆子系统，每个子系统自己 plan 组件）、增量进度保护（已完成子任务不受父级回退影响）、集成验证（子系统组合后的端到端测试）。

**扩展不需要大重构**。当前核心抽象（TaskSession 递归、Phase/PhaseGroup 模型、rollback_target）已具备扩展基础：

| 扩展需求 | 当前抽象 | 改动范围 |
|---------|---------|---------|
| 更深的递归分解 | TaskSession 已是递归单元 | 配置调整（`max_recursive_depth` / `max_child_sessions`） |
| 子系统各自有完整 plan | 子 TaskSession 已支持完整 phase 流程 | 零改动 |
| 已完成子任务不受父级回退影响 | 子 TaskSession 已有独立状态 | 局部改动：`_handle_exhausted` 增加**选择性回退**（只重新规划失败的子任务，跳过已完成的） |
| 集成验证 | Phase / PhaseGroup 模型通用 | YAML 新增集成 verify 阶段，框架零改动 |

唯一需要非平凡改动的是**选择性回退**——当前回退是"跳回 `rollback_target` 重新走后续所有 phase"，对巨大任务需要"只重新规划失败的子任务"。但这个改动局限在 `_handle_exhausted` + plan phase 的上下文注入逻辑，不涉及 TaskSession / Phase / PhaseGroup 的核心模型重构。

### 预算会计（Budget Accounting）

子 TaskSession 工具调用计入父级预算，确保总资源消耗可控。子级上限与父级剩余取较小值，防止单个子任务吃掉所有预算。需要 BudgetTracker 组件在父子间共享。

### 并行 SubAgent

Phase 内通过 `spawn_sub_agents()` 并行执行多个有界任务（如 research 阶段并行调研多个模块）。SubAgent 不递归，不需要自己的 plan/verify。

### Git Worktree 隔离

并行写操作的执行单元需要文件系统隔离。通过 `git worktree` 为每个并行子任务创建独立工作目录，execute 完成后在 integrate 阶段合并。需要新增 integrate⟷verify PhaseGroup 处理合并冲突。

### Port/Adapter 抽象

当 TaskSession 与外部模块的接口稳定后，抽象为 Protocol 接口：TurnExecutionPort、AgentPort、SessionPort、ToolRegistrationPort、WorkspacePort。实现依赖倒置，使 orchestration 包可独立测试。

### Model Router

PhaseConfig 已支持 `model` 字段实现静态模型分配（Phase 3）。Model Router 在此基础上增加**动态**能力：lead（深度推理：research、plan）vs worker（机械执行：implement、verify），含失败升级机制——worker 连续失败超阈值时自动升级为 lead。

### 并行子 TaskSession + 依赖图

子 TaskSession 并行执行（需配合 worktree 隔离），支持 `depends_on` 声明子任务间依赖关系，有依赖的串行执行，无依赖的并行执行。

---

## Appendix

### A. 新增模块清单

```
src/everbot/core/orchestration/           [NEW]
  ├── __init__.py
  ├── task_session.py                     # TaskSession, TaskSessionConfig, TaskSessionState
  ├── phase.py                            # Phase 执行逻辑
  ├── phase_group.py                      # PhaseGroup 循环 + 外循环回退
  ├── verification.py                     # VerificationCmdConfig, _extract_verify_result
  ├── workflow_loader.py                  # YAML dict → Config（纯函数）+ 校验
  ├── events.py                           # TaskSessionEvent 定义
  └── report.py                           # WorkflowReport 生成 + Markdown 渲染

skills/<skill-name>/
  └── workflows/                          [NEW]
      ├── feature-dev.yaml
      ├── bugfix.yaml
      └── implement-verify.yaml
```

### B. 现有模块影响

| 模块 | 变化 | 说明 |
|------|------|------|
| `TurnOrchestrator` | **不变** | TaskSession 直接调用 |
| `AgentFactory` | **不变** | TaskSession 直接调用 |
| `SessionManager` | **小幅扩展** | `infer_session_type()` 需识别 `workflow_` 前缀 |
| `ContextStrategy` | **新增注册** | 新增 `WorkflowContextStrategy` |
| `events.py` | **不变** | 新增 `source_type` 值（`workflow`、`workflow_checkpoint`） |
| `Channel 层` | **小幅扩展** | `_on_background_event` 扩展 `source_type` 白名单 |
| `mailbox.py` | **不变** | 通过已有 `deposit_mailbox_event()` 使用 |
| SKILL scripts/references | **不变** | 现有文件被 workflow `instruction_ref` 引用 |

### C. LLM 可用工具

**已有工具**（Dolphin 内置）：`_bash`, `_python`, `_read_file`, `_read_folder`, `_date`, `_load_skill_resource`

**新增框架工具**：

| 工具 | 可用场景 | 说明 |
|------|---------|------|
| `start_workflow(name, context)` | SKILL 上下文中 | 触发多阶段执行 |
| `spawn_task_session(desc, workflow, context?)` | implement Phase 内 | Phase 2 实现。`context` 注入子 session 初始 artifacts |
