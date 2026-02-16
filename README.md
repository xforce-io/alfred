# Alfred EverBot

Ever Running Bot - 持续运行的 Agent 系统，支持心跳驱动的任务执行。

## 特性

- **持续运行**: 通过 macOS `launchd` 作为后台服务运行
- **心跳机制**: 定期自我唤醒，执行任务推进
- **工作区管理**: 基于 Markdown 的配置文件（AGENTS.md, HEARTBEAT.md 等）
- **Session 管理**: 自动持久化和恢复对话历史
- **并发控制**: Session 级别的锁机制，避免竞态条件
- **真实 Dolphin Agent**: 集成 Dolphin SDK，使用真实的 LLM Agent

## 快速开始

### 1. 安装依赖

```bash
pip install dolphin-sdk pyyaml
```

可选（推荐）：确保 Dolphin 的 `system_skillkit` 已启用（提供 `_read_file/_read_folder` 等工具能力）。本仓库默认配置已包含该项：`config/dolphin.yaml`。

### 2. 初始化 Agent

```bash
# 初始化 Agent 工作区
./bin/everbot init my_agent
```

这将创建：

```
~/.alfred/agents/my_agent/
├── agent.dph        # Agent 定义（Dolphin 格式）
├── AGENTS.md        # 行为规范
├── HEARTBEAT.md     # 心跳任务清单
├── MEMORY.md        # 长期记忆
└── USER.md          # 用户画像
```

### 3. 配置

```bash
# 复制配置示例
cp config/everbot.example.yaml ~/.alfred/config.yaml

# 编辑配置
vim ~/.alfred/config.yaml
```

配置示例：

```yaml
everbot:
  enabled: true
  default_model: gpt-4  # 默认模型

  agents:
    my_agent:
      workspace: ~/.alfred/agents/my_agent
      heartbeat:
        enabled: true
        interval: 30          # 每30分钟执行一次
        active_hours: [8, 22] # 8:00-22:00 活跃
```

### 4. 编写任务清单

编辑 `~/.alfred/agents/my_agent/HEARTBEAT.md`：

```markdown
# 心跳任务

## 待办
- [ ] 检查今日新闻
- [ ] 更新日报

## 已完成
- [x] 初始化工作区 (2026-02-01)
```

### 5. 启动

```bash
# 一键启动（后台启动 daemon + web）
./bin/everbot start

# Web 界面地址
# http://127.0.0.1:8765

# 前台启动（用于调试）
./bin/everbot start --foreground

# 仅启动 daemon（不启动 web）
./bin/everbot start --no-web

# 自定义端口
./bin/everbot start --web-port 9000
```

## CLI 命令

```bash
# 初始化
./bin/everbot init [agent_name]           # 创建 Agent 工作区

# 启动和管理
./bin/everbot start                       # 启动 daemon + web（后台）
./bin/everbot start --foreground          # 前台启动（调试用）
./bin/everbot start --no-web              # 仅启动 daemon
./bin/everbot stop                        # 停止所有服务
./bin/everbot restart                     # 重启所有服务
./bin/everbot status                      # 查看状态

# 其他
./bin/everbot list                        # 列出所有 Agent
./bin/everbot doctor                      # 环境自检（配置/技能/依赖/工作区）
./bin/everbot heartbeat --agent my_agent  # 手动触发心跳
./bin/everbot config --show               # 显示当前配置
./bin/everbot config --init               # 初始化默认配置
```

运行时文件：
- `~/.alfred/everbot.pid`: 守护进程 PID 文件
- `~/.alfred/everbot-web.pid`: Web 进程 PID 文件
- `~/.alfred/everbot.status.json`: 守护进程状态快照（供 `status`/Web 读取）
- `~/.alfred/logs/everbot.out`: 守护进程日志
- `~/.alfred/logs/everbot-web.out`: Web 服务器日志
- `~/.alfred/logs/heartbeat.log`: 心跳日志

## 使用示例

### 示例 1: 基础示例

```bash
# 运行基础示例（展示所有功能）
PYTHONPATH=. python examples/everbot_demo.py
```

### 示例 2: 真实 Agent 对话

