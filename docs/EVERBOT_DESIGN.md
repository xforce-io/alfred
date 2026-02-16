# Alfred EverBot 设计方案

> **版本**: v1.1
> **创建时间**: 2026-02-01
> **更新时间**: 2026-02-01

---

## 目录

1. [概述与目标](#一概述与目标)
2. [核心概念](#二核心概念)
3. [Dolphin Context 管理](#三dolphin-context-管理)
4. [Skills 管理（ResourceSkillkit）](#四skills-管理resourceskillkit)
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

1. **最大化复用 Dolphin SDK**
   - 继续使用 `AgentRuntime`、`DolphinAgent`、`Env` 等核心组件
   - Skillkit 机制保持不变
   - 配置系统继续使用 dolphin.yaml

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
| DolphinAgent | Dolphin SDK 提供的 Agent 执行核心 |
| Session | 会话，包含 Agent 实例和对话历史 |
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
│  │            Agent Runtime (Dolphin)           │          │
│  │  - Env / GlobalConfig                        │          │
│  │  - GlobalSkills + ResourceSkillkits          │          │
│  │  - DolphinAgent 实例池                       │          │
│  └──────────────┬───────────────────────────────┘          │
│                 │                                           │
│  ┌──────────────▼───────────────────────────────┐          │
│  │            Session Manager                    │          │
│  │  - 内存缓存 + JSONL 持久化                    │          │
│  │  - 并发锁管理                                 │          │
│  └──────────────────────────────────────────────┘          │
└─────────────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                       文件系统                               │
│  ~/.alfred/                                                 │
│  ├── config.yaml              # 主配置                       │
│  ├── agents/<name>/           # Agent 工作区                 │
│  │   ├── agent.dph            # Agent 模板定义               │
│  │   ├── AGENTS.md            # 行为规范                     │
│  │   ├── HEARTBEAT.md         # 心跳任务清单                 │
│  │   ├── MEMORY.md            # 长期记忆                     │
│  │   └── USER.md              # 用户画像                     │
│  ├── sessions/                # 会话历史 (JSONL)             │
│  └── logs/                    # 服务日志                     │
└─────────────────────────────────────────────────────────────┘
```

---

## 三、Dolphin Context 管理

### 3.1 Context API 参考

Dolphin SDK 的 `Context` 对象提供以下核心 API：

| 方法 | 参数 | 返回值 | 说明 |
|------|------|--------|------|
| `set_variable(name, value)` | `str`, `Any` | `None` | 设置变量，可在 .dph 模板中用 `$name` 引用 |
| `get_variable(name)` | `str` | `Any` | 获取变量值 |
| `get_messages()` | - | `Messages` | 获取消息集合对象 |
| `get_history_messages(normalize)` | `bool` | `List[Dict]` | 获取历史消息列表 |
| `set_session_id(session_id)` | `str` | `None` | 设置会话 ID |
| `set_skills(skills)` | `List[Skill]` | `None` | 注册可用工具 |
| `init_trajectory(path, overwrite)` | `str`, `bool` | `None` | 初始化执行轨迹记录 |
| `clear_history()` | - | `None` | 清空历史消息 |
| `set_history_messages(messages)` | `List[Dict]` | `None` | 设置历史消息（用于恢复） |

### 3.2 Context 注入区域

```
┌─────────────────────────────────────────────────────────────────┐
│                     Dolphin Context                              │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ 1. Variables (变量)                                       │   │
│  │    - context.set_variable("name", value)                 │   │
│  │    - 在 .dph 模板中用 $name 引用                          │   │
│  │    - 适合：静态配置、模板参数、工作区指令                  │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ 2. Messages (消息历史)                                    │   │
│  │    - 通过 arun/continue_chat 自动管理                    │   │
│  │    - 使用 Dolphin API 操作，不直接修改内部列表           │   │
│  │    - 适合：对话轮次、心跳交互                              │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ 3. Buckets (桶 - 可选)                                    │   │
│  │    - context_manager.add_bucket(name, content)           │   │
│  │    - 内置桶：HISTORY, SYSTEM, RETRIEVAL                  │   │
│  │    - 适合：检索结果、RAG 数据注入                         │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### 3.3 .dph 模板示例

EverBot 使用的 Agent 模板需要支持动态注入工作区指令：

```yaml
# ~/.alfred/agents/daily_insight/agent.dph

name: daily_insight
description: 每日市场洞察助理

# 系统提示（支持变量引用）
system_prompt: |
  $workspace_instructions

  # 当前时间
  当前时间：$current_time

# 模型配置
model:
  name: $model_name
  temperature: 0.7

# 工具配置
tools:
  - type: bash
    enabled: true
  - type: python
    enabled: true
  - type: retrieval
    enabled: true
```

**变量注入示例**：

```python
# 注入工作区指令
workspace_instructions = workspace_loader.build_system_prompt()
context.set_variable("workspace_instructions", workspace_instructions)

# 注入时间
context.set_variable("current_time", datetime.now().strftime("%Y-%m-%d %H:%M"))

# 注入模型名
context.set_variable("model_name", "gpt-4")
```

### 3.4 Heartbeat 注入机制

#### 3.4.1 注入策略

心跳触发时，通过 **Variables 注入** + **User Message 触发** 的组合方式：

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

    async def inject_heartbeat_context(
        self,
        agent: DolphinAgent,
        heartbeat_content: str,
    ) -> str:
        """
        注入心跳信息到 Context

        Returns:
            构造好的 user message，用于触发 Agent 执行
        """
        context = agent.executor.context

        # 1. 注入心跳系统指令（追加到 workspace_instructions）
        current_instructions = context.get_variable("workspace_instructions") or ""
        context.set_variable(
            "workspace_instructions",
            current_instructions + "\n\n" + self.HEARTBEAT_SYSTEM_INSTRUCTION
        )

        # 2. 注入心跳元数据
        context.set_variable("heartbeat_mode", True)
        context.set_variable("heartbeat_time", datetime.now().isoformat())

        # 3. 构造触发消息
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

### 3.5 History 管理（使用 Dolphin API）

#### 3.5.1 核心原则

**不直接操作 `msgs.messages` 列表**，而是使用 Dolphin 提供的 API：

```python
# ❌ 错误：直接修改内部列表
msgs.messages = msgs.messages[-20:]

# ✅ 正确：使用 Dolphin API
context.clear_history()
context.set_history_messages(trimmed_messages)
```

#### 3.5.2 History 裁剪实现

```python
class HistoryManager:
    """History 管理器 - 使用 Dolphin API"""

    MAX_HISTORY_ROUNDS = 10  # 最多保留 10 轮对话

    def __init__(self, memory_path: Path):
        self.memory_path = memory_path

    def trim_if_needed(self, agent: DolphinAgent) -> bool:
        """
        裁剪过长的 History

        使用 Dolphin API 而非直接操作内部列表

        Returns:
            是否执行了裁剪
        """
        context = agent.executor.context
        history = context.get_history_messages(normalize=True)

        max_messages = self.MAX_HISTORY_ROUNDS * 2  # user + assistant

        if len(history) <= max_messages:
            return False

        # 1. 提取要归档的消息
        archived_messages = history[:-max_messages]

        # 2. 归档到 MEMORY.md
        self._archive_to_memory(archived_messages)

        # 3. 使用 Dolphin API 重置 History
        trimmed_messages = history[-max_messages:]
        context.clear_history()
        context.set_history_messages(trimmed_messages)

        logger.info(f"裁剪 History: 归档 {len(archived_messages)} 条，保留 {len(trimmed_messages)} 条")
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

#### 3.6.1 Session 数据结构

```python
@dataclass
class SessionData:
    """Session 持久化数据"""
    session_id: str
    agent_name: str
    model_name: str
    history_messages: List[Dict]  # 对话历史
    variables: Dict[str, Any]     # Context 变量
    created_at: str
    updated_at: str

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> "SessionData":
        return cls(**data)
```

#### 3.6.2 持久化实现

```python
class SessionPersistence:
    """Session 持久化管理器"""

    def __init__(self, sessions_dir: Path):
        self.sessions_dir = sessions_dir
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def _get_session_path(self, session_id: str) -> Path:
        return self.sessions_dir / f"{session_id}.json"

    async def save(self, session_id: str, agent: DolphinAgent, model_name: str):
        """保存 Session 到文件"""
        context = agent.executor.context

        # 提取需要持久化的数据
        data = SessionData(
            session_id=session_id,
            agent_name=agent.name,
            model_name=model_name,
            history_messages=context.get_history_messages(normalize=True),
            variables={
                "workspace_instructions": context.get_variable("workspace_instructions"),
                "model_name": context.get_variable("model_name"),
                # 其他需要持久化的变量...
            },
            created_at=context.get_variable("session_created_at") or datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
        )

        session_path = self._get_session_path(session_id)
        with open(session_path, "w", encoding="utf-8") as f:
            json.dump(data.to_dict(), f, ensure_ascii=False, indent=2)

        logger.debug(f"Session 已保存: {session_id}")

    async def load(self, session_id: str) -> Optional[SessionData]:
        """从文件加载 Session"""
        session_path = self._get_session_path(session_id)
        if not session_path.exists():
            return None

        with open(session_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        return SessionData.from_dict(data)

    async def restore_to_agent(self, agent: DolphinAgent, session_data: SessionData):
        """恢复 Session 数据到 Agent"""
        context = agent.executor.context

        # 1. 恢复变量
        for name, value in session_data.variables.items():
            if value is not None:
                context.set_variable(name, value)

        # 2. 恢复历史消息（使用 Dolphin API）
        if session_data.history_messages:
            context.set_history_messages(session_data.history_messages)

        # 3. 设置 session ID
        context.set_session_id(session_data.session_id)

        logger.info(f"Session 已恢复: {session_data.session_id}, 历史消息: {len(session_data.history_messages)} 条")
```

### 3.7 并发控制

为避免心跳与用户对话的竞态条件，使用 Session 级别的锁：

```python
class SessionManager:
    """Session 管理器 - 带并发控制"""

    def __init__(self, persistence: SessionPersistence):
        self.persistence = persistence
        self._agents: Dict[str, DolphinAgent] = {}  # 内存缓存
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

        # 执行心跳逻辑
        agent = await self.session_manager.get_or_create_agent(session_id)
        result = await self._execute_heartbeat(agent)
        return result
```

---

## 四、Skills 管理（ResourceSkillkit）

### 4.1 Dolphin Skillkit 体系

| 层级 | 类型 | 说明 | 加载方式 |
|------|------|------|---------|
| **内置** | System Functions | `_bash`, `_python`, `_date` | `dolphin.yaml` 启用 |
| **扩展** | ResourceSkillkit | 目录扫描加载 | 配置 `resource_skills.directories` |
| **自定义** | Custom Skillkit | 代码定义 | `runtime.register_skillkit()` |

### 4.2 配置示例

```yaml
# config/dolphin.yaml

resource_skills:
  enabled: true
  directories:
    # 全局技能目录
    - "~/.alfred/skills"
    # Agent 专属技能（运行时动态添加）
```

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
├── dolphin.yaml                     # Dolphin 配置
│
├── skills/                          # 全局共享技能
│
├── agents/                          # Agent 工作区
│   └── <agent_name>/
│       ├── agent.dph                # Agent 模板
│       ├── config.yaml              # Agent 配置
│       ├── AGENTS.md                # 行为规范
│       ├── HEARTBEAT.md             # 心跳任务
│       ├── MEMORY.md                # 长期记忆
│       ├── USER.md                  # 用户画像
│       └── skills/                  # Agent 专属技能
│
├── sessions/                        # 会话存储
│   ├── <session_id>.json            # Session 数据
│   └── metadata.json                # 索引
│
├── trajectories/                    # 执行轨迹（调试用）
│
└── logs/                            # 服务日志
    ├── everbot.log
    └── heartbeat.log
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
│  4. 获取/创建 Agent                               │
│     - 尝试从缓存获取                              │
│     - 缓存未命中则创建并恢复 Session              │
└──────────────────────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────────────────────┐
│  5. 注入 Heartbeat Context                        │
│     - workspace_instructions += heartbeat_prompt │
│     - set heartbeat_mode = True                  │
└──────────────────────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────────────────────┐
│  6. 执行 Agent                                    │
│     - agent.arun() 或 agent.continue_chat()      │
│     - 收集响应                                    │
└──────────────────────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────────────────────┐
│  7. 后处理                                        │
│     - 裁剪 History（如需要）                      │
│     - 持久化 Session                              │
│     - 释放锁                                      │
│     - 记录日志                                    │
└──────────────────────────────────────────────────┘
    │
    ▼
返回结果
```

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

            # 2. 获取或创建 Agent
            agent = await self.session_manager.get_or_create_agent(
                self.session_id,
                self.agent_name
            )

            # 3. 读取任务清单
            heartbeat_content = self._read_heartbeat_md()
            if not heartbeat_content:
                return "HEARTBEAT_OK"

            # 4. 注入 Context
            user_message = await self._inject_heartbeat_context(agent, heartbeat_content)

            # 5. 执行
            result = await self._run_agent(agent, user_message)

            # 6. 持久化
            await self.session_manager.save_session(self.session_id, agent)

            return result

    async def _inject_heartbeat_context(self, agent, heartbeat_content: str) -> str:
        """注入心跳上下文"""
        context = agent.executor.context

        # 追加心跳指令
        current = context.get_variable("workspace_instructions") or ""
        context.set_variable(
            "workspace_instructions",
            current + "\n\n" + self.HEARTBEAT_SYSTEM_INSTRUCTION
        )

        # 设置心跳标记
        context.set_variable("heartbeat_mode", True)
        context.set_variable("heartbeat_time", datetime.now().isoformat())

        # 构造触发消息
        return f"""
[系统心跳 - {datetime.now().strftime('%Y-%m-%d %H:%M')}]

## 任务清单

{heartbeat_content}

---

请检查任务清单并执行。如无待办，回复"HEARTBEAT_OK"。
"""

    async def _run_agent(self, agent, message: str) -> str:
        """执行 Agent"""
        result = ""
        async for event in agent.continue_chat(message=message, stream_mode="delta"):
            if "_progress" in event:
                for progress in event["_progress"]:
                    if progress.get("stage") == "llm":
                        answer = progress.get("answer", "")
                        if answer:
                            result = answer
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
- [ ] 集成 Dolphin Agent 执行
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

### 9.1 复用组件

| 组件 | 来源 | 说明 |
|------|------|------|
| DolphinAgent | dolphin.sdk | Agent 执行核心 |
| AgentRuntime | dolphin.sdk | 运行时管理 |
| ResourceSkillkit | dolphin.sdk | 技能加载 |

### 9.2 依赖

**必需**：
- Python 3.10+
- dolphin-sdk
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
│                      │                              │
│              ┌───────▼───────┐                      │
│              │ Dolphin SDK   │                      │
│              │ (共享)        │                      │
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
