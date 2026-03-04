# Ops Skill 设计文档

## 1. 概述

ops 是一个面向 Alfred (EverBot) 运维场景的技能，提供对指定环境的 Alfred 实例进行常见运维操作和可观测性查询的能力。

核心目标：

- **运维操作**：通过 `bin/everbot` CLI 对目标环境执行启停、重启、配置查看等管理操作
- **可观测性**：查询 daemon 运行状态、agent 心跳健康度、任务执行情况、日志与轨迹分析
- **环境感知**：支持对多个环境（本地、远程）的 Alfred 实例进行操作

## 2. 技能定位

| 维度 | 说明 |
|------|------|
| 执行模式 | External skill（通过 bash 脚本执行） |
| 典型使用者 | Alfred agent 自身或人类运维人员 |
| 触发场景 | 用户请求查看状态、排查问题、执行运维操作 |
| 输出格式 | JSON（机器可读）+ 人类可读摘要 |

## 3. 功能模块

### 3.1 生命周期管理

对 Alfred daemon 和 Web 服务的启停控制。

| 操作 | 说明 | 对应 CLI |
|------|------|----------|
| `start` | 启动 daemon + Web | `bin/everbot start` |
| `stop` | 停止 daemon + Web | `bin/everbot stop` |
| `restart` | 重启 | `bin/everbot restart` |

### 3.2 状态查询（可观测性核心）

| 操作 | 说明 | 数据来源 |
|------|------|----------|
| `status` | daemon 运行状态、PID、运行时长 | `everbot.status.json` |
| `agents` | 已注册 agent 列表及基本信息 | `everbot.status.json` + agent 工作区 |
| `heartbeat` | 各 agent 心跳状态、最近执行结果 | `everbot.status.json` heartbeats 字段 |
| `tasks` | 各 agent 的 HEARTBEAT.md 任务列表及状态 | `HEARTBEAT.md` JSON block / `task_states` |
| `metrics` | 运行时指标（session 数、LLM 延迟、tool 调用量） | `everbot.status.json` metrics 字段 |
| `logs` | 查看最近日志（daemon、heartbeat、web） | `~/.alfred/logs/` 日志文件 |
| `trajectory` | 最近轨迹分析（错误、循环、延迟尖峰） | trajectory 文件 + trajectory-reviewer |

### 3.3 配置查询

| 操作 | 说明 |
|------|------|
| `config` | 查看当前运行配置 |
| `agent-config` | 查看指定 agent 的 AGENTS.md / agent.dph 配置 |

### 3.4 诊断

| 操作 | 说明 | 对应 CLI |
|------|------|----------|
| `doctor` | 环境自检（依赖、配置、技能完整性） | `bin/everbot doctor` |
| `diagnose` | 综合诊断（status + heartbeat + logs + trajectory 的聚合分析） | 组合调用 |

## 4. 架构设计

### 4.1 目录结构

```
skills/ops/
├── SKILL.md                    # 技能元数据 + 使用说明
├── scripts/
│   ├── ops_cli.py             # 主入口 dispatcher
│   ├── lifecycle.py           # 启停操作
│   ├── observe.py             # 可观测性查询
│   └── diagnose.py            # 诊断分析
└── references/
    └── runbook.md             # 常见问题 runbook
```

### 4.2 调用方式

```bash
# 通过 dispatcher 统一入口
python skills/ops/scripts/ops_cli.py <command> [options]

# 示例
python skills/ops/scripts/ops_cli.py status
python skills/ops/scripts/ops_cli.py heartbeat --agent daily_insight
python skills/ops/scripts/ops_cli.py logs --source heartbeat --tail 50
python skills/ops/scripts/ops_cli.py diagnose --agent daily_insight
```

### 4.3 环境定位

#### 4.3.1 项目路径发现（`project_root`）

ops skill 需要定位 `bin/everbot` 所在的项目代码路径。当前这个路径没有被持久化记录。

