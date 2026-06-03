# Epic:用 milkie 跨进程替换 dolphin agent provider

> 本文件是该替换工作的**单一事实源(SSOT)**。GitHub 不另建 epic issue;阶段性
> 工作可各自开分支 / PR。关联:milkie#86(最小 serve)、milkie#87(跨语言工具桥)、
> alfred#32(AgentProvider 抽象,已 merged)。

## 1. 目标

把 alfred 的 agent runtime 从 `dolphin`(Python,进程内 SDK)替换为 `milkie`
(TypeScript,事件溯源 + 确定性 replay + checkpoint,能力更强),最终拆除对
dolphin 的依赖。

## 2. 核心约束:跨语言

milkie 是 **TS/Node 库**,alfred + dolphin 是 **Python**。Python 无法 `import`
milkie。因此替换形态固定为**跨进程 sidecar**:

```
alfred(Python) ──spawn──▶ milkie serve(Node,HTTP+SSE,生命周期绑定 alfred)
        │  POST /chat(响应体即 SSE)         │
        │◀── message_delta / tool.* / 终态 ──┘
   MilkieProvider 适配层(事件映射)
```

- milkie 自己**不绑死 alfred 协议**:serve 透出 milkie 原生事件名,事件映射
  (pid 合成、stage 分类、字段翻译到 `TurnEvent`)是 alfred 适配层的职责。
- alfred **自托管** sidecar(spawn + 等就绪信号 + SIGTERM),不依赖中心化常驻服务。

## 3. 关键事实与坑(已核实,勿重新踩)

- **pid ≠ toolCallId**:dolphin `progress.id` 是 StageInstance 自生成 uuid(llm/
  skill 块都有);milkie `toolCallId` 仅覆盖工具调用配对。适配层需自己合成 pid
  (工具块用 toolCallId,LLM 块自生成 —— turn_orchestrator 的 llm 分支本就不读 pid)。
- **token 流不进持久化 EventStore**:milkie `message_delta` 是非持久化、仅走
  `onModelEvent` 回调;serve 在 handler 闭包里把它写进同一条 SSE。
- **终态由 serve 合成**:`agent.run.completed` 从广播白名单排除,改由 `AgentResult`
  合成(status + output),保证终态唯一、不受广播时序影响。这是「省 /status」的前提。
- **连本地 sidecar 必须 `trust_env=False`**:环境有 `http_proxy` 时 httpx 会把
  127.0.0.1 也代理掉 → /chat 502。e2e 实测踩到,已在 `MilkieProvider._new_client` 修复。
- **milkie 无独立 think/reasoning 事件**:alfred 的「think-only 轮」循环检测退化为
  当空轮处理即可,不影响功能。

## 4. 阶段路线

每阶段独立分支 / PR;A 阶段全程保持现有测试绿(仍用 DolphinProvider 实现),风险隔离。

