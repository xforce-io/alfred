# Alfred EverBot 设计方案

> **版本**: v1.3
> **创建时间**: 2026-02-01
> **更新时间**: 2026-06-27（按 milkie sidecar 真实架构重写，替换过时的 dolphin 描述）

---

## 目录

1. [概述与目标](#一概述与目标)
2. [核心概念](#二核心概念)
3. [会话与上下文管理](#三会话与上下文管理)
4. [Skills 管理（discover_skills）](#四skills-管理discover_skills)
5. [用户数据统一管理](#五用户数据统一管理)
6. [整体架构](#六整体架构)
7. [模块设计](#七模块设计)
8. [实现路线图](#八实现路线图)
9. [技术选型](#九技术选型)

---

## 一、概述与目标

### 1.1 背景

当前 Alfred 的 chat 能力通过 Web API (`/api/chat`) 以**请求-响应**模式提供，用户需要主动发起对话。我们希望将其改造为 **Ever Running Bot（持续运行机器人）**，使 Agent 具备：

- **持续运行**：通过 macOS `launchd` 作为后台服务运行
- **心跳机制**：定期自我唤醒，执行任务推进
- **管理前端（建议）**：提供轻量管理界面（状态/日志/任务）
- **记忆系统**：基于工作区的 Markdown + 向量检索

### 1.2 设计原则

1. **统一 agent runtime：milkie sidecar**
   - agent 执行核心为 milkie（TS/Node）。alfred 是 Python，无法进程内 `import`，故每个 agent 跑一个 `milkie serve` 子进程，经 HTTP + SSE 通信（跨进程 sidecar）
   - alfred 侧的 provider 抽象：`MilkieProvider`（唯一实现）+ `SidecarPool`（惰性 spawn + 常驻复用）+ `SidecarLauncher`（装配启动命令/env）
   - 技能经 `discover_skills` 扫工作区 `SKILL.md`，注入 prompt 技能段；agent 经 milkie 内建 `run_command` 跑脚本
   - 模型路由读 `config/models.yaml`（纯 YAML），不再依赖 dolphin factory / dolphin.yaml / global_config

2. **轻量化实现**
   - 不引入 WebSocket Gateway
   - 优先使用文件系统存储会话（兼容已有 JSONL 格式）
   - 后台服务采用单进程模型

3. **渐进式演进**
   - Phase 1: 心跳机制 + 后台服务
   - Phase 2: 工作区指令系统
   - Phase 3: 管理前端与可观测性（建议）

### 1.3 明确范围（Scope / Non-Goals）

**v1 范围内**：
- macOS `launchd` 后台服务
- 心跳驱动的任务执行
- Markdown 工作区指令（AGENTS.md, HEARTBEAT.md 等）
- Session 持久化与恢复

**v1 不做**：
- 不要求兼容旧版 `/api/chat` 协议
- 不兼容 `systemd`（仅 macOS）
- 不引入复杂网关与多通道插件体系

---

## 二、核心概念

### 2.1 术语对照

| 概念 | 说明 |
|------|------|
| EverBot Daemon | 后台服务进程 |
| milkie serve | 每个 agent 一个的 milkie 子进程，alfred 经 HTTP + SSE 驱动 |
| MilkieProvider | provider 中立能力 port（`AgentProvider`）的唯一实现，跨进程驱动 `milkie serve` |
| SidecarPool | 惰性 spawn + 常驻复用 per-agent sidecar 的池 |
| Session | 会话，由 milkie 的 `contextId` 标识，历史由 `milkie serve` 自持久化 |
| HeartbeatRunner | 心跳运行器，定时唤醒 Agent |
| Workspace | Agent 工作区，包含 Markdown 配置文件 |

### 2.2 核心组件关系

```
┌─────────────────────────────────────────────────────────────┐
│                    EverBot Daemon (常驻服务)                  │
│  ┌────────────┐   ┌────────────┐   ┌────────────┐          │
│  │ Heartbeat  │   │   Cron     │   │   Health   │          │
│  │  Runner    │   │  Scheduler │   │   Check    │          │
│  └─────┬──────┘   └─────┬──────┘   └────────────┘          │
│        │                │                                   │
│        ▼                ▼                                   │
│  ┌──────────────────────────────────────────────┐          │
│  │      Agent Runtime (MilkieProvider)          │          │
│  │  - SidecarPool（惰性 spawn + 常驻复用）      │          │
│  │  - SidecarLauncher（装配 milkie serve）      │          │
│  │  - turn_orchestrator（policy：重试/budget）  │          │
│  └──────────────┬───────────────────────────────┘          │
│                 │ HTTP + SSE                                │
│  ┌──────────────▼───────────────────────────────┐          │
│  │   milkie serve 子进程（per-agent sidecar）   │          │
│  │  - POST /chat（SSE 驱动一轮）/resume/...     │          │
│  │  - --state-store sqlite 自持久化历史         │          │
│  └──────────────────────────────────────────────┘          │
└─────────────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                       文件系统                               │
│  ~/.alfred/                                                 │
│  ├── config.yaml              # 主配置                       │
│  ├── config/models.yaml       # 模型路由（纯 YAML）         │
│  ├── agents/<name>/           # Agent 工作区                 │
│  │   ├── SOUL.md / AGENTS.md  # 人格 / 行为规范             │
│  │   ├── SKILLS.md            # 技能段                       │
│  │   ├── HEARTBEAT.md         # 心跳任务清单                 │
│  │   ├── MEMORY.md            # 长期记忆                     │
│  │   └── USER.md              # 用户画像                     │
│  ├── milkie/<name>/agent.md   # milkie agent 定义（fsm/model）│
│  └── logs/                    # 服务日志（含 traces/）       │
└─────────────────────────────────────────────────────────────┘
```

---

## 三、会话与上下文管理

### 3.1 会话身份与跨进程上下文

milkie 是事件溯源 runtime，会话身份 = **contextId**。alfred 经 `MilkieProvider` 跨进程读写
`milkie serve` 的会话状态，主要端点（均为 HTTP，`/chat`、`/resume` 的响应体即 SSE）：

| 端点 | 说明 |
|------|------|
| `POST /chat` | 驱动**一轮对话**（`run_turn`）。payload `{contextId, input, goal}`，响应体即 SSE 流 |
| `POST /resume` | 用户中断后**续跑**（SSE） |
| `POST /interrupt` | 中断当前运行 |
| `POST /context/set` / `POST /context/get` | 跨进程读写**会话变量** |
| `POST /context/state` | 查运行态（`paused`） |
| `POST /session/history` | 导出**全量 canonical 历史** `Message[]` |
| `POST /projection/attach` | 把已投递到 channel 的外部产出登记为 context projection（按 `sourceRunId` 去重） |
| `POST /llm` | **无状态**一次性 LLM 调用，payload 含 `tier`(default/fast)+`temperature` |

**关键差异**：dolphin 时代的进程内 `Context.set_variable($name)` / `.dph` 模板变量插值机制
**不存在**。alfred 不直接持有 agent 的内部 context；会话变量经 `/context/*` 端点跨进程读写，
对话历史由 `milkie serve` 自己持久化。

### 3.2 上下文注入路径

```
┌─────────────────────────────────────────────────────────────────┐
│              alfred 侧（MilkieProvider / turn_orchestrator）     │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ 1. system prompt（spawn 时一次性传给 milkie serve）       │   │
│  │    - WorkspaceLoader 读 SOUL/AGENTS/SKILLS/USER/MEMORY.md │   │
│  │      合成，落入 sidecar 的 agent.md `systemPrompt`        │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ 2. input（每轮经 POST /chat 传入）                        │   │
│  │    - 易变内容（mailbox 事件、due tasks）前置注入 input    │   │
│  │    - 保持 system prompt 稳定，prefix cache 友好           │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ 3. 会话变量（POST /context/set/get）                      │   │
│  │    - 需跨进程持有的运行态变量                              │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### 3.3 milkie agent 定义

milkie agent 的 prompt 由其 **`agent.md`** 的 `systemPrompt`/FSM 决定（不是 per-turn override）。
sidecar 的 agent.md 落在 `~/.alfred/milkie/<agent>/agent.md`（含 fsm/model/models 字段）。

alfred 侧由 `infra/workspace.py` 的 `WorkspaceLoader` 读 agent 工作区
`~/.alfred/agents/<name>/` 的 **SOUL/AGENTS/SKILLS/USER/MEMORY.md** 合成 system prompt，
spawn 时传给 `milkie serve`（替代了 dolphin 的 AgentFactory + `$workspace_instructions` 注入；
dolphin 语法的 `.dph` 模板与变量插值已不存在）。

```python
# WorkspaceLoader 合成 system prompt（落入 sidecar agent.md 的 systemPrompt）
system_prompt = workspace_loader.build_system_prompt()  # SOUL/AGENTS/SKILLS/USER/MEMORY.md

# per-agent 模型：everbot.agents.<name>.model > everbot.default_model
# 模型路由读 config/models.yaml，经 model_config.load_model_config 定位
```

### 3.4 Heartbeat 注入机制

#### 3.4.1 注入策略

心跳触发时，心跳指令进入 system prompt（spawn 时落入 sidecar agent.md），任务清单作为
**input** 经 `POST /chat` 注入触发本轮执行：

```python
class HeartbeatRunner:
    """心跳运行器"""

    HEARTBEAT_SYSTEM_INSTRUCTION = """
## 心跳模式

你正在执行定期心跳检查。请查看任务清单，执行需要推进的任务。

### 行为规则
1. 如果任务清单为空或所有任务已完成，回复：HEARTBEAT_OK
2. 如果有任务需要执行，立即开始执行
3. 执行完成后，简要汇报结果
4. 不要询问用户确认，直接行动
"""

    def build_heartbeat_input(
        self,
        heartbeat_content: str,
    ) -> str:
        """
        构造心跳触发 input（经 POST /chat 的 `input` 字段传给 sidecar）

        心跳系统指令由 ContextStrategy 拼入 system prompt（稳定，prefix cache 友好）；
        每次到期的任务清单走 input 注入（易变）。

        Returns:
            构造好的 input 文本，用于触发本轮执行
        """
        user_message = f"""
[系统心跳 - {datetime.now().strftime('%Y-%m-%d %H:%M')}]

## 任务清单

{heartbeat_content}

---

请检查上述任务清单，如有需要推进的任务请立即执行。
如无待办任务，回复"HEARTBEAT_OK"。
"""
        return user_message
```

#### 3.4.2 Session 策略选择

提供两种模式，通过配置选择：

| 模式 | 配置值 | 说明 | 适用场景 |
|------|--------|------|----------|
| **共享模式** | `heartbeat.session_mode: shared` | 心跳复用用户 Session | 需要心跳感知对话上下文 |
| **独立模式** | `heartbeat.session_mode: isolated` | 心跳使用独立 Session | 不希望心跳污染用户对话 |

**默认推荐：独立模式**，通过 MEMORY.md 共享必要信息，避免心跳消息污染用户对话历史。

### 3.5 History 管理（milkie 自持久化）

#### 3.5.1 核心原则

**对话历史由 `milkie serve` 自持久化**（`--state-store sqlite --data-dir <…>`），alfred 不再
进程内直接操作历史列表。需要读取 canonical 历史时经 `POST /session/history` 导出 `Message[]`。

dolphin 时代「alfred 把存档历史灌回 agent」的恢复链路已不存在：同 contextId 跨 daemon 重启
**自动从 checkpoint 恢复** → `needs_history_restore() == False`。归档/裁剪策略（如把陈旧历史
沉淀到 MEMORY.md）改为基于 `/session/history` 导出后在 alfred 侧做摘要，而非回写 milkie 内部历史。

#### 3.5.2 History 归档实现

```python
class HistoryManager:
    """History 归档器 - 基于 POST /session/history 导出"""

    MAX_HISTORY_ROUNDS = 10  # 触发归档的轮次阈值

    def __init__(self, memory_path: Path):
        self.memory_path = memory_path

    async def archive_if_needed(self, provider: "MilkieProvider", context_id: str) -> bool:
        """
        导出 canonical 历史，超阈值则把陈旧片段归档到 MEMORY.md

        历史由 milkie serve 自持久化，alfred 只读不回写；归档是 alfred 侧的记忆沉淀。

        Returns:
            是否执行了归档
        """
        history = await provider.export_history(context_id)  # POST /session/history

        max_messages = self.MAX_HISTORY_ROUNDS * 2  # user + assistant

        if len(history) <= max_messages:
            return False

        # 提取要归档的消息并沉淀到 MEMORY.md
        archived_messages = history[:-max_messages]
        self._archive_to_memory(archived_messages)

        logger.info(f"归档历史: 沉淀 {len(archived_messages)} 条到 MEMORY.md")
        return True

    def _archive_to_memory(self, messages: List[Dict]):
        """将消息归档到 MEMORY.md"""
        if not messages:
            return

        # 格式化为摘要
        summary_lines = ["", "---", "", f"## 历史对话归档 ({datetime.now().strftime('%Y-%m-%d %H:%M')})", ""]

        for msg in messages:
            role = "用户" if msg.get("role") == "user" else "助手"
            content = msg.get("content", "")[:200]  # 截断
            if len(msg.get("content", "")) > 200:
                content += "..."
            summary_lines.append(f"**{role}**: {content}")
            summary_lines.append("")

        # 追加到 MEMORY.md
        with open(self.memory_path, "a", encoding="utf-8") as f:
            f.write("\n".join(summary_lines))
```

### 3.6 Session 持久化与恢复

**职责分工**：对话历史的持久化/恢复由 `milkie serve` 自己负责（sqlite/jsonl）；alfred 侧只持有
轻量的会话元数据（session_id 到 contextId 的映射、mailbox、revision 等运行态），不再保存/回灌
完整 history_messages。

#### 3.6.1 Session 元数据结构

```python
@dataclass
class SessionData:
    """Session 元数据（不含完整历史，历史由 milkie serve 自持久化）"""
    session_id: str
    agent_name: str
    context_id: str               # milkie 会话身份；历史按此恢复
    mailbox: List[Dict]           # 待消费的背景事件
    created_at: str
    updated_at: str

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> "SessionData":
        return cls(**data)
```

#### 3.6.2 会话身份对齐（contextId）

无需把历史灌回 agent。关键是把 contextId 对齐到稳定的 alfred `session_id`，否则每次重启会生成
新随机 contextId、旧历史成孤儿：

```python
class SessionManager:
    """会话管理：把 contextId 锚定到稳定 session_id，保证跨重启连续性"""

    async def acquire_sidecar(self, session_id: str):
        """从 SidecarPool 取（或惰性 spawn）该 agent 的 sidecar"""
        sidecar = await self.pool.get_or_spawn(self.agent_name)

        # 把 milkie contextId 对齐到稳定的 alfred session_id
        # 同 contextId 跨 daemon 重启会自动从 checkpoint 恢复历史
        provider.set_session_id(session_id)   # → contextId
        return sidecar
```

**临时路径（reflector / cron）**保留随机 contextId 做会话隔离，不对齐稳定 session_id。

> 历史遗留：`~/.alfred/agents/<name>/agent.dph` 文件可能仍存在，但 dolphin 的
> AgentFactory / AgentRuntime / Env / DolphinAgent / agent.dph 解析链路已不存在；
> agent 配置以「工作区 Markdown + milkie agent.md」为准。`infra/dolphin_compat.py`
> 是有意保留的兼容模块（纯字符串常量 `KEY_HISTORY*` + no-op `ensure_*_compatibility`），
> 不 import dolphin。

### 3.7 并发控制

为避免心跳与用户对话的竞态条件，使用 Session 级别的锁：

```python
class SessionManager:
    """Session 管理器 - 带并发控制"""

    def __init__(self, persistence: SessionPersistence, pool: "SidecarPool"):
        self.persistence = persistence
        self.pool = pool                             # per-agent sidecar 复用
        self._locks: Dict[str, asyncio.Lock] = {}   # Session 锁

    def _get_lock(self, session_id: str) -> asyncio.Lock:
        """获取 Session 锁（懒创建）"""
        if session_id not in self._locks:
            self._locks[session_id] = asyncio.Lock()
        return self._locks[session_id]

    async def acquire_session(self, session_id: str, timeout: float = 30.0) -> bool:
        """
        获取 Session 锁

        Args:
            session_id: 会话 ID
            timeout: 超时时间（秒）

        Returns:
            是否成功获取锁
        """
        lock = self._get_lock(session_id)
        try:
            await asyncio.wait_for(lock.acquire(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            logger.warning(f"获取 Session 锁超时: {session_id}")
            return False

    def release_session(self, session_id: str):
        """释放 Session 锁"""
        lock = self._get_lock(session_id)
        if lock.locked():
            lock.release()

    @asynccontextmanager
    async def session_context(self, session_id: str, timeout: float = 30.0):
        """
        Session 上下文管理器

        Usage:
            async with session_manager.session_context("session_123") as acquired:
                if acquired:
                    # 执行操作
                else:
                    # 处理锁获取失败
        """
        acquired = await self.acquire_session(session_id, timeout)
        try:
            yield acquired
        finally:
            if acquired:
                self.release_session(session_id)
```

**使用示例**：

```python
# 心跳执行时
async def run_heartbeat(self, session_id: str):
    async with self.session_manager.session_context(session_id, timeout=5.0) as acquired:
        if not acquired:
            logger.info(f"Session {session_id} 被占用，跳过本次心跳")
            return "HEARTBEAT_SKIPPED"

        # 执行心跳逻辑（从 SidecarPool 取/惰性 spawn 该 agent 的 sidecar）
        sidecar = await self.session_manager.acquire_sidecar(session_id)
        result = await self._execute_heartbeat(sidecar)
        return result
```

---

## 四、Skills 管理（discover_skills）

### 4.1 技能体系

milkie **没有** dolphin 的 `ResourceSkillkit` / 编程式 skillkit 注册。技能改为基于工作区
`SKILL.md` 文件的发现与执行：

| 阶段 | 机制 | 说明 |
|------|------|------|
| **发现** | `discover_skills`（`milkie/skills.py`） | 扫工作区 `SKILL.md` → 产出 `skill_list` manifest |
| **注入** | `build_milkie_skills_section` | 把技能段拼入 system prompt |
| **执行** | milkie 内建 `run_command` | agent 读 `SKILL.md` 正文并跑脚本，与 dolphin 能力对等 |

### 4.2 配置示例

per-agent allowlist 通过 alfred 配置控制（不再有 dolphin.yaml）：

```yaml
# config.yaml
everbot:
  agents:
    <agent_name>:
      skills:
        include: ["web-search", "routine-manager"]
        exclude: []
```

**技能指纹**（过滤后 name/title/description/abs_path 的 sha256）变化时，`SidecarPool` 只重生
该 agent 的 sidecar，免全量重启 daemon。

### 4.3 技能目录结构

```
~/.alfred/
├── skills/                          # 全局共享技能
│   └── web-search/
│       └── SKILL.md
│
└── agents/<agent_name>/
    └── skills/                      # Agent 专属技能
        └── custom-skill/
            └── SKILL.md
```

---

## 五、用户数据统一管理

### 5.1 目录结构

```
~/.alfred/                           # ALFRED_HOME
├── config.yaml                      # 主配置
├── config/models.yaml               # 模型路由（纯 YAML）
│
├── skills/                          # 全局共享技能
│
├── agents/                          # Agent 工作区
│   └── <agent_name>/
│       ├── SOUL.md                  # 人格
│       ├── AGENTS.md                # 行为规范
│       ├── SKILLS.md                # 技能段
│       ├── config.yaml              # Agent 配置
│       ├── HEARTBEAT.md             # 心跳任务
│       ├── MEMORY.md                # 长期记忆
│       ├── USER.md                  # 用户画像
│       └── skills/                  # Agent 专属技能
│
├── milkie/                          # milkie sidecar 数据
│   └── <agent_name>/
│       ├── agent.md                 # milkie agent 定义（systemPrompt/fsm/model）
│       └── <data-dir>/              # serve 自持久化历史（sqlite/jsonl）
│
├── sessions/                        # 会话元数据存储
│   ├── <session_id>.json            # Session 元数据（含 contextId）
│   └── metadata.json                # 索引
│
└── logs/                            # 服务日志
    ├── everbot.log
    ├── heartbeat.log
    └── traces/                      # milkie runId HTML 报告
```

### 5.2 UserDataManager

```python
class UserDataManager:
    """用户数据统一管理器"""

    def __init__(self, alfred_home: Path = None):
        self.alfred_home = alfred_home or Path("~/.alfred").expanduser()

    # --- 路径属性 ---

    @property
    def config_path(self) -> Path:
        return self.alfred_home / "config.yaml"

    @property
    def agents_dir(self) -> Path:
        return self.alfred_home / "agents"

    @property
    def sessions_dir(self) -> Path:
        return self.alfred_home / "sessions"

    @property
    def logs_dir(self) -> Path:
        return self.alfred_home / "logs"

    # --- Agent 管理 ---

    def get_agent_dir(self, agent_name: str) -> Path:
        return self.agents_dir / agent_name

    def list_agents(self) -> List[str]:
        """列出所有 Agent"""
        if not self.agents_dir.exists():
            return []
        return [d.name for d in self.agents_dir.iterdir()
                if d.is_dir() and (d / "agent.dph").exists()]

    def get_workspace_files(self, agent_name: str) -> Dict[str, Optional[str]]:
        """获取 Agent 工作区文件内容"""
        agent_dir = self.get_agent_dir(agent_name)
        files = {}
        for filename in ["AGENTS.md", "HEARTBEAT.md", "MEMORY.md", "USER.md"]:
            file_path = agent_dir / filename
            files[filename] = file_path.read_text() if file_path.exists() else None
        return files

    # --- 初始化 ---

    def ensure_directories(self):
        """确保必要目录存在"""
        for dir_path in [self.agents_dir, self.sessions_dir, self.logs_dir]:
            dir_path.mkdir(parents=True, exist_ok=True)

    def init_agent_workspace(self, agent_name: str):
        """初始化 Agent 工作区"""
        agent_dir = self.get_agent_dir(agent_name)
        agent_dir.mkdir(parents=True, exist_ok=True)

        # 创建默认文件
        templates = {
            "AGENTS.md": f"# {agent_name} 行为规范\n\n## 核心职责\n\n（待补充）\n",
            "HEARTBEAT.md": "# 心跳任务\n\n（暂无任务）\n",
            "MEMORY.md": "# 长期记忆\n\n（暂无记录）\n",
            "USER.md": "# 用户画像\n\n（待补充）\n",
        }

        for filename, content in templates.items():
            file_path = agent_dir / filename
            if not file_path.exists():
                file_path.write_text(content)
```

### 5.3 配置优先级

```
环境变量 > Agent 配置 > 全局配置 > 默认值

示例：
1. ALFRED_MODEL_NAME 环境变量
2. ~/.alfred/agents/<name>/config.yaml 中的 model_name
3. ~/.alfred/config.yaml 中的 model_name
4. 内置默认值 "gpt-4"
```

---

## 六、整体架构

### 6.1 消息流程

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   用户 CLI  │     │   Web API   │     │  Heartbeat  │
└──────┬──────┘     └──────┬──────┘     └──────┬──────┘
       │                   │                   │
       └───────────────────┼───────────────────┘
                           │
                           ▼
              ┌────────────────────────┐
              │    EverBot Dispatcher  │
              │    (消息路由 + 锁管理)  │
              └───────────┬────────────┘
                          │
         ┌────────────────┼────────────────┐
         ▼                ▼                ▼
   ┌───────────┐   ┌───────────┐   ┌───────────┐
   │ Session A │   │ Session B │   │ Session C │
   │  (locked) │   │  (free)   │   │ (locked)  │
   └─────┬─────┘   └─────┬─────┘   └─────┬─────┘
         │               │               │
         ▼               ▼               ▼
   ┌───────────┐   ┌───────────┐   ┌───────────┐
   │  Agent A  │   │  Agent B  │   │  Agent C  │
   └───────────┘   └───────────┘   └───────────┘
```

### 6.2 心跳执行流程

```
定时触发
    │
    ▼
┌──────────────────────────────────────────────────┐
│  1. 检查活跃时段                                   │
│     if not is_active_time(): return              │
└──────────────────────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────────────────────┐
│  2. 读取 HEARTBEAT.md                             │
│     if empty or no tasks: return HEARTBEAT_OK    │
└──────────────────────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────────────────────┐
│  3. 获取 Session 锁                               │
│     if not acquired: return HEARTBEAT_SKIPPED    │
└──────────────────────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────────────────────┐
│  4. 获取 Sidecar                                  │
│     - SidecarPool 取/惰性 spawn                   │
│     - contextId 对齐稳定 session_id               │
└──────────────────────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────────────────────┐
│  5. 构造 Heartbeat Input                          │
│     - 心跳指令在 system prompt（agent.md）        │
│     - 任务清单走 input（POST /chat）             │
└──────────────────────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────────────────────┐
│  6. 执行一轮（run_turn）                          │
│     - POST /chat (SSE) → _progress               │
│     - 收集响应                                    │
└──────────────────────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────────────────────┐
│  7. 后处理                                        │
│     - 历史由 milkie serve 自持久化               │
│     - 归档 MEMORY.md（如需要）                    │
│     - 持久化 Session 元数据                       │
│     - 释放锁                                      │
│     - 记录日志                                    │
└──────────────────────────────────────────────────┘
    │
    ▼
返回结果
```

### 6.3 任务执行模型

> **v1.2 新增**：v1.1 中所有任务统一通过 Agent turn 执行。随着系统演进，引入了
> 结构化任务清单、Python Job 模块和 CronExecutor，需要重新定义任务分类和执行语义。

#### 6.3.1 两个正交维度

任务执行由两个独立维度决定：

| 维度 | 选项 | 决定什么 |
|------|------|----------|
| **执行方式** | `job`（Python 模块）/ `agent`（LLM turn） | **怎么执行**：调 Python 还是调 LLM |
| **上下文模式** | `inline` / `isolated` | **在哪执行**：主 session 分支内，还是独立分支 |

这两个维度是**正交**的，不应耦合：

|  | inline | isolated |
|--|--------|----------|
| **job** | Python 模块在 heartbeat 周期内同步执行 | Python 模块独立执行，结果通过 delivery pipeline 交付 |
| **agent** | Agent turn 在主 session 上下文中执行 | Agent turn 在独立 session 分支中执行 |

#### 6.3.2 执行方式：Job vs Agent

**Job 任务**（`task.job` 非空）：

- 有对应的 Python 模块（`everbot.core.jobs.<module_name>`）
- 通过 `_invoke_job()` → `module.run(context: SkillContext)` 直接调用
- 不参与 Agent 上下文体系，通过 `SkillContext` 获取所需资源
- 适合：逻辑确定的系统任务，内部可按需调用 `context.llm`

**Agent 任务**（`task.job` 为空）：

- 通过 milkie sidecar 的一轮对话（`POST /chat` SSE）执行，LLM 自主推理 + 调用工具
- 需要 Agent 上下文（system prompt、对话历史、工具集）
- 适合：开放性任务，需要 LLM 理解意图和自主执行

**判定规则**：`task.job` 字段决定执行方式，与 `execution_mode` 无关。

#### 6.3.3 上下文模式：Inline vs Isolated

`execution_mode` 控制任务的**上下文分支和结果交付方式**：

**Inline**：

- 在主 heartbeat session 的上下文中执行
- Agent 任务：共享主 session 对话历史，执行过程留在主 session
- Job 任务：在 heartbeat 周期内同步执行，结果作为 `TaskResult` 返回
- 适合：轻量任务、需要感知主 session 上下文的任务

**Isolated**：

- 创建独立的执行上下文，与主 session 分离
- Agent 任务：fork 独立 agent session（干净 system prompt + 对话历史）
- Job 任务：独立执行，不阻塞 heartbeat 主流程
- 结果通过 delivery pipeline 回流（mailbox → inject_to_history → realtime push）
- 中间过程不污染主 session
- 适合：耗时任务、多轮交互任务、产生大量中间上下文的任务

#### 6.3.4 执行流程

```
Heartbeat Cycle
│
├── Due Tasks
│   │
│   ├── Inline (按序执行，在主 session 内)
│   │   ├── job  → _invoke_job() → Python module.run(context)
│   │   ├── deterministic → programmatic output (e.g. 报时)
│   │   └── agent → inject context → run_agent() in primary session
│   │
│   └── Isolated (逐个执行，独立上下文)
│       ├── job  → _invoke_job() → Python module.run(context) → delivery
│       └── agent → create session → run_agent() → delivery
│
└── Inspector / Reflection (主 session 上下文)
```

#### 6.3.5 内置 Job

Inspector 自动注册到 HEARTBEAT.md 的系统 Job：

| Job | 模块 | execution_mode | scanner | 说明 |
|-----|------|----------------|---------|------|
| memory-review | `jobs/memory_review.py` | inline | session | 检查近期对话，更新 MEMORY.md |
| task-discover | `jobs/task_discover.py` | inline | session | 从对话中发现新的定时任务意图 |
| skill-evaluate | `jobs/skill_evaluate.py` | **isolated** | — | 评估技能执行效果（内部调 LLM Judge） |

> **注**：memory-review 和 task-discover 为轻量 inline Job，在 heartbeat 周期内同步完成。
> skill-evaluate 内部通过 `context.llm` 调用 LLM Judge，耗时较长，使用 isolated
> 避免阻塞 heartbeat 主循环。isolated + job 走 `_invoke_job()` 执行 Python 模块，
> 不创建 Agent session。

#### 6.3.6 HEARTBEAT.md 任务格式

> v1.1 中 HEARTBEAT.md 为纯 Markdown checklist。v1.2 改为 JSON 结构化格式。

```json
{
  "version": 2,
  "tasks": [
    {
      "id": "routine_<hash>",
      "title": "任务标题",
      "description": "任务描述",
      "enabled": true,
      "schedule": "2h",
      "timezone": "Asia/Shanghai",
      "execution_mode": "inline|isolated",
      "job": "module-name|null",
      "scanner": "session|null",
      "state": "pending|running|done|failed",
      "last_run_at": "ISO8601",
      "next_run_at": "ISO8601",
      "timeout_seconds": 180,
      "retry": 0,
      "max_retry": 3
    }
  ]
}
```

关键字段语义：

| 字段 | 说明 |
|------|------|
| `execution_mode` | 上下文模式，控制分支策略和结果交付 |
| `job` | Python 模块名。非空时走 `_invoke_job()`，为空时走 Agent turn |
| `scanner` | 前置扫描器（如 `session`），检查是否有新数据需要处理 |
| `schedule` | 调度表达式（`2h` / `1d` / cron 表达式） |

---

## 七、模块设计

### 7.1 目录结构

```
src/everbot/
├── __init__.py
├── daemon.py           # 守护进程主逻辑
├── heartbeat.py        # 心跳运行器
├── workspace.py        # 工作区加载
├── session.py          # Session 管理
├── history.py          # History 管理
├── config.py           # 配置管理
├── user_data.py        # 用户数据管理
└── cli.py              # CLI 命令
```

### 7.2 HeartbeatRunner

```python
# src/everbot/heartbeat.py

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable, Any
import logging

logger = logging.getLogger(__name__)


class HeartbeatRunner:
    """
    心跳运行器

    定期唤醒 Agent 检查任务清单并执行。
    """

    HEARTBEAT_SYSTEM_INSTRUCTION = """
## 心跳模式

你正在执行定期心跳检查。请查看任务清单，执行需要推进的任务。

### 行为规则
1. 如果任务清单为空或所有任务已完成，回复：HEARTBEAT_OK
2. 如果有任务需要执行，立即开始执行
3. 执行完成后，简要汇报结果
4. 不要询问用户确认，直接行动
"""

    def __init__(
        self,
        agent_name: str,
        workspace_path: Path,
        session_manager: "SessionManager",
        interval_minutes: int = 30,
        active_hours: tuple[int, int] = (8, 22),
        max_retries: int = 3,
        on_result: Optional[Callable[[str, str], Any]] = None,
    ):
        self.agent_name = agent_name
        self.workspace_path = Path(workspace_path)
        self.session_manager = session_manager
        self.interval_minutes = interval_minutes
        self.active_hours = active_hours
        self.max_retries = max_retries
        self.on_result = on_result

        self._running = False
        self._last_result: Optional[str] = None

    @property
    def session_id(self) -> str:
        """心跳使用独立 Session"""
        return f"heartbeat_{self.agent_name}"

    def _is_active_time(self) -> bool:
        """检查是否在活跃时段"""
        hour = datetime.now().hour
        start, end = self.active_hours
        return start <= hour < end

    def _read_heartbeat_md(self) -> Optional[str]:
        """读取 HEARTBEAT.md"""
        path = self.workspace_path / "HEARTBEAT.md"
        if not path.exists():
            return None

        content = path.read_text().strip()
        # 过滤仅有标题或注释的情况
        lines = [l for l in content.split('\n')
                 if l.strip() and not l.strip().startswith('#')]
        return content if lines else None

    def _should_skip_response(self, response: str) -> bool:
        """判断是否静默处理"""
        if "HEARTBEAT_OK" in response:
            return True
        if response == self._last_result:
            return True
        return False

    async def _execute_with_retry(self) -> str:
        """带重试的执行逻辑"""
        last_error = None

        for attempt in range(self.max_retries):
            try:
                return await self._execute_once()
            except Exception as e:
                last_error = e
                logger.warning(f"心跳执行失败 (尝试 {attempt + 1}/{self.max_retries}): {e}")
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(5 * (attempt + 1))  # 递增等待

        raise last_error

    async def _execute_once(self) -> str:
        """执行一次心跳"""
        # 1. 获取 Session 锁
        async with self.session_manager.session_context(self.session_id, timeout=5.0) as acquired:
            if not acquired:
                return "HEARTBEAT_SKIPPED"

            # 2. 取/惰性 spawn 该 agent 的 sidecar（contextId 对齐 session_id）
            sidecar = await self.session_manager.acquire_sidecar(self.session_id)

            # 3. 读取任务清单
            heartbeat_content = self._read_heartbeat_md()
            if not heartbeat_content:
                return "HEARTBEAT_OK"

            # 4. 构造心跳触发 input（心跳指令在 system prompt，任务清单走 input）
            heartbeat_input = self._build_heartbeat_input(heartbeat_content)

            # 5. 执行（POST /chat 驱动一轮，历史由 milkie serve 自持久化）
            result = await self._run_turn(sidecar, heartbeat_input)

            return result

    def _build_heartbeat_input(self, heartbeat_content: str) -> str:
        """构造心跳触发 input"""
        return f"""
[系统心跳 - {datetime.now().strftime('%Y-%m-%d %H:%M')}]

## 任务清单

{heartbeat_content}

---

请检查任务清单并执行。如无待办，回复"HEARTBEAT_OK"。
"""

    async def _run_turn(self, sidecar, input_text: str) -> str:
        """驱动一轮对话：MilkieProvider.run_turn → SSE → 中立 _progress → turn_orchestrator"""
        result = ""
        # turn_orchestrator 在中立 _progress 流上套 policy（重试 / tool budget / 循环检测）
        async for event in self.provider.run_turn(sidecar, input=input_text):
            if "_progress" in event:
                for progress in event["_progress"]:
                    if progress.get("stage") == "llm":
                        delta = progress.get("delta", "")
                        if delta:
                            result += delta
        return result

    async def run_once(self):
        """执行一次心跳（带前置检查）"""
        if not self._is_active_time():
            logger.debug(f"[{self.agent_name}] 非活跃时段，跳过")
            return

        logger.info(f"[{self.agent_name}] 开始心跳")

        try:
            result = await self._execute_with_retry()

            if self._should_skip_response(result):
                logger.debug(f"[{self.agent_name}] 静默响应")
            else:
                self._last_result = result
                logger.info(f"[{self.agent_name}] 心跳结果: {result[:100]}...")

                if self.on_result:
                    await self.on_result(self.agent_name, result)

        except Exception as e:
            logger.error(f"[{self.agent_name}] 心跳失败: {e}")

    async def start(self):
        """启动心跳循环"""
        self._running = True
        logger.info(f"[{self.agent_name}] 心跳启动，间隔 {self.interval_minutes} 分钟")

        while self._running:
            await self.run_once()
            await asyncio.sleep(self.interval_minutes * 60)

    def stop(self):
        """停止心跳"""
        self._running = False
        logger.info(f"[{self.agent_name}] 心跳已停止")
```

### 7.3 WorkspaceLoader

```python
# src/everbot/workspace.py

from pathlib import Path
from typing import Optional
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


@dataclass
class WorkspaceInstructions:
    """工作区指令集"""
    agents_md: Optional[str] = None
    user_md: Optional[str] = None
    memory_md: Optional[str] = None
    heartbeat_md: Optional[str] = None


class WorkspaceLoader:
    """
    工作区加载器

    从 Agent 工作区读取 Markdown 配置文件。
    """

    INSTRUCTION_FILES = {
        'agents_md': 'AGENTS.md',
        'user_md': 'USER.md',
        'memory_md': 'MEMORY.md',
        'heartbeat_md': 'HEARTBEAT.md',
    }

    def __init__(self, workspace_path: Path):
        self.workspace_path = Path(workspace_path)

    def _read_file(self, filename: str) -> Optional[str]:
        """读取单个文件"""
        file_path = self.workspace_path / filename
        if file_path.exists():
            content = file_path.read_text()
            logger.debug(f"加载 {filename} ({len(content)} 字符)")
            return content
        return None

    def load(self) -> WorkspaceInstructions:
        """加载所有指令文件"""
        instructions = WorkspaceInstructions()
        for attr, filename in self.INSTRUCTION_FILES.items():
            setattr(instructions, attr, self._read_file(filename))
        return instructions

    def build_system_prompt(self) -> str:
        """
        构建系统提示

        将工作区文件内容组合为系统提示的一部分。
        """
        instructions = self.load()
        parts = []

        if instructions.agents_md:
            parts.append(f"# 行为规范\n\n{instructions.agents_md}")

        if instructions.user_md:
            parts.append(f"# 用户画像\n\n{instructions.user_md}")

        if instructions.memory_md:
            # 仅包含 MEMORY.md 的摘要部分（避免过长）
            memory_lines = instructions.memory_md.split('\n')[:50]
            parts.append(f"# 历史记忆\n\n" + '\n'.join(memory_lines))

        if not parts:
            return ""

        return "\n\n---\n\n".join(parts)
```

### 7.4 EverBotDaemon

```python
# src/everbot/daemon.py

import asyncio
import signal
import logging
from pathlib import Path
from typing import Dict, Optional
from datetime import datetime

from .heartbeat import HeartbeatRunner
from .session import SessionManager
from .user_data import UserDataManager
from .config import load_config

logger = logging.getLogger(__name__)


class EverBotDaemon:
    """
    EverBot 守护进程

    管理多个 Agent 的心跳和生命周期。
    """

    def __init__(self, config_path: Optional[str] = None):
        self.config = load_config(config_path)
        self.user_data = UserDataManager()
        self.session_manager: Optional[SessionManager] = None
        self.heartbeat_runners: Dict[str, HeartbeatRunner] = {}
        self._running = False

    async def _init_components(self):
        """初始化组件"""
        self.user_data.ensure_directories()
        self.session_manager = SessionManager(self.user_data.sessions_dir)
        logger.info("组件初始化完成")

    async def _on_heartbeat_result(self, agent_name: str, result: str):
        """心跳结果回调"""
        # TODO: 可以在这里实现通知、日志聚合等
        log_file = self.user_data.logs_dir / "heartbeat.log"
        with open(log_file, "a") as f:
            f.write(f"[{datetime.now().isoformat()}] [{agent_name}] {result[:200]}\n")

    def _create_heartbeat_runners(self):
        """为配置的 Agent 创建心跳运行器"""
        agents_config = self.config.get("everbot", {}).get("agents", {})

        for agent_name, agent_config in agents_config.items():
            heartbeat_config = agent_config.get("heartbeat", {})
            if not heartbeat_config.get("enabled", False):
                continue

            workspace_path = Path(agent_config.get(
                "workspace",
                f"~/.alfred/agents/{agent_name}"
            )).expanduser()

            runner = HeartbeatRunner(
                agent_name=agent_name,
                workspace_path=workspace_path,
                session_manager=self.session_manager,
                interval_minutes=heartbeat_config.get("interval", 30),
                active_hours=tuple(heartbeat_config.get("active_hours", [8, 22])),
                on_result=self._on_heartbeat_result,
            )
            self.heartbeat_runners[agent_name] = runner
            logger.info(f"注册心跳: {agent_name}")

    async def start(self):
        """启动守护进程"""
        self._running = True
        logger.info("EverBot 守护进程启动")

        await self._init_components()
        self._create_heartbeat_runners()

        # 启动所有心跳
        tasks = [runner.start() for runner in self.heartbeat_runners.values()]

        if tasks:
            await asyncio.gather(*tasks)
        else:
            logger.info("无心跳任务，等待...")
            while self._running:
                await asyncio.sleep(60)

    async def stop(self):
        """停止守护进程"""
        self._running = False
        for runner in self.heartbeat_runners.values():
            runner.stop()
        logger.info("EverBot 守护进程已停止")

    def health_check(self) -> Dict:
        """健康检查"""
        return {
            "status": "running" if self._running else "stopped",
            "timestamp": datetime.now().isoformat(),
            "agents": list(self.heartbeat_runners.keys()),
        }


async def main():
    """CLI 入口"""
    import argparse

    parser = argparse.ArgumentParser(description="EverBot Daemon")
    parser.add_argument("--config", type=str, help="配置文件路径")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    daemon = EverBotDaemon(config_path=args.config)

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(daemon.stop()))

    await daemon.start()


if __name__ == "__main__":
    asyncio.run(main())
```

### 7.5 配置示例

```yaml
# ~/.alfred/config.yaml

everbot:
  enabled: true

  # Agent 配置
  agents:
    daily_insight:
      workspace: ~/.alfred/agents/daily_insight
      heartbeat:
        enabled: true
        interval: 30          # 分钟
        active_hours: [8, 22] # 活跃时段
        session_mode: isolated # isolated | shared

    research:
      workspace: ~/.alfred/agents/research
      heartbeat:
        enabled: false

# 日志配置
logging:
  level: INFO
  file: ~/.alfred/logs/everbot.log
```

### 7.6 LaunchAgent 配置

```xml
<!-- ~/Library/LaunchAgents/com.alfred.everbot.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.alfred.everbot</string>

    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/python3</string>
        <string>-m</string>
        <string>src.everbot.daemon</string>
        <string>--config</string>
        <string>~/.alfred/config.yaml</string>
    </array>

    <key>WorkingDirectory</key>
    <string>/path/to/alfred</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>~/.alfred/logs/everbot.out.log</string>

    <key>StandardErrorPath</key>
    <string>~/.alfred/logs/everbot.err.log</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>ALFRED_HOME</key>
        <string>~/.alfred</string>
    </dict>
</dict>
</plist>
```

---

## 八、实现路线图

### Phase 1: 基础框架

**目标**：搭建项目骨架，实现核心组件

**任务**：
- [ ] 创建 `src/everbot/` 模块结构
- [ ] 实现 `UserDataManager`
- [ ] 实现 `WorkspaceLoader`
- [ ] 实现 `SessionManager`（内存版）
- [ ] 编写单元测试

**验收**：
```bash
# 能够加载工作区文件
python -c "from src.everbot.workspace import WorkspaceLoader; ..."
```

### Phase 2: 心跳机制

**目标**：实现完整的心跳执行流程

**任务**：
- [ ] 实现 `HeartbeatRunner`
- [ ] 实现 `HistoryManager`
- [ ] 集成 milkie sidecar 执行（MilkieProvider.run_turn）
- [ ] 实现 Session 持久化
- [ ] 添加并发控制

**验收**：
```bash
# 手动触发心跳
alfred everbot heartbeat --agent daily_insight

# 观察日志
tail -f ~/.alfred/logs/heartbeat.log
```

### Phase 3: 后台服务

**目标**：作为 macOS launchd 服务运行

**任务**：
- [ ] 实现 `EverBotDaemon`
- [ ] 创建 LaunchAgent plist
- [ ] 添加 CLI 命令：`alfred everbot start/stop/status/install/uninstall`
- [ ] 实现健康检查端点

**验收**：
```bash
# 安装服务
alfred everbot install

# 检查状态
alfred everbot status

# 查看日志
alfred everbot logs
```

### Phase 4: 高级功能（可选）

- [ ] Cron 表达式支持
- [ ] 多 Agent 并发心跳
- [ ] Web 管理界面
- [ ] Metrics 和告警

---

## 九、技术选型

### 9.1 核心组件

| 组件 | 来源 | 说明 |
|------|------|------|
| MilkieProvider | `provider/milkie/provider.py` | `AgentProvider` 唯一实现，跨进程驱动 milkie serve |
| SidecarPool | `provider/milkie/pool.py` | 惰性 spawn + 常驻复用 per-agent sidecar |
| SidecarLauncher | `provider/milkie/launcher.py` | 装配 milkie serve 启动命令/env |
| milkie serve | milkie（TS/Node） | agent 执行核心子进程，经 HTTP + SSE 通信 |
| discover_skills | `provider/milkie/skills.py` | 扫工作区 SKILL.md，发现技能 |

### 9.2 依赖

**必需**：
- Python 3.10+
- Node.js（运行 milkie serve；node_bin 经 launcher 装配）
- milkie（TS/Node，dist 经 SidecarLauncher 定位）
- PyYAML
- asyncio (标准库)

**可选**：
- uvloop（性能优化）

### 9.3 与现有系统的关系

```
┌─────────────────────────────────────────────────────┐
│                   Alfred 系统                        │
│                                                     │
│  ┌─────────────┐     ┌─────────────────────────┐   │
│  │   Web API   │     │      EverBot Daemon     │   │
│  │  (原有)     │     │        (新增)           │   │
│  └──────┬──────┘     └───────────┬─────────────┘   │
│         │                        │                  │
│         └────────────┬───────────┘                  │
│                      │ MilkieProvider (HTTP + SSE)  │
│              ┌───────▼───────┐                      │
│              │ milkie serve  │                      │
│              │ (per-agent)   │                      │
│              └───────────────┘                      │
└─────────────────────────────────────────────────────┘
```

---

## 附录

### A. HEARTBEAT.md 格式规范

```markdown
# 心跳任务

## 待办
- [ ] 检查今日财经新闻
- [ ] 更新 daily_insight 报告

## 已完成
- [x] 整理本周阅读记录 (2026-02-01)

## 执行记录
<!-- 由 EverBot 自动追加 -->
```

### B. AGENTS.md 格式规范

```markdown
# Agent 行为规范

## 身份
你是 XXX 助理，负责 ...

## 核心职责
1. ...
2. ...

## 沟通风格
- 简洁专业
- 数据驱动

## 限制
- 不要 ...
- 避免 ...
```

### C. CLI 命令参考

```bash
# 启动守护进程（前台）
alfred everbot start

# 启动守护进程（后台，通过 launchd）
alfred everbot install

# 停止服务
alfred everbot stop
alfred everbot uninstall

# 查看状态
alfred everbot status

# 查看日志
alfred everbot logs
alfred everbot logs --agent daily_insight

# 手动触发心跳
alfred everbot heartbeat --agent daily_insight

# 列出 Agent
alfred everbot list

# 初始化新 Agent
alfred everbot init my_agent
```

---

**文档结束**
