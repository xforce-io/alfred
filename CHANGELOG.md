# EverBot 更新日志

## 最近更新 (2026-02)

### 安全与稳定性
- fix(security): 修复 3 个安全漏洞（Telegram 下载安全、Session 注入等）
- fix(workspace,telegram): 重写 local origin 到 upstream URL，压缩 tool call 显示
- fix(session): 防止 `import_portable_session` 返回 None、Session 恢复后重新加载工作区指令
- fix(daemon): 非交互 shell 下加载 `~/.env.secrets`，启动时快速失败检查环境变量
- fix(telegram): 启动时清除 pending updates 防止 stale replay

### Coding Master 技能
- feat(coding-master): 完整的代码审查与开发自动化技能，集成 Codex 引擎
- feat(coding-master): 模块化 SOP 架构（深度审查、Bugfix、Feature 开发）
- feat(coding-master): 复杂度工作流、验收标准、工作区基线保证
- feat(coding-master): `analyze --repos` 支持无锁审查，工作区弹性增强
- fix(coding-master): 引擎命令超时设为 600s

### 核心功能增强
- feat(memory): 结构化记忆系统（Phase 1）—— LLM 提取关键事实、Token 级去重、自动合并归档
- feat(core): 技能阶段守护、邮箱错误 ACK、Session 锁加固
- feat(heartbeat): 自动清理 7 天以上的已完成/失败任务
- feat(session): 基于 LLM 的历史压缩器（滑动窗口）

### 多渠道
- feat(telegram): 照片识别（多模态视觉 API）、文档下载、媒体提取、URL 处理
- feat(telegram): telegramify-markdown 升级到 v1.0（entity-based 渲染）
- feat(telegram,core): Telegram 中显示 tool call，限制冗余搜索

### 投资与数据技能
- feat(investment-signal): 新增中国市场信号与箱体突破分析器
- fix(paper-discovery): 使用 HuggingFace JSON API 替代脆弱的 HTML 爬虫

### 工程改进
- refactor: 拆分单体模块，重组测试结构（unit/integration/web）
- test(coding-master): 新增深度审查 E2E 测试
- fix(tasks): 调度任务重试耗尽后重新激活
- fix(heartbeat): 过滤内联任务失败标记，不展示给用户

---

## v0.1.0 (2026-02-01)

### 新功能

#### 核心模块
- **AgentFactory**: Dolphin Agent 创建工厂
  - Dolphin SDK 集成，自动加载工作区指令
  - 支持自定义模型和配置

- **UserDataManager**: 统一数据管理
  - Agent 工作区管理、目录初始化、文件模板生成

- **WorkspaceLoader**: 工作区文件加载
  - 加载 AGENTS.md, HEARTBEAT.md, MEMORY.md, USER.md
  - 构建系统提示

- **SessionManager**: Session 管理
  - JSONL 格式持久化、并发锁（asyncio.Lock）、Session 恢复

- **HistoryManager**: History 管理
  - 裁剪（保留最近 10 轮）、归档到 MEMORY.md

- **HeartbeatRunner**: 心跳运行器
  - 定时触发、活跃时段控制、重试机制

- **EverBotDaemon**: 守护进程
  - 多 Agent 管理、心跳调度、信号处理、状态快照

- **CLI**: 命令行接口
  - init / list / start / stop / restart / status / doctor / config

- **Web Dashboard**: FastAPI + WebSocket
  - 实时对话、Agent/Session 管理 API、API Key 认证

#### 技能系统
- **coding-master**: 代码审查与开发自动化（深度审查 SOP、Bugfix、Feature）
- **routine-manager**: 任务调度（Cron/Interval、时区感知）
- **investment-signal**: 市场分析（宏观流动性、价值投资、中国市场信号）
- **daily-attractor**: 每日市场监控与 Telegram 推送
- **paper-discovery**: AI/ML 论文发现（HuggingFace + arXiv）
- **dev-browser**: 浏览器自动化（持久页面状态、ARIA 快照）
- **skill-installer**: 动态技能管理
- **tushare**: 中国财经数据接口

#### 测试
- ~55 个测试文件
  - `tests/unit/` — 单元测试（~38 个）
  - `tests/integration/` — 集成测试（~10 个）
  - `tests/web/` — 端到端测试（~5 个）

#### 文档
- README.md / QUICKSTART.md: 使用文档
- docs/EVERBOT_DESIGN.md: 架构设计（v1.1）
- docs/runtime_design.md: 运行时设计
- docs/memory_system_design.md: 记忆系统设计
- docs/channel_design.md: 多渠道设计
- docs/SKILLS_GUIDE.md: 技能开发指南

### 技术栈

- Python 3.10+
- Dolphin SDK（Agent 运行时）
- FastAPI + Uvicorn（Web 服务）
- asyncio（并发控制）
- PyYAML（配置）
- httpx（HTTP 客户端）
- pytest（测试）

### 下一步

- [ ] macOS launchd 深度集成
- [ ] Metrics 和监控告警
- [ ] 多用户权限管理
- [ ] 技能市场（远程注册表）
- [ ] 高级记忆功能（RAG、向量检索）
