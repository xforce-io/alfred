# EverBot 更新日志

## v0.1.0 (2026-02-01)

### 新功能

#### 核心模块
- ✅ **AgentFactory**: 真实的 Dolphin Agent 创建工厂
  - 支持 Dolphin SDK 集成
  - 自动加载工作区指令
  - 支持自定义模型和配置
  
- ✅ **UserDataManager**: 统一数据管理
  - Agent 工作区管理
  - 目录结构初始化
  - 文件模板生成

- ✅ **WorkspaceLoader**: 工作区文件加载
  - 加载 AGENTS.md, HEARTBEAT.md, MEMORY.md, USER.md
  - 构建系统提示
  
- ✅ **SessionManager**: Session 管理
  - Session 持久化（JSON 格式）
  - 并发锁机制（asyncio.Lock）
  - Session 恢复

- ✅ **HistoryManager**: History 管理
  - History 裁剪（保留最近 10 轮）
  - 归档到 MEMORY.md

- ✅ **HeartbeatRunner**: 心跳运行器
  - 定时触发机制
  - 活跃时段控制
  - 重试机制
  - 静默处理（HEARTBEAT_OK）

- ✅ **EverBotDaemon**: 守护进程
  - 多 Agent 管理
  - 心跳调度
  - 信号处理

- ✅ **CLI**: 命令行接口
  - init: 初始化 Agent 工作区
  - list: 列出 Agent
  - start: 启动守护进程
  - config: 配置管理

#### 测试
- ✅ 12 个单元测试全部通过
  - test_everbot_basic.py: 10 个测试
  - test_agent_factory.py: 2 个测试

#### 示例
- ✅ everbot_demo.py: 基础功能演示
- ✅ real_agent_demo.py: 真实 Agent 对话示例

#### 文档
- ✅ README.md: 使用文档
- ✅ src/everbot/README.md: 模块文档
- ✅ docs/EVERBOT_DESIGN.md: 设计文档（v1.1，已优化）

### 重要改进

#### 从 Mock 迁移到真实 Agent
- **之前**: 使用 Mock Agent（仅用于测试）
- **现在**: 使用真实的 Dolphin SDK Agent
- **影响**: 可以真正执行 LLM 对话和工具调用

#### Dolphin SDK 集成
- 正确的 .dph 文件格式
- Context API 适配（get_var_value, set_variable）
- GlobalConfig 和 GlobalSkills 管理

#### 并发控制
- Session 级别的锁
- 心跳与用户对话隔离
- 超时机制

### 技术栈

- Python 3.10+
- Dolphin SDK 0.1.0
- PyYAML
- asyncio
- pytest

### 目录结构

```
alfred/
├── src/everbot/         # 核心模块（8 个文件）
├── tests/               # 测试（12 个测试）
├── examples/            # 示例（2 个）
├── config/              # 配置示例
├── docs/                # 设计文档
├── bin/everbot          # 启动脚本
└── bin/setup            # 安装脚本
```

### 下一步

- [ ] macOS launchd 集成
- [ ] 健康检查端点
- [ ] Web 管理界面
- [ ] Metrics 和监控
