# Alfred EverBot

**Ever Running Bot** — 永远在线的个人 AI Agent 平台。

EverBot 让你的 AI Agent 像一个真正的助手一样持续运行：它能主动执行任务、通过多种渠道与你沟通、并随着交互不断积累记忆。你只需用 Markdown 定义 Agent 的行为和任务，剩下的交给 EverBot。

## 特性

- **永远在线**: 后台守护进程，Agent 7x24 待命
- **心跳驱动**: 定时自我唤醒，主动推进待办任务
- **多渠道接入**: Web 界面 + Telegram Bot，随时随地对话
- **技能系统**: 可扩展的插件化技能（浏览器自动化、数据查询、论文检索等）
- **持久记忆**: 对话历史自动持久化，长期记忆归档到 MEMORY.md
- **Markdown 驱动**: 用 AGENTS.md 定义人设，HEARTBEAT.md 定义任务，所见即所得

## 快速开始

```bash
git clone <repo-url> alfred && cd alfred
bin/setup                        # 安装环境
source .venv/bin/activate        # 激活虚拟环境
./bin/everbot init my_agent      # 创建 Agent（自动注册到配置）
./bin/everbot start              # 启动（daemon + Web）
```

> 详见 [QUICKSTART.md](QUICKSTART.md)，包含 Telegram 配置等完整步骤。

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

## Roadmap

- [ ] Metrics 和监控告警
- [ ] 多用户权限管理
- [ ] 技能市场（远程注册表）

## 许可证

（待定）

## 参考

- 设计文档: [docs/EVERBOT_DESIGN.md](docs/EVERBOT_DESIGN.md)
- Dolphin SDK: https://github.com/your-org/dolphin