**方案**：daemon 启动时将 `PROJECT_ROOT`（由 `bin/everbot` 脚本通过 `SCRIPT_DIR/..` 计算得到）写入 `~/.alfred/everbot.status.json` 的 `project_root` 字段。

写入时机：`bin/everbot` 启动 daemon 前通过环境变量 `ALFRED_PROJECT_ROOT` 传递给 Python 进程，daemon 在 `_write_status_snapshot()` 中将其写入 status 快照。

**变更点**：

1. `bin/everbot`：在 `start` 分支中 `export ALFRED_PROJECT_ROOT="${PROJECT_ROOT}"`
2. `src/everbot/cli/daemon.py`：`_write_status_snapshot()` 中读取 `os.environ.get("ALFRED_PROJECT_ROOT")` 写入 snapshot
3. ops skill：从 `everbot.status.json` 的 `project_root` 读取，拼接 `bin/everbot` 路径

**status snapshot 新增字段**：

```json
{
  "status": "running",
  "project_root": "/Users/xupeng/dev/github/alfred",
  "pid": 12345,
  ...
}
```

ops skill 通过此字段定位 `bin/everbot`：

```python
project_root = snapshot["project_root"]
everbot_bin = f"{project_root}/bin/everbot"
```

#### 4.3.2 多环境支持

默认操作本地环境（`ALFRED_HOME=~/.alfred`）。通过 `--env` 参数支持远程环境：

```bash
# 本地（默认）
python skills/ops/scripts/ops_cli.py status

# 远程（通过 SSH）
python skills/ops/scripts/ops_cli.py status --env remote --host user@server
```

远程模式通过 SSH 执行 `bin/everbot` 命令，要求目标机器已部署 Alfred。远程环境的 `project_root` 同样从目标机器的 `everbot.status.json` 中获取。

### 4.4 输出规范

所有命令输出统一 JSON 格式：

```json
{
  "ok": true,
  "command": "status",
  "data": {
    "running": true,
    "pid": 12345,
    "uptime_seconds": 3600,
    "agents": ["daily_insight", "code_reviewer"],
    "web": {"running": true, "url": "http://127.0.0.1:8765"}
  },
  "timestamp": "2026-03-04T10:00:00+08:00"
}
```

错误输出：

```json
{
  "ok": false,
  "command": "status",
  "error": "daemon not running",
  "hint": "Run 'bin/everbot start' to start the daemon"
}
```

## 5. `bin/everbot` 增强需求

当前 `bin/everbot status` 输出为人类可读文本，缺少机器可读的结构化接口。需要增强以下能力：

### 5.1 结构化输出

为 status 等查询命令增加 `--json` 标志，输出 JSON 格式：

```bash
bin/everbot status --json
```

### 5.2 新增可观测命令

| 命令 | 说明 |
|------|------|
| `bin/everbot metrics` | 输出运行时指标快照（JSON） |
| `bin/everbot tasks --agent NAME` | 输出指定 agent 的任务列表及状态（JSON） |
| `bin/everbot logs --source daemon\|heartbeat\|web --tail N` | 结构化日志查询 |

### 5.3 健康检查端点

在 Web API 中增加健康检查端点：

```
GET /api/health → daemon + agent 健康状态
GET /api/metrics → 运行时指标
GET /api/agents/{name}/tasks → agent 任务列表
```

## 6. 命令详细设计

### 6.1 `status`

聚合 daemon 进程状态、Web 状态、agent 概况。

```
输入: --env local (default) | --env remote --host <host>
输出:
  - daemon: running/stopped, pid, uptime
  - web: running/stopped, url
  - agents: 列表 + 各自最近心跳时间
  - scheduler: 下次调度时间
```

### 6.2 `heartbeat`

查看 agent 心跳详情。

```
输入: --agent <name> (可选，不指定则显示全部)
输出:
  - 各 agent 最近心跳时间戳
  - 心跳结果摘要
  - 连续失败次数
  - 下次预期心跳时间
```

### 6.3 `tasks`

查看 agent 的定时任务列表。

