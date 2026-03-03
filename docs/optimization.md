# Alfred EverBot 架构优化清单

> 基于 2026-02-23 全项目架构 Review，按严重程度分级。

---

## 🔴 高优先级

### H1. Web API 无认证与安全防护

**现状：**
- FastAPI 应用未配置 CORS 中间件
- 无任何认证机制（API Key / JWT / Basic Auth）
- 无速率限制
- 所有路由（含 WebSocket）公开可访问
- `request.json()` 未做 schema 校验，无输入大小限制
- 错误堆栈直接输出到 stdout，可能泄露敏感信息

**涉及文件：**
- `src/everbot/web/app.py`
- `src/everbot/web/services/chat_service.py`

**风险：** 恶意请求可直接操控 agent、读取 session 数据、触发任意 heartbeat。

---

### H2. Telegram 媒体下载无安全校验

**现状：**
- 文件下载无大小限制
- 无文件类型白名单校验
- 文件名未做清洗，存在路径穿越（path traversal）风险
- 下载目录直接写入 `~/.alfred/agents/{name}/tmp/documents/`

**涉及文件：**
- `src/everbot/channels/telegram_channel.py` (L778-866)

**风险：** 恶意用户可通过构造文件名写入任意路径，或通过大文件耗尽磁盘。

---

### H3. 依赖版本未锁定

**现状：**
- `requirements.txt` 中所有核心依赖（fastapi, uvicorn, httpx 等）均未指定版本
- `telegramify-markdown>=1.0.0rc0` 依赖了 pre-release 版本
- Dolphin SDK 未列入依赖（仅注释说明从源码安装）
- 缺少 dev/test 依赖声明（pytest, pytest-asyncio）

**涉及文件：**
- `requirements.txt`

**风险：** `pip install` 可能拉取不兼容版本，导致生产环境不可复现的故障。

---

### H4. HeartbeatRunner 上帝类

**现状：**
- 1772 LOC，40+ 方法，13 个构造参数
- 同时承担：任务执行、session 管理、事件录制、反思逻辑、routine 管理、LLM 交互
- `_execute_once()` 180 LOC / 5 层嵌套
- `_execute_structured_tasks()` 130 LOC / 5+ 层嵌套

**涉及文件：**
- `src/everbot/core/runtime/heartbeat.py`

**风险：** 极难测试和维护，任何修改都可能引发连锁 bug。

---

## 🟡 中优先级

### M1. 异常处理过于宽泛且静默

**现状：**
- 全项目大量 `except Exception:` + 静默吞掉异常
- `_is_permanent_error()` 通过字符串匹配判断错误类型
- config 加载失败静默返回默认值，不抛异常

**涉及文件：**
- `heartbeat.py`, `factory.py`, `scheduler.py`, `session.py`, `config.py`

**风险：** 错误被隐藏，线上问题难以排查。

---

### M2. SessionManager 职责过重

**现状：**
- 557+ LOC，30+ 方法
- 同时负责：进程内锁、跨进程锁、agent 缓存、timeline 事件、指标收集、邮箱事件（含去重）、session 归档、历史迁移
- 双层锁协议（asyncio.Lock + fcntl.flock）增加死锁风险

**涉及文件：**
- `src/everbot/core/session/session.py`

---

### M3. 配置管理无统一注入模式

**现状：**
- 无依赖注入框架，`load_config()` 被多处独立调用
- `UserDataManager` 在多个地方被直接实例化
- 配置文件无完整性校验、无权限检查

**涉及文件：**
- `src/everbot/infra/config.py`
- `src/everbot/core/agent/factory.py`

---

### M4. 模块间循环耦合

**现状：**
- HeartbeatRunner ↔ SessionManager 双向依赖
- HeartbeatRunner → RoutineManager → TaskManager 链式依赖
- 多个模块共同读写 `HEARTBEAT.md`，无统一访问层
- TaskState 枚举值以字符串形式跨模块传递

---

### M5. Dolphin SDK 补丁管理分散

**现状：**
- 运行时 monkey-patch 分散在多个入口点（web 启动、agent 创建）
- 无集中的补丁注册机制，无测试覆盖
- 补丁失败仅 print 到 stdout，不走日志系统

**涉及文件：**
- `src/everbot/infra/dolphin_patches.py`
- `src/everbot/infra/dolphin_compat.py`

---

### M6. 信号处理存在潜在 Bug

**现状：**
- `lambda: asyncio.create_task(daemon.stop())` 创建异步任务但不 await
- 进程可能在 `stop()` 完成前退出，导致资源未释放
- PID 检测使用 `os.kill(pid, 0)` 存在 PID 复用竞态

**涉及文件：**
- `src/everbot/cli/daemon.py` (L447-448)

---

## 🟢 低优先级

### L1. 魔法数字散布各处

**现状：**
- `MAX_RESTORED_HISTORY_MESSAGES = 120`, `MAX_TIMELINE_EVENTS = 500`, `_MAX_CACHED_LOCKS = 200` 等常量分散定义
- TurnPolicy 默认值硬编码（`max_attempts=3`, `max_tool_calls=14`）
- 截断逻辑中的 `0.6`, `50` 等魔法数字

---

### L2. 测试覆盖空白

**缺失测试：**
- DaemonLock 进程锁
- Dolphin patches 补丁逻辑
- Web 安全（CORS / auth / 恶意输入）
- 文件系统错误处理（权限拒绝、磁盘满）
- Telegram 媒体下载安全校验

---

### L3. AgentFactory 方法过长

**现状：**
- `create_agent()` 100+ LOC
- `_create_agent_config()` 60+ LOC
- 包含遗留 YAML → DPH 迁移逻辑与主流程混杂
- 技能过滤逻辑可提取为独立组件

**涉及文件：**
- `src/everbot/core/agent/factory.py`

---

### L4. 硬编码模板和路径

**现状：**
- `user_data.py` 内嵌大段 Markdown 模板文本（L165-207）
- 配置候选路径硬编码而非参数化
- 全局 skills 目录路径从 `__file__` 推导

---

### L5. Web 应用全局状态无清理

**现状：**
- `_tasks: Dict[str, str] = {}` 全局 dict 追踪异步任务，无过期清理
- 背景任务通过 `asyncio.create_task()` 创建，无 cancel/wait 管理

---

## 修复优先级建议

| 阶段 | 目标 | 涉及项 |
|------|------|--------|
| Phase 1 | 安全加固 | H1, H2, H3 |
| Phase 2 | 核心模块拆分 | H4, M2 |
| Phase 3 | 稳定性提升 | M1, M5, M6 |
| Phase 4 | 架构优化 | M3, M4 |
| Phase 5 | 代码质量 | L1 ~ L5 |
