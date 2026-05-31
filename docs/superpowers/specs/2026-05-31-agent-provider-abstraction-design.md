# AgentProvider 抽象层设计（解耦 dolphin SDK）

- Issue: #32
- 分支: `feat/32-agent-provider-abstraction`
- 日期: 2026-05-31

## 1. 背景与目标

alfred 当前把 dolphin 当作**进程内 Python SDK** 深度耦合使用，没有统一的 provider 抽象层。直接 `import dolphin` 散落在 6 个主干源文件中，外加 `infra/dolphin_compat.py` 这一薄兼容垫片。

**目标**：引入 `AgentProvider` 抽象层，把所有 `import dolphin` 收敛到一个 `provider/dolphin/` 包里。本 issue 后，alfred 主干代码（除 `provider/dolphin/` 包外）再无任何文件 `import dolphin`。

**最高优先级不变量：行为零变化。** dolphin 仍是唯一实现，只是被收敛到一个包后面。事件流格式、factory 内部逻辑、history compaction 设置、状态机语义全部原样保留。这是一次**纯结构性重构**，不改任何运行时行为。

## 2. 现状：dolphin 耦合点全清单

| 文件 | dolphin 依赖 | 耦合性质 |
|---|---|---|
| `core/agent/factory.py` | `DolphinAgent` / `GlobalConfig` / `GlobalSkills` | 构造：创建并初始化 agent（~1000 行，含大量 alfred 专属编排） |
| `core/channel/core_service.py` | `AgentState`（类型）+ 裸 `agent.executor.context` / `agent.snapshot` / `agent.state` | 类型枚举 + 鸭子访问 |
| `web/services/chat_service.py` | `AgentState` / `PauseType`（类型）+ 裸 `agent.interrupt()` / `resume_with_input()` / `_pause_type` | 类型枚举 + 鸭子访问 |
| `core/memory/_extractor_helpers.py` | `LLMClient` / `Messages` / `MessageRole` | 一次性 LLM 调用 |
| `core/session/compressor.py` | `LLMClient` / `Messages` / `MessageRole` | 一次性 LLM 调用 |
| `channels/telegram_skillkit.py` | 继承 `Skillkit` / `SkillFunction` | 工具插件基类 |
| `infra/dolphin_compat.py` | `dolphin.core.flags` + 常量 | 已有的兼容垫片 |

**关键事实**：`core/runtime/turn_orchestrator.py`（~1200 行运行循环）**不 import dolphin**——它已是鸭子类型（`agent: Any`，仅调用 `continue_chat`/`arun`，消费 progress-dict 事件格式）。因此最复杂的运行循环**无需改动**，仅需让传入的 agent 仍满足同样的鸭子接口（dolphin agent 原样满足）。

## 3. 架构：provider 包

新增 `src/everbot/core/agent/provider/` 包：

```
src/everbot/core/agent/provider/
  __init__.py        # 对外公共出口：get_provider() + 端口/类型/helper re-export
  base.py            # AgentProvider 协议 + 中立类型/常量（不 import dolphin）
  dolphin/
    __init__.py
    provider.py      # DolphinProvider(AgentProvider) — 组合下列能力
    factory.py       # 由现 core/agent/factory.py 迁入（创建逻辑，import dolphin）
    llm.py           # 由现 _extractor_helpers.call_dolphin_llm 迁入（一次性 LLM 调用）
    state.py         # 封装 AgentState/PauseType → is_paused/is_error/is_user_interrupt_paused 等 helper
    skillkit.py      # re-export Skillkit / SkillFunction 基类
    compat.py        # 由现 infra/dolphin_compat.py 迁入（flags + 常量）
```

**边界规则**：`base.py` 与所有主干消费方都**不得** `import dolphin`；只有 `provider/dolphin/**` 可以。这是可被一条 grep 守护的硬约束（见 §7 测试）。

## 4. 端口设计（`base.py`，中立、不依赖 dolphin）

`AgentProvider` 定义为 `typing.Protocol`（鸭子接口，便于测试替身）。它聚合当前从 dolphin 获得的全部能力：

```python
class AgentProvider(Protocol):
    # 1) 创建 —— 返回裸 agent 对象（保持现有鸭子接口，turn_orchestrator 原样消费）
    async def create_agent(
        self, agent_name: str, workspace_path: Path,
        model_name: str | None = None,
        extra_variables: dict | None = None,
        tools_override: list[str] | None = None,
    ) -> Any: ...

    # 2) 状态查询 helper（替代主干里对 AgentState/PauseType 的直接比较）
    def is_paused(self, agent: Any) -> bool: ...
    def is_error(self, agent: Any) -> bool: ...
    def is_user_interrupt_paused(self, agent: Any) -> bool: ...

    # 3) 一次性 LLM 调用（替代 _extractor_helpers / compressor 里的裸 LLMClient）
    async def call_llm(
        self, context: Any, prompt: str,
        temperature: float = 0.3, fast: bool = False,
    ) -> str: ...

    # 4) 运行时兼容（替代 ensure_continue_chat_compatibility）
    def ensure_chat_compatibility(self) -> bool: ...
```

**中立常量**（从 `base.py` 重新导出，值与 dolphin 当前一致）：`KEY_HISTORY`、`KEY_HISTORY_COMPACT_ON_PERSIST`、`KEY_HISTORY_COMPACT_RECENT_TURNS`。

**Skillkit 基类**：telegram 需要继承 dolphin 的 `Skillkit`。由 `provider/__init__.py` re-export `SkillkitBase` / `SkillFunction`，telegram 改为 `from ...core.agent.provider import SkillkitBase, SkillFunction`。这保留 dolphin 的 skill 调用约定（`props` / `gvp`），行为不变。

