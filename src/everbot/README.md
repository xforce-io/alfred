# EverBot - Ever Running Bot

持续运行的 Agent 系统，支持心跳驱动的任务执行。

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
        interval: 30          # 每30分钟执行一次
        active_hours: [8, 22] # 8:00-22:00 活跃
```

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

### 集成真实的 Dolphin Agent

`cli/daemon.py` 中的 `_default_agent_factory` 是一个 Mock 实现，需要替换为真实的 Agent 创建逻辑：

```python
async def create_agent(agent_name: str, workspace_path: Path):
    """创建真实的 Dolphin Agent"""
    from dolphin.sdk import DolphinAgent, AgentRuntime

    # 加载工作区指令
    loader = WorkspaceLoader(workspace_path)
    workspace_instructions = loader.build_system_prompt()

    # 创建 Runtime
    runtime = AgentRuntime(config_path="path/to/dolphin.yaml")

    # 创建 Agent
    agent = DolphinAgent(
        name=agent_name,
        file_path=str(workspace_path / "agent.dph"),
        global_config=runtime.global_config,
        global_skills=runtime.global_skills,
        variables={
            "workspace_instructions": workspace_instructions,
            "model_name": "gpt-4",
            "current_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        },
    )
    await agent.initialize()
    return agent

# 使用
daemon = EverBotDaemon(agent_factory=create_agent)
```

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