```
输入: --agent <name> (必选)
输出:
  - 任务列表: id, title, schedule, state, last_run_at, next_run_at
  - 失败任务高亮
  - 重试计数
```

### 6.4 `logs`

查看系统日志。

```
输入:
  --source daemon|heartbeat|web (默认 heartbeat)
  --tail N (默认 50)
  --level ERROR|WARNING|INFO (过滤级别)
  --agent <name> (按 agent 过滤)
输出:
  - 最近 N 行日志
  - 错误/警告统计
```

### 6.5 `trajectory`

分析最近执行轨迹。

```
输入:
  --agent <name> (可选)
  --limit-files N (默认 3)
输出:
  - 错误密度
  - 循环检测
  - 延迟尖峰
  - 可操作建议
```

调用现有 `trajectory-reviewer` skill 的脚本实现。

### 6.6 `diagnose`

综合诊断，聚合多项检查结果。

```
输入: --agent <name> (可选)
输出:
  - 整体健康评分 (healthy / degraded / unhealthy)
  - 各检查项结果:
    - daemon 状态
    - agent 心跳健康
    - 任务失败率
    - 最近错误统计
    - 资源使用（日志/轨迹文件大小）
  - 建议操作列表
```

## 7. 实现计划

### Phase 0: 基础设施（`project_root` 写入）

0. `bin/everbot` 导出 `ALFRED_PROJECT_ROOT` 环境变量
1. `daemon.py` 的 `_write_status_snapshot()` 将 `project_root` 写入 status 快照

### Phase 1: 基础框架 + 状态查询

2. 创建 `skills/ops/` 目录结构和 SKILL.md
3. 实现 `ops_cli.py` dispatcher
4. 实现 `status`、`agents`、`heartbeat` 命令（读取 `everbot.status.json`）
5. 为 `bin/everbot status` 增加 `--json` 输出

### Phase 2: 日志与任务查询

5. 实现 `tasks` 命令（解析 HEARTBEAT.md）
6. 实现 `logs` 命令（读取 + 过滤日志文件）
7. 实现 `metrics` 命令

### Phase 3: 诊断 + 轨迹

8. 实现 `trajectory` 命令（复用 trajectory-reviewer）
9. 实现 `diagnose` 综合诊断
10. 编写 runbook

### Phase 4: 远程 + Web API

11. 支持 `--env remote` SSH 模式
12. Web API 健康检查端点
13. `bin/everbot` 新增 `metrics`、`tasks` 子命令

## 8. 测试方式与用例

### 8.1 测试策略

| 层级 | 方式 | 覆盖范围 |
|------|------|----------|
| 单元测试 | pytest | 各模块的解析逻辑、输出格式化、错误处理 |
| 集成测试 | pytest + fixture | ops_cli dispatcher 端到端调用 |
| 手动验收 | 本地 daemon 环境 | 真实 daemon 启停、状态查询 |

### 8.2 单元测试用例

#### `observe.py` — 状态查询模块

| 用例 | 输入 | 预期输出 |
|------|------|----------|
| 正常 status 解析 | 合法 `everbot.status.json` 内容 | `{"ok": true, "data": {"running": true, "pid": ..., "project_root": ...}}` |
| daemon 未运行 | status 文件不存在或 `status=stopped` | `{"ok": false, "error": "daemon not running"}` |
| status 文件损坏 | 非法 JSON | `{"ok": false, "error": "corrupted status file"}` |
| heartbeat 正常查询 | status 中含 heartbeats 字段 | 返回各 agent 心跳时间、结果摘要 |
| heartbeat 指定 agent | `--agent daily_insight` | 仅返回该 agent 的心跳信息 |
| heartbeat agent 不存在 | `--agent nonexistent` | `{"ok": false, "error": "agent not found"}` |
| tasks 解析 | 合法 HEARTBEAT.md | 返回任务列表含 id, title, state, schedule |
| tasks 空列表 | HEARTBEAT.md 无任务 | `{"ok": true, "data": {"tasks": []}}` |
| logs 读取 | 存在日志文件 | 返回最近 N 行 |
| logs 文件不存在 | 日志文件缺失 | `{"ok": false, "error": "log file not found"}` |
| logs 按级别过滤 | `--level ERROR` | 仅返回 ERROR 级别日志行 |
| metrics 查询 | status 中含 metrics 字段 | 返回 session_count、latency 等指标 |