```bash
# 创建并与真实 Agent 对话
PYTHONPATH=. python examples/real_agent_demo.py

# 查看 Agent 信息
PYTHONPATH=. python examples/real_agent_demo.py info
```

### 示例 3: 在代码中使用

```python
from src.everbot import create_agent, UserDataManager
from pathlib import Path

# 初始化
user_data = UserDataManager()
user_data.init_agent_workspace("my_agent")

# 创建 Agent
agent_dir = user_data.get_agent_dir("my_agent")
agent = await create_agent("my_agent", agent_dir)

# 对话
async for event in agent.continue_chat(message="Hello!", stream_mode="delta"):
    # 处理响应...
    pass
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

### agent.dph - Agent 定义

Dolphin 格式的 Agent 定义文件，支持变量注入：

```
'''
Agent 名称

$workspace_instructions
''' -> system

/explore/(model="$model_name", tools=[_bash, _python])
...
-> answer
```

## 测试

```bash
# 运行所有测试
python -m pytest tests/ -v

# 运行特定测试
python -m pytest tests/test_agent_factory.py -v
```

测试覆盖：
- ✓ 用户数据管理（UserDataManager）
- ✓ 工作区加载（WorkspaceLoader）
- ✓ Session 管理（SessionManager）
- ✓ 配置管理（Config）
- ✓ Agent 工厂（AgentFactory）
- ✓ 真实 Dolphin Agent 创建

## 架构

```
EverBot Daemon
    │
    ├── AgentFactory ─────► DolphinAgent (真实 LLM)
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

- **AgentFactory**: 创建和初始化 Dolphin Agent
- **UserDataManager**: 统一数据管理
- **WorkspaceLoader**: 工作区文件加载
- **SessionManager**: Session 管理（带并发控制）
- **HistoryManager**: History 裁剪与归档
- **HeartbeatRunner**: 心跳执行器
- **EverBotDaemon**: 守护进程主逻辑

## 项目结构

```
alfred/
├── src/everbot/              # EverBot 核心模块
│   ├── cli/                  # CLI 入口
│   ├── web/                  # Web 入口
│   ├── core/                 # 业务逻辑
│   │   ├── runtime/
│   │   ├── models/
│   │   ├── agent/
│   │   ├── tasks/
│   │   └── session/
│   ├── infra/                # 基础设施
│   └── __init__.py
│
├── tests/                    # 测试
│   ├── test_everbot_basic.py
│   └── test_agent_factory.py
│
├── examples/                 # 示例
│   ├── everbot_demo.py       # 基础示例
│   └── real_agent_demo.py    # 真实 Agent 示例
│
├── config/
│   └── everbot.example.yaml  # 配置示例
│
├── docs/
│   └── EVERBOT_DESIGN.md     # 设计文档
│
└── bin/everbot               # 启动脚本
```

## 常见问题

### Q: 如何修改心跳间隔？

A: 编辑 `~/.alfred/config.yaml`，修改 `everbot.agents.<agent_name>.heartbeat.interval` 值（单位：分钟）。

### Q: 心跳任务会污染用户对话历史吗？

A: 不会。默认使用 `isolated` 模式，心跳使用独立的 Session（`heartbeat_<agent_name>`）。

### Q: History 会无限增长吗？

A: 不会。`HistoryManager` 会自动裁剪过长的历史，保留最近 10 轮对话，其余归档到 `MEMORY.md`。

### Q: 如何查看心跳日志？

A: 查看 `~/.alfred/logs/heartbeat.log`。

### Q: 如何自定义 Agent 的 .dph 文件？

A: 编辑 `~/.alfred/agents/<agent_name>/agent.dph`，使用 Dolphin 语法定义 Agent 行为。

## 待实现功能

- [ ] launchd 集成（macOS 后台服务）
- [ ] PID 文件管理
- [ ] 健康检查端点
- [ ] 手动心跳触发
- [ ] Web 管理界面
- [ ] Metrics 和告警

## 许可证

（待定）

## 参考

- 设计文档: [docs/EVERBOT_DESIGN.md](docs/EVERBOT_DESIGN.md)
- Dolphin SDK: https://github.com/your-org/dolphin
