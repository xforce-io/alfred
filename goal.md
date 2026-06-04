# Epic:用 milkie 跨进程替换 dolphin agent provider

> 本文件是该替换工作的**单一事实源(SSOT)**。GitHub 不另建 epic issue;阶段性
> 工作可各自开分支 / PR。关联:milkie#86(最小 serve)、~~milkie#87(跨语言工具桥)~~
> (2026-06-04 核查后删除,见 §4 telegram 原生化 / §6 横幅)、alfred#32(AgentProvider 抽象,已 merged)。

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
| **C MilkieProvider 接入** | 接进 `get_provider` + 配置开关;sidecar 产品化;状态/会话映射 | ✅ **turn 层端到端 + C3 配置开关 + MilkieProvider 完整 AgentProvider 契约 done**(两 provider 均满足 runtime_checkable);🔶 set_variable/get_variable 已走 #83 `/context/*` 端点实现;**call_llm 已对接 #126 `/llm`(fast→tier、temperature、raise_on_error 双语义,真 serve tier 路由 e2e 绿)**;register_skillkit 现 raise NotImplementedError —— 不再走"跨语言桥",改由 §4 **telegram 原生化**(TS 重写/输出约定)承接 |
| **#3 daemon 集成** | sidecar 池(惰性 spawn+常驻)+ launcher + 创建路径收敛(agent_service/cron/inspector/heartbeat 经 `get_provider_for_agent`)+ daemon 生命周期统一回收 + resume 实现 + 裸 context 收敛 | ✅ **已完成**(全程 dolphin 行为零变化,1658 unit 绿 + 真 e2e:池 spawn→create_agent→run_turn→shutdown 子进程退出) |
| **#4 软翻默认** | per-agent 路由(显式>telegram 自动回退 dolphin>全局)+ 配置示例 | ✅ **路由+回退已交付**;`everbot.provider` 默认值代码层仍 dolphin(零配置安全),翻转经发布配置体现 |
| **E milkie 能力层** ★真关键路径 | milkie 侧补两样,使现有 skill 在 milkie 下能跑:① `run_command` shell/exec 工具(跑 `python scripts/…`/`inv`/`codex-cli`);② skill 目录发现(扫 `SKILL.md` → 注入 prompt,即 milkie 自己点名未做的 "v2 Skill Registry")。**经源码核查 replay 天然安全**:record 真跑 / replay 全走缓存、handler 在 replay 期绝不执行(`ReplayingIOPort` 持 `ExplodingInnerPort` 单测背书),**无需任何"不可重放"契约**。`create_plan`(非确定 `uuid()`+写 WM)即现成 replay-safe 范例 | ⬜ **P0 待办**(此前 goal.md 未列——力气都花在已完成的数据面)。正确性免费;工程量在 skill 发现 loader + 输出体积治理(见验收) |
| **telegram 原生化**(替代原"D 桥") | telegram 是唯一编程式 skillkit(`_tg_send_file`/`_tg_send_photo`),**无 Python 本质**:全部本质 = 取 chat_id + bot_token + 一个 HTTP POST 到 Bot API。两条路任选:① milkie 侧 **TS 原生重写**这 2 工具(chat_id 走 `/context` #83、token 走 agent 配置);② 挪到 **channel 输出约定**(与文本回复同一条 provider 无关路径)。**不建跨语言桥** | ⬜ 待办(**小**;撤掉 telegram 自动回退的前置) |
| **去 dolphin 硬依赖** | 拆 `import dolphin` 与依赖声明;保留 DolphinProvider 作 fallback | 🔶 **部分**:① 主干 import 边界达标(`test_agent_provider_boundary` 绿,dolphin 收敛在 `provider/dolphin/**`)② `dolphin_compat` 解耦——guarded import,缺 dolphin 也能加载主干(`test_dolphin_compat_optional`)。**仍 gated**(不安全强拆):requirements 移除 + milkie-only 完全可用,需先 (a) oneshot `call_llm`(记忆抽取/历史压缩)中立化(现仍路由 dolphin)(b) 翻默认到 milkie(`everbot.provider` 代码层仍 dolphin,零配置安全)。两者属发布/后续决策,不在本 PR 强行 box |

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
- **E milkie 能力层(可证伪验收)**:
  - *★真实 skill 端到端(核心 —— 证明那十几个 shell 型 skill 在 milkie 下真能跑)*:取一个**现成 shell 型 skill**(如 `skills/ops` 的 `python skills/ops/scripts/ops_cli.py <cmd>`、或 `skills/web` 的 `python $SKILL_DIR/scripts/search.py`),milkie 下的 agent:① 经 skill 发现机制在 system prompt 看到该 skill;② 自主发起 `run_command` 跑该 skill 的**真实 bash 调用**;③ 拿到脚本真实 stdout 并据此完成回复。反例(未达标):agent 看不到该 skill / 无 `run_command` 可调 / bash 调用拿不到结果。
  - *shell 工具冒烟(隔离快测)*:`run_command` 跑 `python -c "print(6*7)"` → stdout 含 `42` + exitCode 0。
  - *replay 安全证伪*:录一条用了 `run_command` 的 run → 把被调脚本改名/删除使其"再次真跑必失败" → 对同一 run `milkie replay` 仍产出原 stdout(证明 handler 未被重跑)。反例:replay 期真的 fork 了进程 / 报脚本不存在。
  - *skill 发现证伪*:往 skill 目录放一个新 `SKILL.md`(声明一条 `python $SKILL_DIR/scripts/x.py`)→ **不改 agent 配置**,agent 可用 skill 列表 / system prompt 即出现它。反例:新增后 agent 看不到。
  - *输出体积*:context 侧给 `run_command` 配 `resultStrategy:{shape:{kind:'tail',maxChars:N}}` → 进 LLM 的输出 ≤ N 字符且 replay 不变(`resultStrategy` 纯函数,现成复用,零新工作)。event-log 侧 `tool.responded.payload.output` 仍全量 → 大输出致 JSONL/CacheIndex 膨胀时再做截断/外置(**可延后存储优化,非正确性**)。
- **telegram 原生化(可证伪验收)**:全局 `provider: milkie` + demo_agent **显式**配 milkie(绕过自动回退)→ telegram 发一条消息 → 收到**文本回复**(不再 `register_skillkit` NotImplementedError、不再静默丢弃 turn)→ 让 agent 发一个文件 → telegram 实际收到该文件。反例(未达标):出现 NotImplementedError 或用户无回复。
- **去依赖(可证伪验收)**:
  - *结构边界(已达)*:`test_agent_provider_boundary` —— 主干(除 `provider/dolphin/**` + `infra/dolphin_compat.py`)`import dolphin` 零命中。
  - *import 解耦(已达)*:`test_dolphin_compat_optional` —— 模拟 dolphin 未安装时 `dolphin_compat` 仍可导入(`flags=None`、`ensure_continue_chat_compatibility` no-op、常量 fallback)。
  - *仍 gated(本 PR 不强拆)*:requirements 移除 dolphin 的前置 = (a) oneshot `call_llm` 中立化(`_extractor_helpers`/`compressor` 现经 `oneshot_llm_provider` 路由 dolphin)(b) 翻默认到 milkie。强拆会回退默认 dolphin 部署,故诚实标注为后续。

## 5. 当前进度

**已完成(本 session,TDD 全程红绿,alfred 全量 1585 passed)**:
- **alfred**:第0步 PoC、A1 run_turn、A2a/A2b context 收敛、A3 trajectory、A4 skillkit(接口收敛 100%)、C1/C2 MilkieProvider 走 `_progress`、C 端到端 + C3 配置开关、MilkieProvider 完整 AgentProvider 契约 + **context var 经 serve 端点跨进程**。
- **milkie**:serve 暴露 `/context/set·get·list`(#83)+ `/llm` 一次性 LLM + `/session/export·import`(#124,已 merge 进 main)端点。
- **端到端验证**:① config 切 milkie → turn_orchestrator → 真 serve → 同构 TurnEvent;② context var 跨进程往返(MilkieProvider ↔ 真 serve)。
- 提交:alfred(`0e5ef8d`…`e47f056`,分支 `feat/34-milkie-provider-poc`);milkie(`05c2169`,分支 `feat/86-milkie-serve`)。

**剩余(去 dolphin 依赖的前置)**:
- ~~`call_llm`~~ ✅ **已完成**:milkie #124(`/llm`)+ #126(`tier`+`temperature`)均交付;alfred `MilkieProvider.call_llm` 已对接(`fast`→tier、temperature、raise_on_error 双语义、HTTP 错误映射),含真 serve tier 路由跨进程 e2e。🔶 遗留:生产 spawn serve 时把 alfred config 的 default_model/fast_llm **写进 agent 文件两档 model**(目前仅 e2e 手写),属 sidecar 产品化范畴。
- ~~A2 会话持久化~~ ✅ **已完成**:milkie #128(`/session/history` 全量逐条历史)+ #130(serve `--state-store sqlite --data-dir` 持久化,重启恢复)交付;alfred `export_session` 接口收敛(5 处调用点 → DolphinProvider 行为不变)+ `MilkieProvider.export_session` 走 #128 翻译 canonical `Message[]`→alfred history;restore **不灌回**靠 serve 自持久化。**重启恢复 e2e 绿**(sqlite serve→run_turn 产历史→SIGTERM 重启→同 contextId 完整取回)。🔶 遗留:sidecar 启动前需 mkdir data-dir(SQLiteStore 不自建)。
- ~~`register_skillkit` / D Python skill 桥~~ ❌ **改写(2026-06-04 核查)**:不建跨语言桥。telegram(唯一编程式 skillkit、无 Python 本质)走 §4 **telegram 原生化**;其余 shell 型 skill 走 §4 **E milkie 能力层**(`run_command` + skill 发现)。
- 去 dolphin 依赖 — 依赖 E + telegram 原生化(不再依赖 milkie#87)。

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

## 6. 诚实现状:数据面 + 控制面(#3 daemon 集成)已通

> **⚠️ 2026-06-04 架构核查更正(取代本节下方"卡 milkie#87"的旧框架):**
> 经 alfred + milkie 双仓源码核查,本节原结论"剩下唯一硬卡点 = milkie#87 跨语言工具桥"**被推翻**:
> 1. **#87 不必建**:telegram 是唯一编程式 skillkit 且无 Python 本质;其余 skill 全是 shell 型 markdown(运行时无关)。见 §4 新增的 **telegram 原生化** 行。
> 2. **真正未列的关键路径 = E milkie 能力层**:milkie **当前没有** shell/exec 工具,也**没有** skill 目录发现(`skill_list` 是空 stub),所以那一堆 shell 型 markdown skill 在 milkie 下会"指令在、能力断"。这才是迁移的真实大头,此前完全没出现在路线图里。
> 3. **曾被担心的"确定性 replay vs 副作用 shell 工具"张力 = 不存在**:milkie record 真跑 / replay 全缓存、handler 在 replay 期绝不执行(`ExplodingInnerPort` 单测背书),副作用 shell 工具**零改造即 replay 安全**。
> 下方原文保留作历史轨迹;凡"待 milkie#87 / 唯一硬卡点 / D skill 桥"字样均以本横幅与 §4 新表为准。

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
- **已知 corner(milkie#87 前)**:若用户**显式**把 telegram-serving agent 配成 milkie(`everbot.agents.<name>.provider=milkie`,绕过自动回退),该 agent 收到消息时 `register_skillkit` 抛 NotImplementedError → turn 被 `_chat_worker` 捕获记 log、**静默丢弃**(不崩进程,但用户无回复)。自动回退路径(未显式声明)不受影响。建议后续加启动期 config 校验或 fail-loud 用户提示。
- `call_llm` 在 per-agent pool 模式(provider 经 `get_provider_for_agent` 构造,无固定 base_url)下 fail-loud;milkie 原生 call_llm 待按需设计。一次性 `call_llm`(记忆抽取/历史压缩)经 `oneshot_llm_provider()` 显式路由 dolphin(dolphin 进程内特性,milkie pool 无固定 serve)。

**PR review 接缝修复(#35 review 后,本轮补强)**:
- **运行/保存/变量/状态操作贯穿 per-agent provider**:新增 `provider_for(agent)`(按 agent 对象类型派发),替换**全部** agent 相关 `get_provider()`(turn_orchestrator/core_service/session/persistence/cron/heartbeat/skill_change_detector/context_manager + **web chat_service** 7 处 interrupt/resume/get_variable)。修复「创建路由对、操作回退全局 provider」的核心缺陷;dolphin 默认基线字节级不变。`needs_history_restore()` 守护亦按 agent 派发。
- **`MilkieAgentHandle` 加 `name`**:修 `chat_service`/`persistence` 的 `agent.name` AttributeError(milkie agent 首连/保存即崩)。
- **sidecar 就绪后持续 drain stdout**:防 pipe buffer 满导致子进程 write 阻塞(/chat 假死);`close()` 取消 drain task。
- 完备性扫描:全 src 已无遗漏的全局 `get_provider()` agent 操作。
- **sidecar start 失败不泄漏**:`pool.get_or_spawn` 在 `start()` 失败时 `await close()` 终止已 spawn 的子进程再 re-raise(ready 超时场景子进程已起来,旧码会留孤儿)。
- **milkie HTTP 非 2xx 明确抛错**:`/chat`(run_turn)、`/resume`、`/interrupt`、`/context/set·get` 非 2xx → 抛 `RuntimeError`/`HTTPStatusError`(原先 500/404 被静默吞 → core_service 显示「(无响应)」)。`core_service` 宽 except 捕获 → 记 error timeline + 发用户可见错误消息。`call_llm`(dual raise_on_error)/`export_session`(404→空)语义不变。
- 1680 unit 绿 + 6 真 e2e 绿。

**评估**:#3 daemon 集成本 session 已完整交付(sidecar 池/生命周期/创建路径/resume/裸 context 收敛),全程 dolphin 行为零变化(1658 unit 绿)+ 真子进程 e2e 验证;#4 路由与回退就位,milkie 可经配置端到端跑通。去硬依赖待 milkie#87。

## 7. milkie 侧依赖

- **milkie#86** 最小 `milkie serve`(HTTP+SSE,含 token 透传):✅ 已交付,alfred 验收通过(8/8 + 9/9 测试)。
- **milkie#124** serve 端点增补(`/llm` 一次性 LLM + `/session/export·import` portable session):✅ 已交付,merge 进 main(23 jest passed)。顺手落地了 #83 context 端点的孤儿提交。
- **milkie#126** `/llm` 加 `tier`(具名 model 档)+ `temperature`:✅ 已交付 merged,alfred `call_llm` 已对接。
- **milkie#128** `/session/history` by-context 全量逐条历史导出(含 tool chain):✅ 已交付 merged,alfred `export_session` 已对接。
- **milkie#130** serve 持久化 store(SQLite+Jsonl,sidecar 重启从 checkpoint 恢复):✅ 已交付 merged,重启恢复 e2e 已验证。
- ~~**milkie#87** 跨语言工具桥~~ ❌ **不做(2026-06-04 源码核查后删除)**:全 alfred 仅 1 个编程式 skillkit(telegram,2 方法)且无 Python 本质 → TS 原生重写 / 输出约定即可;其余 skill 全是 shell 型 markdown(运行时无关)。通用双向跨语言桥**无真实用户**,不建。改由 E 阶段(milkie 补 `run_command` + skill 发现)承接 skill 能力。
- milkie P0/P1(#80–#85)+ #124 均已 closed/merged。