| 阶段 | 范围 | 状态 |
|---|---|---|
| **第0步 垂直切片 PoC** | 真 `milkie serve` + sidecar 管理 + SSE/adapter,证明 token 流端到端透传 | ✅ **已完成** |
| **A 接口收敛** | 扩 `AgentProvider`(run_turn 中立事件契约 / context / trajectory / skillkit);turn_orchestrator 改吃中立事件。仍用 DolphinProvider,行为不变 | ✅ **接口收敛完成**:A1 run_turn + A2a/A2b 干净点 context(workflow/skill_change_detector/cron/heartbeat/core_service/chat_service)+ A3 trajectory + A4 skillkit,全部 DolphinProvider 实现,**1585 全量回归绿**。🔶 剩余 A2b 点 = session/persistence 的 dolphin 快照层(snapshot/portable_session/_history/SessionCompressor),本质即 A2 会话持久化。**milkie 侧端点已交付(#124 `/session/export·import` merged)**,待 alfred session 层对接 |
| **C MilkieProvider 接入** | 接进 `get_provider` + 配置开关;sidecar 产品化;状态/会话映射 | ✅ **turn 层端到端 + C3 配置开关 + MilkieProvider 完整 AgentProvider 契约 done**(两 provider 均满足 runtime_checkable);🔶 set_variable/get_variable 已走 #83 `/context/*` 端点实现;**call_llm 已对接 #126 `/llm`(fast→tier、temperature、raise_on_error 双语义,真 serve tier 路由 e2e 绿)**;register_skillkit 待 milkie#87 |
| **#3 daemon 集成** | sidecar 池(惰性 spawn+常驻)+ launcher + 创建路径收敛(agent_service/cron/inspector/heartbeat 经 `get_provider_for_agent`)+ daemon 生命周期统一回收 + resume 实现 + 裸 context 收敛 | ✅ **已完成**(全程 dolphin 行为零变化,1658 unit 绿 + 真 e2e:池 spawn→create_agent→run_turn→shutdown 子进程退出) |
| **#4 软翻默认** | per-agent 路由(显式>telegram 自动回退 dolphin>全局)+ 配置示例 | ✅ **路由+回退已交付**;`everbot.provider` 默认值代码层仍 dolphin(零配置安全),翻转经发布配置体现 |
| **D Python skill 桥** | telegram 等 Python skill 在 milkie 下可用(对应 milkie#87) | ⬜ 待办(P2,条件性;telegram-serving agent 现自动回退 dolphin 兜住) |
| **去 dolphin 硬依赖** | 拆 `import dolphin` 与依赖声明 | ⬜ 最后(依赖 milkie#87;保留 DolphinProvider 作 fallback) |

### 验证方法(环境前置 + 命令)

**环境前置(关键,易踩)**:
- 必须 `PYTHONPATH=src`:pyproject 的 `pythonpath=["."]` 指不到 src layout,现有 CI 即用此方式。
- 用 `.venv/bin/python -m pytest`;`asyncio_mode=auto`,async 测试无需标记。
- e2e 需 node + milkie 已 build:`cd ../milkie && npm run build`(serve 进 dist);**未 build 时 e2e 自动 skip**(不算失败)。

**第0步 验证命令(可复现)**:
```bash
# 1) 单元:快、确定、无外部依赖 → 28 passed
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/unit/test_milkie_sse.py tests/unit/test_milkie_adapter.py \
  tests/unit/test_milkie_sidecar.py tests/unit/test_milkie_provider.py -q

# 2) e2e 冒烟:真 spawn `milkie serve` + 本地 fake OpenAI 流式 server(无 key)→ 1 passed
PYTHONPATH=src .venv/bin/python -m pytest tests/e2e/test_milkie_serve_smoke.py -q
#    断言:收到 ≥2 个 LLM_DELTA(证明逐 token,非整段),拼成 "Hello, world!"
#         + 唯一 TURN_COMPLETE(status=completed) + 子进程 SIGTERM 后已退出

# 3) 回归 + 边界守护(主干不得 import dolphin)→ 10 passed
PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_agent_provider_*.py -q
```

**各阶段验收标准**:
- **第0步**:上述三组命令全绿 = token 流端到端 + 子进程托管 + 不破坏现有。✅
- **A 接口收敛**:扩 `AgentProvider` 后**现有全部测试仍绿**(行为零变化是硬标准);新接口有契约/单元测试;turn_orchestrator 改吃中立事件后其现有测试绿。
- **C 接入**:配置开关切 milkie 后一条真实对话端到端通(复用 e2e 模式);切回 dolphin 仍绿(双 provider 并存验证)。
- **D skill 桥**:telegram 等 Python skill 在 milkie 下被调用的 e2e。
- **去依赖**:边界守护反转(主干 + provider 均不得 import dolphin);全套回归绿。

## 5. 当前进度

**已完成(本 session,TDD 全程红绿,alfred 全量 1585 passed)**:
- **alfred**:第0步 PoC、A1 run_turn、A2a/A2b context 收敛、A3 trajectory、A4 skillkit(接口收敛 100%)、C1/C2 MilkieProvider 走 `_progress`、C 端到端 + C3 配置开关、MilkieProvider 完整 AgentProvider 契约 + **context var 经 serve 端点跨进程**。
- **milkie**:serve 暴露 `/context/set·get·list`(#83)+ `/llm` 一次性 LLM + `/session/export·import`(#124,已 merge 进 main)端点。
- **端到端验证**:① config 切 milkie → turn_orchestrator → 真 serve → 同构 TurnEvent;② context var 跨进程往返(MilkieProvider ↔ 真 serve)。
- 提交:alfred(`0e5ef8d`…`e47f056`,分支 `feat/34-milkie-provider-poc`);milkie(`05c2169`,分支 `feat/86-milkie-serve`)。

**剩余(去 dolphin 依赖的前置)**:
- ~~`call_llm`~~ ✅ **已完成**:milkie #124(`/llm`)+ #126(`tier`+`temperature`)均交付;alfred `MilkieProvider.call_llm` 已对接(`fast`→tier、temperature、raise_on_error 双语义、HTTP 错误映射),含真 serve tier 路由跨进程 e2e。🔶 遗留:生产 spawn serve 时把 alfred config 的 default_model/fast_llm **写进 agent 文件两档 model**(目前仅 e2e 手写),属 sidecar 产品化范畴。
- ~~A2 会话持久化~~ ✅ **已完成**:milkie #128(`/session/history` 全量逐条历史)+ #130(serve `--state-store sqlite --data-dir` 持久化,重启恢复)交付;alfred `export_session` 接口收敛(5 处调用点 → DolphinProvider 行为不变)+ `MilkieProvider.export_session` 走 #128 翻译 canonical `Message[]`→alfred history;restore **不灌回**靠 serve 自持久化。**重启恢复 e2e 绿**(sqlite serve→run_turn 产历史→SIGTERM 重启→同 contextId 完整取回)。🔶 遗留:sidecar 启动前需 mkdir data-dir(SQLiteStore 不自建)。
- `register_skillkit` / D Python skill — 需 milkie#87 跨语言工具桥(双向 RPC,大功能,P2 条件性;仅当要在 milkie 下复用 telegram 等 Python skill 才需要)。
- 去 dolphin 依赖 — 依赖以上全部。

**第0步垂直切片 PoC 已完成(TDD,含真 e2e)**:

```
src/everbot/core/agent/provider/milkie/
  __init__.py / sse.py / adapter.py / sidecar.py / provider.py
tests/unit/   test_milkie_{sse,adapter,sidecar,provider}.py
tests/e2e/    test_milkie_serve_smoke.py   ← 真 spawn milkie serve + fake OpenAI 流式 server
```

- 验证:fake OpenAI 多 content 帧 → milkie LLM stream → onModelEvent → serve SSE
  → MilkieProvider → 逐 `LLM_DELTA` + `TURN_COMPLETE(output="Hello, world!")`。
- 子进程 e2e 缺口(milkie#86 验收指出)由此兜住:就绪信号 + SIGTERM 优雅退出。

## 6. 诚实现状:数据面 + 控制面(#3 daemon 集成)已通,剩去硬依赖(待 milkie#87)

**已对接(provider 抽象,DolphinProvider 行为零变化全程回归绿)**:
- `call_llm` → `/llm`(tier+temperature);`export_session` 收敛(5 调用点)+ `MilkieProvider` 走 `/session/history`;`restore` 收敛(`needs_history_restore`,milkie 靠 serve 自持久化跳过灌回);`interrupt/resume` 收敛(chat_service 去裸调用)。
- sidecar 产品化**奠基**:`agent_spec`(dolphin model 配置→milkie 两档 ModelConfig + agent.md 生成),真 serve 加载 e2e 绿。
- **3 个真 e2e 端到端验证 milkie 路径核心**:① tier 路由(`/llm`)② 重启恢复(sqlite serve+SIGTERM 重启+export)③ 生成 agent.md 跑 turn。

**关键修正**:主干对 dolphin agent 对象的真实裸耦合 **~17 处**(此前 60 是把 `import everbot.core.agent.factory/provider` 模块路径、`"agent.dph"` 文件名误判)。大头 `agent.executor.context`(多在 dolphin-only 持久化层,milkie 已 short-circuit/收敛)。

**#3 daemon 集成 — ✅ 已完成**(设计 `docs/design/34-milkie-daemon-integration.md`,计划 `…-plan.md`):
- **sidecar 池 + launcher**:`SidecarLauncher`(agent 配置→serve 命令+env+data-dir,复用 agent_spec)+ `SidecarPool`(`agent_name→serve`,惰性 spawn、并发只 spawn 一次、统一 SIGTERM 关闭),挂在 `MilkieProvider` 内。**async spawn vs 同步 `get_provider` 矛盾化解**:`get_provider` 保持同步返回单例,spawn 发生在 async `create_agent`;pool 惰性构建,构造不做 config I/O。
- **创建路径收敛**:`agent_service.create_agent_instance` + cron `CronExecutor._create_job_agent`(真实隔离任务路径)+ `Inspector._run_llm` + heartbeat `_restricted_agent_factory` 全部经 `get_provider_for_agent(name).create_agent(...)`(原直连 dolphin factory)。删除了 heartbeat 侧的死代码 `_create_job_agent`/`_execute_isolated_task`。
- **daemon 生命周期**:`stop()` 调 `shutdown_all_providers()`(覆盖全局单例 + `_provider_by_name` 缓存中真正持池的 provider,防 serve 泄漏);dolphin no-op;`AgentProvider` 补 `shutdown_sidecars` 契约。
- **resume 实现**:milkie `/resume` 流式当新一轮消费(去 NotImplementedError)。
- **真实 system_prompt loader**:`WorkspaceLoader.build_system_prompt()`(SOUL/AGENTS/SKILLS/USER/MEMORY.md),缺 workspace 则 raise(不静默空)。
- **裸 context 收敛**:cron/heartbeat/core_service/persistence/session 的 8 处 `agent.executor.context`/`agent.snapshot` 访问全部 milkie-safe(turn 路径 var 读经 `provider.get_variable`;持久化/记忆深访问用 `needs_history_restore()` 守护,dolphin 路径**字节级不变**)。

**#4 软翻默认 — 路由+回退已交付**:`get_provider_for_agent` 实现 per-agent 路由(显式 `everbot.agents.<name>.provider` > 全局 milkie 下 telegram-serving agent 自动回退 dolphin+warning > 全局)。telegram skillkit(milkie#87 未建)是唯一硬卡点,被自动回退兜住。`everbot.provider` 默认值代码层仍 dolphin(零配置安全),示例配置已文档化切换方式。

**仍剩(均依赖 milkie#87 或属硬清理)**:
- D Python skill 桥(milkie#87):telegram 等编程式 skillkit 在 milkie 下原生可用 —— 落地后可撤 telegram 自动回退。
- 去 dolphin **硬依赖**:拆 `import dolphin` + requirements;依赖 milkie#87 + 保留 DolphinProvider 作 fallback,故置最后。
- milkie 侧 gap:`createGateway` per-model apiKey 注入(两档跨 cloud 时;后续 milkie issue)。

**评估**:#3 daemon 集成本 session 已完整交付(sidecar 池/生命周期/创建路径/resume/裸 context 收敛),全程 dolphin 行为零变化(1658 unit 绿)+ 真子进程 e2e 验证;#4 路由与回退就位,milkie 可经配置端到端跑通。去硬依赖待 milkie#87。

## 7. milkie 侧依赖

- **milkie#86** 最小 `milkie serve`(HTTP+SSE,含 token 透传):✅ 已交付,alfred 验收通过(8/8 + 9/9 测试)。
- **milkie#124** serve 端点增补(`/llm` 一次性 LLM + `/session/export·import` portable session):✅ 已交付,merge 进 main(23 jest passed)。顺手落地了 #83 context 端点的孤儿提交。
- **milkie#126** `/llm` 加 `tier`(具名 model 档)+ `temperature`:✅ 已交付 merged,alfred `call_llm` 已对接。
- **milkie#128** `/session/history` by-context 全量逐条历史导出(含 tool chain):✅ 已交付 merged,alfred `export_session` 已对接。
- **milkie#130** serve 持久化 store(SQLite+Jsonl,sidecar 重启从 checkpoint 恢复):✅ 已交付 merged,重启恢复 e2e 已验证。
- **milkie#87** 跨语言工具桥:🔲 P2,仅当 D 阶段要复用 Python skill 才需要。
- milkie P0/P1(#80–#85)+ #124 均已 closed/merged。
