# Alfred EverBot

**Ever Running Bot** — 永远在线的个人 AI Agent 平台。

EverBot 让你的 AI Agent 像一个真正的助手一样持续运行：它能主动执行任务、通过多种渠道与你沟通、并随着交互不断积累记忆。你只需用 Markdown 定义 Agent 的行为和任务，剩下的交给 EverBot。

## 特性

- **永远在线**: 后台守护进程，Agent 7x24 待命
- **心跳驱动**: Cron / Interval 定时自我唤醒，主动推进待办任务，支持时区感知
- **多渠道接入**: Web 界面（FastAPI + WebSocket）+ Telegram Bot，随时随地对话
- **技能系统**: 可扩展的插件化技能（代码审查、浏览器自动化、投资信号、论文检索等）
- **持久记忆**: 对话历史自动持久化，LLM 自动提取关键事实归档到 MEMORY.md
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

## 技能系统

EverBot 通过可插拔的技能模块扩展 Agent 的能力。每个技能是一个独立目录，包含 `SKILL.md`（说明文档）和实现代码。

| 技能 | 用途 | 主要功能 |
|------|------|----------|
| **coding-master** | 代码审查与开发自动化 | 深度审查 SOP、Bugfix 工作流、Feature 开发、工作区锁 |
| **routine-manager** | 任务调度管理 | Cron/Interval 调度、时区感知、执行模式配置 |
| **investment-signal** | 市场分析框架 | 宏观流动性、价值投资评分、中国市场信号 |
| **daily-attractor** | 每日市场监控 | 融资定价偏移、吸引子追踪、Telegram 推送 |
| **paper-discovery** | AI/ML 论文发现 | HuggingFace + arXiv 集成、热度评分、GitHub Star 排名 |
| **dev-browser** | 浏览器自动化 | 持久页面状态、ARIA 快照、截图能力 |
| **skill-installer** | 动态技能管理 | 注册表安装、多源支持 |
| **tushare** | 中国财经数据 | 股票行情、债券、宏观指标、A 股指标 |

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

# 按类型运行
python -m pytest tests/unit/ -v           # 单元测试（隔离，无外部依赖）
python -m pytest tests/integration/ -v    # 集成测试（跨模块，可能需要网络）
python -m pytest tests/web/ -v            # 端到端测试（WebSocket/API）

# 使用统一入口脚本
tests/run_tests.sh unit                   # 运行单元测试
tests/run_tests.sh all --coverage         # 运行全部测试并生成覆盖率

# 运行特定测试
python -m pytest tests/unit/test_agent_factory.py -v
```

测试覆盖（共 ~55 个测试文件）：

- **单元测试** (~38 个): Agent 工厂、Session 管理、内存系统、Channel 路由、心跳约束、Telegram 安全、Web 认证、技能加载、进程管理等
- **集成测试** (~10 个): Daemon 生命周期、Session 恢复与锁、心跳执行流程、深度审查流程、工作区指令恢复等
- **Web 测试** (~5 个): WebSocket 对话（正常/中断/多会话）、Session 重置 API、故障兜底

## 架构

```
EverBot Daemon
    │
    ├── AgentFactory ─────────► DolphinAgent (LLM)
    │                               │
    │                          SkillKit (技能加载)
    │
    ├── HeartbeatRunner (Agent A)
    │   ├── 读取 HEARTBEAT.md
    │   ├── RoutineManager (Cron/Interval 调度)
    │   ├── 注入 Context
    │   ├── 执行 Agent
    │   └── 持久化 Session
    │
    ├── MemorySystem
    │   ├── MemoryExtractor (LLM 提取关键事实)
    │   ├── MemoryMerger (去重与合并)
    │   └── MemoryStore (持久化到 MEMORY.md)
    │
    ├── ChannelService
    │   ├── SessionResolver (渠道→Agent 映射)
    │   ├── TelegramChannel
    │   └── WebChannel (FastAPI + WebSocket)
    │
    ├── SessionManager (JSONL 持久化 + 并发锁)
    │
    └── Web Dashboard (FastAPI)
        ├── Chat API (WebSocket 实时对话)
        ├── Agent/Session 管理 API
        └── API Key 认证
