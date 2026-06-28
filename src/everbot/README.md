# EverBot - Ever Running Bot

永远在线的个人 AI Agent 平台。

## 快速开始

### 1. 初始化

```bash
# 初始化 EverBot 目录结构
python -m src.everbot.cli init

# 初始化特定 Agent 工作区
python -m src.everbot.cli init my_agent
```

这将创建以下目录结构：

```
~/.alfred/
├── config.yaml              # 主配置
├── agents/
│   └── my_agent/
│       ├── agent.dph        # Agent 定义
│       ├── AGENTS.md        # 行为规范
│       ├── HEARTBEAT.md     # 心跳任务清单
│       ├── MEMORY.md        # 长期记忆
│       └── USER.md          # 用户画像
├── sessions/                # 会话存储
└── logs/                    # 日志
```

### 2. 配置

复制示例配置并修改：

```bash
cp config/everbot.example.yaml ~/.alfred/config.yaml
```

编辑 `~/.alfred/config.yaml`：

```yaml
everbot:
  agents:
    my_agent:
      workspace: ~/.alfred/agents/my_agent
      heartbeat:
        enabled: true
        interval: 30           # 活跃时段每 30 分钟执行一次
        active_hours: [8, 22]  # 活跃时段，按【部署机本地时区】判断，半开区间 [8, 22)（22:00 不含）
        night_interval: 0      # 非活跃时段间隔（分钟）：0 或省略 = 夜间静默，正整数 = 夜间降频
```

> ⚠️ `active_hours` 按**部署机本地时区**判断（不是 UTC）。修改 `config.yaml` 后需执行
> `./bin/everbot restart` 才会生效——daemon 不会热重载已加载的代码与配置。

### 3. 编写任务清单

编辑 `~/.alfred/agents/my_agent/HEARTBEAT.md`：

```markdown
# 心跳任务

## 待办
- [ ] 检查今日新闻
- [ ] 更新日报

## 已完成
- [x] 初始化工作区 (2026-02-01)
```

### 4. 启动

```bash
# 一键启动（推荐）
./bin/everbot start

# Web 界面: http://127.0.0.1:8765
```

## CLI 命令

**推荐使用 `./bin/everbot`**，它提供完整的进程管理功能。

```bash
# 启动和管理
./bin/everbot start                       # 启动 daemon + web
./bin/everbot start --foreground          # 前台启动
./bin/everbot start --no-web              # 仅启动 daemon
./bin/everbot stop                        # 停止所有服务
./bin/everbot restart                     # 重启
./bin/everbot status                      # 查看状态

# 其他
./bin/everbot list                        # 列出 Agent
./bin/everbot init [agent_name]           # 创建 Agent
./bin/everbot doctor                      # 环境自检
./bin/everbot heartbeat --agent my_agent  # 手动触发心跳
./bin/everbot config --show               # 查看配置
```

## 工作区文件说明

### AGENTS.md - 行为规范

定义 Agent 的身份、职责和沟通风格：

```markdown
# Agent 行为规范

## 身份
你是 XXX 助理，负责...

## 核心职责
1. ...
2. ...

## 沟通风格
- 简洁专业
- 数据驱动
```

### HEARTBEAT.md - 心跳任务清单

定义定期执行的任务：

```markdown
# 心跳任务

## 待办
- [ ] 任务1
- [ ] 任务2

## 已完成
- [x] 任务0 (2026-02-01)
```

### MEMORY.md - 长期记忆

Agent 的长期记忆存储，会被自动注入到系统提示中：

```markdown
# 长期记忆

## 重要事件
- 2026-02-01: ...

## 历史对话归档
（由系统自动追加）
```

### USER.md - 用户画像

用户的个人信息和偏好：

```markdown
# 用户画像

## 基本信息
- 姓名: ...
- 关注领域: ...

## 偏好
- 沟通风格: ...
```

## 架构说明

```
EverBot Daemon
    │
    ├── HeartbeatRunner (Agent A)
    │   ├── 读取 HEARTBEAT.md
    │   ├── 注入 Context
    │   ├── 执行 Agent
    │   └── 持久化 Session
    │
    ├── HeartbeatRunner (Agent B)
    └── ...
```

核心组件：

- **UserDataManager**: 统一数据管理
- **WorkspaceLoader**: 工作区文件加载
- **SessionManager**: Session 管理（带并发控制）
- **HistoryManager**: History 裁剪与归档
- **HeartbeatRunner**: 心跳执行器
- **EverBotDaemon**: 守护进程主逻辑

## 开发指南

### 集成 Agent runtime（milkie provider）

Agent runtime 由 provider 中立的 `AgentProvider`（`core/agent/provider/base.py`）抽象，当前唯一实现是 `MilkieProvider`（跨进程驱动 `milkie serve` sidecar）。daemon 经 `get_provider()` 拿到 provider，再用 `create_agent` 惰性 spawn 单个 agent 的 sidecar，用 `run_turn` 驱动一轮对话：

```python
from src.everbot.core.agent.provider import get_provider

async def create_agent(agent_name: str, workspace_path: Path):
    """经 provider 中立接口创建 agent（惰性 spawn milkie sidecar）。

    system prompt 由工作区 Markdown（SOUL/AGENTS/SKILLS/USER/MEMORY.md）合成，
    spawn 时传给 milkie serve；技能由 discover_skills 扫描工作区 SKILL.md 注入。
    """
    provider = get_provider()
    agent = await provider.create_agent(
        agent_name=agent_name,
        workspace_path=workspace_path,
        # model_name 不传则按 config/models.yaml + everbot.agents.<name>.model 路由
    )
    return agent

# 使用：daemon 拿到 agent 后用 provider.run_turn 驱动一轮对话
daemon = EverBotDaemon(agent_factory=create_agent)
```

> 注：milkie 是 TS/Node 库，Python 无法直接 `import`，因此形态固定为跨进程 sidecar —— 每个 agent 跑一个 `milkie serve` 子进程，alfred 经 HTTP + SSE 与之通信；会话身份为 `contextId`，`milkie serve` 自持久化历史（重启从 checkpoint 恢复）。

### 测试

```bash
# 运行单元测试（待实现）
pytest tests/

# 手动测试
python -m src.everbot.cli init test_agent
python -m src.everbot.cli start --log-level DEBUG
```

## 常见问题

### Q: 如何修改心跳间隔？

A: 编辑 `~/.alfred/config.yaml`，修改 `everbot.agents.<agent_name>.heartbeat.interval` 值（单位：分钟）。

### Q: 心跳任务会污染用户对话历史吗？

A: 当前版本采用“单 Agent 长驻会话”模式。心跳与聊天共享同一 Session（`web_session_<agent_name>`），以保证长期连续记忆。

### Q: History 会无限增长吗？

A: 不会。`HistoryManager` 会自动裁剪过长的历史，保留最近 10 轮对话，其余归档到 `MEMORY.md`。

### Q: 如何查看心跳日志？

A: 查看 `~/.alfred/logs/heartbeat.log`。

## 待实现功能

- [ ] launchd 集成（macOS 后台服务）
- [ ] PID 文件管理
- [ ] 健康检查端点
- [ ] 手动心跳触发
- [ ] Web 管理界面
- [ ] Metrics 和告警

## 许可证

（待定）