**裸 agent 的处理（关键决策）**：本设计**不**引入 Session 包装对象。裸 dolphin agent 继续在 core_service / chat_service / session_manager / cron / heartbeat 间流转，其 `.executor.context` / `.state` / `.snapshot` / `.interrupt()` 等鸭子访问**原样保留**。抽象只覆盖「类型 import」与「构造/LLM/兼容」这几个真正 `import dolphin` 的点。理由：

- 行为零变化、改动面最小、风险最低，精准满足「收敛到一个包」的目标。
- 引入 Session 包装会波及 session_manager/cron/heartbeat 等大量鸭子访问点，行为变化风险高，超出本 issue 范围。
- 未来真正接入第二个 provider（如 milkie）时，再按需把裸访问收敛进 Session——届时有了本层端口作为前置基础。

## 5. 获取 provider 的方式

`provider/__init__.py` 暴露 `get_provider() -> AgentProvider`，默认返回单例 `DolphinProvider()`。当前阶段只有一个实现，不引入配置开关（YAGNI）。主干通过 `get_provider()` 取得 provider，再调用其方法。

> 注：`create_agent` 模块级函数与 `AgentFactory` 类的现有公共导入路径（`core/agent/__init__.py`、`agent_service.py`）通过 re-export 保持可用，避免破坏现有调用方与测试。

## 6. 各文件改造（保持行为）

1. **`provider/dolphin/factory.py`**：现 `core/agent/factory.py` 整体迁入，**内部逻辑一字不改**。`core/agent/factory.py` 改为从新位置 re-export（保留 `AgentFactory` / `create_agent` 旧导入路径）。`core/agent/__init__.py` 顺手清理现存的重复 import 行（历史遗留，纯清理、不改语义）。
2. **`provider/dolphin/compat.py`**：现 `infra/dolphin_compat.py` 的 dolphin 部分迁入；`infra/dolphin_compat.py` 改为薄 re-export shim（`KEY_HISTORY` 等常量 + `ensure_continue_chat_compatibility`），保持现有 import 路径与 `test_dolphin_compat.py` 可用。
3. **`core/channel/core_service.py`**：删除 `from dolphin... AgentState`；`agent.state != AgentState.PAUSED` → `not provider.is_paused(agent)`；`agent.state == AgentState.ERROR` → `provider.is_error(agent)`；`ensure_continue_chat_compatibility()` → `provider.ensure_chat_compatibility()`；`KEY_HISTORY` 从 provider/compat 取。裸 `executor.context` / `snapshot` 访问保留。
4. **`web/services/chat_service.py`**：删除 `from dolphin... AgentState, PauseType`；两处 `agent.state == AgentState.PAUSED and agent._pause_type == PauseType.USER_INTERRUPT` → `provider.is_user_interrupt_paused(agent)`。裸 `interrupt()` / `resume_with_input()` 保留。
5. **`core/memory/_extractor_helpers.py`**：`call_dolphin_llm` 的 dolphin 实现迁入 `provider/dolphin/llm.py`；helper 改为 `provider.call_llm(...)`。`format_messages` / `extract_json_object` 等纯函数留原处。调用方 `event_extractor.py` / `profile_extractor.py` 行为不变。
6. **`core/session/compressor.py`**：`_generate_summary` 内的裸 `LLMClient` 调用改为 `provider.call_llm(context, prompt, temperature=0.3, fast=True)`（`fast=True` 对应原 `fast_llm` 模型选择，保持模型选取语义）。
7. **`channels/telegram_skillkit.py`**：`from dolphin.core.skill...` → `from ...core.agent.provider import SkillkitBase, SkillFunction`；类继承 `SkillkitBase`。逻辑不变。

## 7. 测试策略（TDD）

先写测试再写实现。新增测试：

- **边界守护测试**（核心）：grep/AST 扫描 `src/everbot/`，断言除 `core/agent/provider/dolphin/**` 外无任何文件出现 `import dolphin` / `from dolphin`。这是「收敛到一个包」的可执行验收。
- **端口契约测试**：用一个 fake agent（普通对象，模拟 dolphin agent 的 `.state`/`._pause_type` 等）验证 `is_paused`/`is_error`/`is_user_interrupt_paused` 的真值表与原 `AgentState`/`PauseType` 比较等价。
- **常量等价测试**：断言 provider/compat 导出的 `KEY_HISTORY` 等常量值与 dolphin 当前值一致。
- **`call_llm` 行为测试**：mock dolphin `LLMClient`，验证消息构造、模型选择（`fast` 开关）、错误前缀（`❌`/`failed to call LLM`）抛 `RuntimeError` 的行为与原 `call_dolphin_llm` 等价。

回归：现有全部单元/集成测试保持绿色（`conftest.py` 对 dolphin 常量的 patch 不受影响；测试可继续直接 import dolphin 做 mock，不在本约束范围内）。

## 8. 端到端验证（行为不变）

抽象完成、回归绿色后：

1. **实际切换运行服务**：以改造后的代码启动 alfred 服务（web/Telegram）。
2. **端到端证明行为一致**：覆盖对话（流式 delta + 工具事件）、会话持久化与恢复、用户打断/恢复（pause/resume）、心跳（heartbeat）路径，确认与改造前表现一致。由于本次只加一层间接、未改逻辑，预期行为完全相同。

## 9. 非目标

- 不实现 milkie provider。
- 不更换 LLM 客户端底层实现（仍走 dolphin `LLMClient`；未来可单独迁 litellm）。
- 不移除 dolphin 依赖本身（dolphin 仍是唯一实现）。
- 不引入 Session 包装对象（裸 agent 流转保留）。
- 不改动 `turn_orchestrator.py` 的事件解析逻辑。
