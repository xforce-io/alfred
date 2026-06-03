# 设计:milkie daemon 集成(#3)+ 软翻默认(#4)

> 关联:`goal.md`(Epic SSOT)、分支 `feat/34-milkie-provider-poc`。
> 本文是 #3/#4 两阶段的设计事实源;落地后把一行摘要回写 `goal.md` 第 4/6 节。

## 1. 目标与终点

把 milkie 从「数据面契约已通、但主干创建路径还没切」推进到 **daemon 端到端可跑 milkie**,
并把全局默认 provider 软翻为 milkie。

- **#3 daemon 集成**:agent 创建路径收敛到 `provider.create_agent`;MilkieProvider
  自管理 sidecar 池(惰性 spawn + 常驻 + daemon 生命周期统一回收);补 `resume()`;
  收敛剩余裸 `agent.executor.context` 访问。
- **#4 软翻默认**:`everbot.provider` 默认改 `milkie`;**telegram-serving agent 自动回退
  dolphin**;保留 `import dolphin` 与 `DolphinProvider` 作 fallback,**不强删**。

### 范围边界(经核实收窄)

milkie 真正缺失、且影响 daemon 路径的能力**只有一处**:`register_skillkit`
(telegram `tg_skillkit` 注入,依赖未建的 milkie#87)。
`~/.alfred/skills/` 的 SKILL.md 技能走通用工具执行,**与本设计无关**。
`resume` 在本设计内实现(milkie `/resume` 流式语义),不再是阻塞。

**非目标**:milkie#87 跨语言工具桥;强删 dolphin 依赖;telegram 在 milkie 下原生发送
(留作后续,撤回退判定的前置)。

## 2. 架构

```
get_provider()                         [同步,全局单例]
  └─ everbot.provider = milkie | dolphin

get_provider_for_agent(agent_name)     [同步,per-agent 路由 — 新增]
  ├─ everbot.agents.<name>.provider 显式覆盖 → 用之
  ├─ 全局 milkie 且该 agent 经 telegram 服务且未显式声明 → 回退 dolphin + warning
  └─ 否则 → 全局 provider

MilkieProvider                         [持 launcher + SidecarPool]
  └─ async create_agent(agent_name):
       SidecarPool.get_or_spawn(agent_name)
         ├─ 命中 → 复用 MilkieSidecar(已就绪)
         └─ 未命中 → SidecarLauncher.build(agent_name)
                      → 生成 agent.md(system_prompt + 两档 model)
                      → mkdir data-dir
                      → 拼 serve cmd + per-cloud key env
                      → MilkieSidecar.start()(等 MILKIE_SERVE_READY <port>)
                      → 入池
       返回 MilkieAgentHandle(base_url = 该 serve, context_id)
```

一个 `milkie serve` 进程 = 一个 agent 定义(`--agent agent.md`);同 agent 的多会话
共享该 serve,靠不同 `context_id` 区分。池按 **agent_name** 维度,不按 session。

## 3. 组件

### 3.1 `SidecarLauncher`(新增 `provider/milkie/launcher.py`)

职责:把一个 alfred agent 的配置翻译成可 spawn 的 `milkie serve` 命令 + env。

输入(从 alfred config 读取):
- agent 的 `system_prompt`(复用 dolphin factory 现有的 agent 定义加载路径)。
- dolphin `llms` / `clouds`(模型档)+ `default_model` / `fast_llm` →
  复用 `agent_spec.build_milkie_model_tiers` 生成两档 ModelConfig。
- `everbot.milkie.dist_path`(默认 `../milkie/dist/cli/index.js`)、`node` 可执行名。
- `everbot.milkie.data_dir_root`(默认 `~/.alfred/milkie/<agent_name>`)。

输出:
- 写出 `agent.md`(`agent_spec.build_milkie_agent_md`)到 data-dir 下。
- 命令:`["node", <dist>, "serve", "--agent", <agent.md>, "--port", "0",
  "--state-store", "sqlite", "--data-dir", <data_dir>]`(port 0 = OS 分配,
  从就绪信号读回)。
- env:按各档 model 的 cloud 注入对应 `api_key`(milkie OpenAICompatibleAdapter 仅从
  env 读 key —— 见 `agent_spec` 已知 gap)。两档跨不同 cloud(不同 key)是已知 milkie
  限制,留 milkie issue;单 cloud 场景本设计可用。
- 预建 data-dir(`mkdir -p`)—— milkie `SQLiteStore` 不自建目录。

纯函数化:`build(agent_name) -> (cmd: list[str], env: dict, data_dir: Path)`,便于单测断言
命令拼装而不真 spawn。

### 3.2 `SidecarPool`(MilkieProvider 内部)

- `_sidecars: dict[str, MilkieSidecar]`,`_locks: dict[str, asyncio.Lock]`。
- `async get_or_spawn(agent_name) -> MilkieSidecar`:per-agent 锁串行化,命中即复用,
  否则 launcher.build → `MilkieSidecar.start()` → 入池。**并发同 agent 只 spawn 一次**。
- `async shutdown_all()`:并发 `MilkieSidecar.close()`(SIGTERM,超时 SIGKILL — 已实现),
  清空池。幂等。

### 3.3 `MilkieProvider` 升级

- 构造从「只持 `base_url`」改为「持 `SidecarLauncher` + `SidecarPool`」。
  `base_url` 不再来自 config 固定值,而是 spawn 后每 agent 各自的端口。
- `create_agent`:走池,返回带该 serve `base_url` 的 handle(替代当前固定 base_url)。
- `resume()`:**实现**。milkie `/resume` 是流式 —— 不再 `NotImplementedError`,而是把
  `/resume` 响应当作新一轮事件流消费(语义对齐:注入消息后续跑,产 `TurnEvent`)。
  调用方 `chat_service` 现有 try/except 兜底不变。
- `register_skillkit`:**保持** `NotImplementedError`(本设计不解,靠 per-agent 回退绕开)。
- `shutdown_sidecars()`:转发 `SidecarPool.shutdown_all()`。

> 测试注入:保留 `client` / `sync_client` 注入入口;新增 launcher 注入(测试喂 fake cmd)。

### 3.4 per-agent provider 路由(`provider/__init__.py`)

- 新增 `get_provider_for_agent(agent_name: str) -> AgentProvider`(同步)。
- 判定:
  1. `everbot.agents.<name>.provider` 显式 → 直接用(milkie/dolphin)。
  2. 全局 milkie + 未显式 + **该 agent 经 telegram 服务** → 回退 dolphin,
     首次回退记一条 warning(「telegram skillkit 待 milkie#87,暂回退 dolphin」)。
  3. 否则 → 全局 `get_provider()`。
- 「经 telegram 服务」判定:读该 agent 是否在 telegram 频道配置内
  (`everbot.channels.telegram` 的 agent 绑定);具体键在实现期对齐。
- DolphinProvider / MilkieProvider 各自仍是进程内单例(milkie 池在 MilkieProvider 内)。

### 3.5 创建路径收敛

当前直连 dolphin factory 的两处改走 provider:
- `agent_service.create_agent_instance`(`create_agent(...)` → `get_provider_for_agent(name).create_agent(...)`)。
- heartbeat `_get_or_create_agent`(同上)。
- `control.py:92` 注册的 `agent_factory.create_agent` 改为经 provider 的等价入口。

> dolphin factory 的便捷 `create_agent`/`AgentFactory` 保留(DolphinProvider 内部仍用),
> 只是主干不再直接调它。

### 3.6 daemon 生命周期

- `EverBotDaemon.stop()` / `start()` 的 `finally` 清理段加:
  `await get_provider().shutdown_sidecars()`(DolphinProvider 为 no-op)。
- 放在 scheduler/channel 停止之后、PID/lock 释放之前。
- 进程异常退出兜底:sidecar 生命周期已绑父进程 SIGTERM;daemon 已有 lifecycle_monitor,
  不额外加监控。

### 3.7 收敛剩余裸 context 访问

`persistence.py` / `heartbeat.py` / `session.py` 仍有数处 `agent.executor.context` /
`agent.snapshot.*` 直接访问(Explore 已定位)。这些点多在 dolphin-only 持久化层、milkie
已 short-circuit(`needs_history_restore=False`),但为彻底解耦,逐处改走 provider 接口或
显式 `isinstance`/能力判定守护,确保 milkie handle 流经时不炸。本阶段**只收敛 daemon 主路径
会触达的点**;纯 dolphin 持久化死代码留注释标记。

## 4. 配置(新增键)

```yaml
everbot:
  provider: milkie            # #4:默认翻 milkie(原 dolphin)
  milkie:
    dist_path: ../milkie/dist/cli/index.js   # 可省,默认相对仓库
    data_dir_root: ~/.alfred/milkie          # 可省
    ready_timeout: 20                          # 可省,默认 20s
  agents:
    <name>:
      provider: dolphin       # 可选 per-agent 覆盖
```

## 5. 错误处理

- **spawn 失败 / 就绪超时**:`MilkieSidecar.start()` 抛 → `create_agent` 包装成带
  agent_name + stderr 尾巴的明确错误向上抛;不入池(下次重试)。
- **未知 llm_name**:`dolphin_model_to_milkie` 已 fail-fast KeyError;launcher 透传。
- **telegram agent 误配 milkie**:per-agent 路由优先看显式配置 —— 显式 milkie 则尊重
  用户意图(不强制回退),但 `register_skillkit` 触发时 `NotImplementedError` 会暴露;
  自动判定(未显式)才回退。文档说明此区别。
- **resume 失败**:milkie `/resume` 异常 → 沿用 chat_service 现有 try/except「fresh start」兜底。

## 6. 测试(覆盖边界 / 异常 / 并发 / 退化)

**单元**
- `launcher.build`:命令拼装 / env key 注入 / agent.md 落盘 / data-dir mkdir / 未知 model 抛错。
- `SidecarPool`:惰性首次 spawn、命中复用不重 spawn、**并发同 agent 只 spawn 一次**(锁)、
  `shutdown_all` 幂等 + 并发关闭、spawn 失败不入池。
- `get_provider_for_agent`:显式 milkie / 显式 dolphin / 全局 milkie+telegram 自动回退 /
  全局 milkie+非 telegram 用 milkie / 全局 dolphin。
- `MilkieProvider.resume`:`/resume` 流式事件 → TurnEvent 映射;异常路径。
- 创建路径收敛:`agent_service` / heartbeat 经 provider(mock provider 断言被调)。

**e2e**(milkie 未 build 时自动 skip,沿用现有约定)
- 真 spawn → `MilkieProvider.create_agent` 经池 → `run_turn` 跑通逐 token。
- daemon shutdown → 池内所有 serve 子进程 SIGTERM 后已退出。
- sqlite 重启恢复(已有,纳入回归)。

**回归硬标准**:`everbot.provider` 仍可切回 dolphin,全量测试绿(双 provider 并存)。

## 7. 交付顺序(子任务,各自可独立验证)

1. `SidecarLauncher`(纯函数 + 单测)。
2. `SidecarPool` + `MilkieProvider` 升级(惰性/并发/shutdown 单测)。
3. `MilkieProvider.resume` 实现 + 单测。
4. `get_provider_for_agent` 路由 + 单测。
5. 创建路径收敛(agent_service / heartbeat / control)。
6. daemon shutdown 接线。
7. 剩余裸 context 访问收敛(主路径)。
8. #4:默认翻 milkie + telegram 自动回退 + 配置文档;全量回归。
9. e2e:create_agent 经池 + daemon shutdown 子进程退出。

每步保持现有测试绿(切回 dolphin 行为零变化)。