```

核心组件：

- **AgentFactory**: 创建和初始化 Dolphin Agent，支持工作区指令注入
- **UserDataManager**: 统一数据管理（工作区、配置、日志）
- **WorkspaceLoader**: 工作区文件加载（AGENTS.md, HEARTBEAT.md, MEMORY.md, USER.md）
- **SessionManager**: Session 管理（JSONL 持久化、并发锁、Session 恢复）
- **MemoryManager**: 长期记忆管理（事实提取→去重→合并→归档）
- **ChannelService**: 多渠道接入（Telegram Bot、Web UI）
- **RoutineManager**: 任务调度（Cron 表达式、Interval、时区感知）
- **HeartbeatRunner**: 心跳执行器（任务读取、上下文注入、结果持久化）
- **EverBotDaemon**: 守护进程主逻辑（多 Agent 管理、信号处理、状态快照）

## 项目结构

```
alfred/
├── src/everbot/              # EverBot 核心模块
│   ├── cli/                  # CLI 入口
│   ├── web/                  # Web 服务（FastAPI + WebSocket）
│   │   ├── app.py            # FastAPI 应用
│   │   ├── auth.py           # API Key 认证
│   │   └── services/         # Agent/Chat 服务层
│   ├── core/                 # 业务逻辑
│   │   ├── agent/            # Agent 工厂 & Dolphin SDK 集成
│   │   ├── channel/          # 多渠道接入（Telegram, Web）
│   │   ├── memory/           # 记忆系统（提取、合并、存储）
│   │   ├── models/           # 系统事件模型
│   │   ├── runtime/          # 心跳执行、Turn 编排、调度器
│   │   ├── session/          # Session 持久化、压缩、历史管理
│   │   └── tasks/            # 任务调度（RoutineManager）
│   └── infra/                # 基础设施（配置、工作区、进程管理）
│
├── skills/                   # 可扩展技能模块（8 个技能）
│   ├── coding-master/        # 代码审查与开发自动化
│   ├── routine-manager/      # 任务调度管理
│   ├── investment-signal/    # 市场分析框架
│   ├── daily-attractor/      # 每日市场监控
│   ├── paper-discovery/      # AI/ML 论文发现
│   ├── dev-browser/          # 浏览器自动化
│   ├── skill-installer/      # 动态技能安装
│   └── tushare/              # 中国财经数据接口
│
├── tests/                    # 测试（~55 个测试文件）
│   ├── unit/                 # 单元测试（隔离，无外部依赖）
│   ├── integration/          # 集成测试（跨模块，可能需要网络）
│   └── web/                  # 端到端测试（WebSocket/API）
│
├── docs/                     # 设计与技术文档
│   ├── EVERBOT_DESIGN.md     # 架构设计（v1.1）
│   ├── runtime_design.md     # 运行时设计
│   ├── memory_system_design.md # 记忆系统设计
│   ├── channel_design.md     # 多渠道设计
│   ├── SKILLS_GUIDE.md       # 技能开发指南
│   └── skills/               # 技能级设计文档
│
├── examples/                 # 使用示例
│   ├── everbot_demo.py       # 基础功能演示
│   └── real_agent_demo.py    # 真实 Agent 对话示例
│
├── config/                   # 配置模板
│   ├── everbot.example.yaml  # EverBot 配置示例
│   └── dolphin.yaml          # Dolphin SDK 配置
│
├── bin/                      # 可执行脚本
│   ├── everbot               # CLI 入口
│   └── setup                 # 安装脚本
│
└── requirements.txt          # Python 依赖
```

## 常见问题

### Q: 如何修改心跳间隔？

A: 编辑 `~/.alfred/config.yaml`，修改 `everbot.agents.<agent_name>.heartbeat.interval` 值（单位：分钟）。

### Q: 心跳任务会污染用户对话历史吗？

A: 不会。默认使用 `isolated` 模式，心跳使用独立的 Session（`heartbeat_<agent_name>`）。

### Q: History 会无限增长吗？

A: 不会。`HistoryManager` 会自动裁剪过长的历史，保留最近 10 轮对话，其余归档到 `MEMORY.md`。MemorySystem 会自动提取关键事实并去重。

### Q: 如何查看心跳日志？

A: 查看 `~/.alfred/logs/heartbeat.log`。

### Q: 如何自定义 Agent 的 .dph 文件？

A: 编辑 `~/.alfred/agents/<agent_name>/agent.dph`，使用 Dolphin 语法定义 Agent 行为。

### Q: 如何开发自定义技能？

A: 参考 [docs/SKILLS_GUIDE.md](docs/SKILLS_GUIDE.md)，在 `skills/` 下创建新目录，包含 `SKILL.md` 和实现代码。

## Roadmap

- [ ] macOS launchd 深度集成
- [ ] Metrics 和监控告警
- [ ] 多用户权限管理
- [ ] 技能市场（远程注册表）
- [ ] 高级记忆功能（RAG、向量检索）

## 许可证

（待定）

## 参考

- 架构设计: [docs/EVERBOT_DESIGN.md](docs/EVERBOT_DESIGN.md)
- 运行时设计: [docs/runtime_design.md](docs/runtime_design.md)
- 记忆系统: [docs/memory_system_design.md](docs/memory_system_design.md)
- 多渠道设计: [docs/channel_design.md](docs/channel_design.md)
- 技能开发: [docs/SKILLS_GUIDE.md](docs/SKILLS_GUIDE.md)