#### `lifecycle.py` — 生命周期模块

| 用例 | 输入 | 预期输出 |
|------|------|----------|
| project_root 发现 | status 中含 `project_root` | 正确拼接 `bin/everbot` 路径 |
| project_root 缺失 | status 中无 `project_root` 字段 | `{"ok": false, "error": "project_root not found in status"}` |
| start 命令构建 | 各种参数组合 | 生成正确的 shell 命令字符串 |

#### `diagnose.py` — 诊断模块

| 用例 | 输入 | 预期输出 |
|------|------|----------|
| 全部健康 | daemon running + 心跳正常 + 无失败任务 | `health: "healthy"` |
| 心跳超时 | 最近心跳 > 2 倍 interval | `health: "degraded"`, 含心跳超时告警 |
| 多项异常 | daemon stopped + 任务失败 | `health: "unhealthy"`, 含多条告警 |

#### `ops_cli.py` — Dispatcher

| 用例 | 输入 | 预期输出 |
|------|------|----------|
| 未知命令 | `ops_cli.py foobar` | `{"ok": false, "error": "unknown command: foobar"}` |
| 缺少必选参数 | `ops_cli.py tasks`（无 `--agent`） | `{"ok": false, "error": "missing required argument: --agent"}` |

### 8.3 集成测试用例

使用 pytest fixture 准备模拟的 `~/.alfred` 环境（临时目录）：

| 用例 | 步骤 | 验证 |
|------|------|------|
| status 端到端 | 写入模拟 status.json → 调用 `ops_cli.py status` | stdout 为合法 JSON，`ok=true` |
| heartbeat 端到端 | 写入含 heartbeats 的 status.json → 调用 `ops_cli.py heartbeat` | 返回心跳数据 |
| tasks 端到端 | 写入模拟 HEARTBEAT.md → 调用 `ops_cli.py tasks --agent test` | 返回任务列表 |
| logs 端到端 | 写入模拟日志文件 → 调用 `ops_cli.py logs --tail 10` | 返回 10 行日志 |
| diagnose 端到端 | 写入完整模拟环境 → 调用 `ops_cli.py diagnose` | 返回健康评分 + 检查项 |
| 远程模式 mock | mock SSH 命令 → 调用 `ops_cli.py status --env remote --host mock` | 通过 SSH 执行并返回结果 |

### 8.4 测试目录结构

```
skills/ops/tests/
├── conftest.py                 # fixture: 临时 ALFRED_HOME、模拟 status.json 等
├── test_observe.py             # 可观测性模块单元测试
├── test_lifecycle.py           # 生命周期模块单元测试
├── test_diagnose.py            # 诊断模块单元测试
├── test_ops_cli.py             # dispatcher 集成测试
└── fixtures/
    ├── status_running.json     # 正常运行的 status 快照
    ├── status_stopped.json     # 停止状态的 status 快照
    ├── heartbeat_sample.md     # 模拟 HEARTBEAT.md
    └── sample_logs/            # 模拟日志文件
```

### 8.5 运行方式

```bash
# 运行全部 ops skill 测试
pytest skills/ops/tests/ -v

# 仅运行单元测试
pytest skills/ops/tests/ -v -k "not integration"

# 仅运行集成测试
pytest skills/ops/tests/ -v -k "integration"
```

## 9. 与现有技能的关系

| 技能 | 关系 |
|------|------|
| `trajectory-reviewer` | ops 的 `trajectory` 命令复用其脚本 |
| `routine-manager` | ops 的 `tasks` 命令读取相同的 HEARTBEAT.md 数据 |
| `alfred_debug` | ops 侧重运维视角（进程级），alfred_debug 侧重问题排查（逻辑级），互补 |
