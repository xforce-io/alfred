# Coding Master 技能设计文档

> **版本**: v0.2 (Draft)
> **创建时间**: 2026-02-24
> **状态**: 设计中

---

## 目录

1. [概述与目标](#一概述与目标)
2. [核心概念](#二核心概念)
3. [配置系统](#三配置系统)
4. [Workspace 管理](#四workspace-管理)
5. [Env 管理](#五env-管理)
6. [Coding Engine 集成](#六coding-engine-集成)
7. [工作流设计](#七工作流设计)
8. [模块设计](#八模块设计)
9. [Telegram 交互协议](#九telegram-交互协议)
10. [安全与约束](#十安全与约束)
11. [实现路线图](#十一实现路线图)

---

## 一、概述与目标

### 1.1 背景

当前 Alfred 的 skill 体系覆盖了信息获取、数据分析、浏览器自动化等场景，但缺少**自主编码**能力。

**Coding Master** 使 Agent 能够：

- 通过 Telegram 对话接收编码任务（bug 修复、功能开发、代码分析）
- 到运行环境 (Env) 采集问题现象，在开发环境 (Workspace) 分析代码并修复
- 拉分支、开发、提交 PR，全程人在回路

### 1.2 设计原则

1. **极简配置** — 一行能跑，需要时再展开细化
2. **Workspace / Env 分离** — 在哪改代码 ≠ 在哪看问题
3. **Engine 可选** — Claude Code / Codex，按任务特点选择
4. **人在回路** — 每个阶段等用户确认，agent 不擅自推进
5. **Telegram 可操作** — 配置增删改通过对话完成，无需手动编辑文件

### 1.3 范围

**v0.1**：Workspace + Env 管理、Claude Code 集成、单任务线性工作流、Telegram 配置管理

**v0.2**：Codex 集成、Engine 选择策略、Git worktree 并行任务

---

## 二、核心概念

### 2.1 Workspace vs Env

```
Workspace (在哪改代码)              Env (在哪看问题)
─────────────────────              ─────────────────
本地开发目录                        代码实际运行的环境
├── 代码仓库 (git repo)            ├── 日志 / 监控
├── 编辑 / 构建 / 测试             ├── 进程状态 / 资源占用
├── 分支管理                       ├── 配置 / 环境变量
└── 提交 PR                        └── 数据库 / 队列 / 存储

访问方式: 本地文件系统               访问方式: 本地 或 SSH
```

同一个项目的 workspace 和 env 可以在不同机器上：

| 场景 | Workspace | Env |
|------|-----------|-----|
| 本地开发 bug | 本地 ~/dev/alfred | 本地 (同 workspace) |
| 线上问题排查 | 本地 ~/dev/alfred | SSH → prod-server |
| 功能开发 | 本地 ~/dev/alfred | 无需 Env（跳过探测） |

### 2.2 在 EverBot 中的定位

```
EverBot Daemon
├── TelegramChannel          ← 用户入口
├── Agent (Dolphin)          ← 意图理解、任务编排
│   └── coding-master        ← 自主编码 skill
│       ├── workspace 管理   ← 本地开发环境
│       ├── env 管理         ← 运行环境（本地/SSH）
│       ├── coding engine    ← Claude Code / Codex
│       └── git 操作         ← 分支、提交、PR
└── HeartbeatRunner          ← 定时任务
```

Agent 负责意图理解和阶段编排；Coding Engine 负责实际的代码分析和编写。

---

## 三、配置系统

### 3.1 极简配置 + 扩展配置

配置存储在 `~/.alfred/config.yaml` 的 `coding_master` 段。支持两种写法，可以混用：

**极简配置**（值为字符串）：

```yaml
coding_master:
  workspaces:
    alfred: ~/dev/github/alfred
    my-app: ~/dev/my-app

  envs:
    alfred-local: ~/dev/github/alfred
    alfred-prod: deploy@prod-server:/opt/alfred
    my-app-staging: dev@staging:/opt/my-app
```

**扩展配置**（值为字典，需要细化时展开）：

```yaml
coding_master:
  workspaces:
    alfred:
      path: ~/dev/github/alfred
      default_env: alfred-prod
      test_command: pytest -x
      lint_command: ruff check .
      branch_prefix: fix/

  envs:
    alfred-prod:
      connect: deploy@prod-server:/opt/alfred
      log: /opt/alfred/logs/daemon.log
      service: alfred-daemon

  default_engine: claude    # claude | codex
  max_turns: 30
```

解析规则：值是 string → 极简模式；值是 dict → 扩展模式。

极简 env 格式：

- 本地：`/absolute/path` → type=local
- SSH：`user@host:/path` → type=ssh（scp 风格）

### 3.2 Workspace ↔ Env 自动关联

不需要显式配 `default_env`，靠命名约定匹配：

```
workspace "alfred" → 自动匹配 env "alfred-local" 或 "alfred-*"
```

用户说"看看线上"时，agent 从 `alfred-*` 中选 `alfred-prod`。

扩展配置中可以用 `default_env` 覆盖自动匹配。

### 3.3 通过 Telegram 管理配置

dispatch.py 提供 `config-*` 子命令，Agent 通过 `_bash()` 调用：

```bash
# 增
python dispatch.py config-add workspace my-app ~/dev/my-app
python dispatch.py config-add env my-app-prod root@server:/opt/my-app

# 改（自动从极简升级为扩展）
python dispatch.py config-set workspace alfred test_command "pytest -x"
python dispatch.py config-set env alfred-prod log "/opt/alfred/logs/daemon.log"

# 删
python dispatch.py config-remove workspace my-app
python dispatch.py config-remove env my-app-prod

# 查
python dispatch.py config-list
```

对话体验：

```
用户: 添加 workspace my-app ~/dev/my-app

Agent: ✅ 已添加 workspace:
  my-app → ~/dev/my-app

用户: 添加 env my-app-prod root@server:/opt/my-app

Agent: ✅ 已添加 env:
  my-app-prod → root@server:/opt/my-app
  正在检查 SSH 连通性... ✅ 可达

用户: 设置 alfred test_command "pytest -x"

Agent: ✅ 已更新 workspace alfred:
  alfred:
    path: ~/dev/github/alfred
    test_command: pytest -x        ← 新增
  (已从极简升级为扩展配置)

用户: 列出所有环境

Agent:
  Workspaces:
    alfred     ~/dev/github/alfred          [idle]
    my-app     ~/dev/my-app                 [idle]

  Envs:
    alfred-local   ~/dev/github/alfred                 [local]
    alfred-prod    deploy@prod-server:/opt/alfred       [ssh ✅]
    my-app-prod    root@server:/opt/my-app              [ssh ✅]
```

### 3.4 无需热加载

dispatch.py 每次通过 `_bash()` 调用都是独立进程，天然读取最新的 config.yaml。不存在缓存、不需要 reload 信号、不需要重启 daemon。

写入 config.yaml 时使用 atomic write（写临时文件 → rename），与现有 session persistence 策略一致。

---

## 四、Workspace 管理

### 4.1 Lock 文件

每个 workspace 使用 `.coding-master.lock` 标记占用状态。

**位置**：`{workspace_path}/.coding-master.lock`

**内容**：

```json
{
  "task": "fix: heartbeat 定时任务未触发",
  "branch": "fix/heartbeat-trigger",
  "engine": "claude",
  "env": "alfred-prod",
  "phase": "developing",
  "phase_history": [
    {"phase": "workspace-check", "completed_at": "2026-02-24T10:30:00Z"},
    {"phase": "env-probe", "completed_at": "2026-02-24T10:31:00Z"},
    {"phase": "analyzing", "completed_at": "2026-02-24T10:33:00Z"},
    {"phase": "confirmed", "completed_at": "2026-02-24T10:35:00Z"}
  ],
  "started_at": "2026-02-24T10:30:00Z",
  "pid": 12345
}
```

**生命周期**：

```
idle (无 lock 文件)
  │  acquire()
  ▼
busy (lock 文件存在)
  ├── 正常完成 → release() → 删除 lock
  ├── 用户取消 → release() → 删除 lock + git cleanup
  └── 进程崩溃 → 下次 acquire 检测 pid 不存活 → 僵尸锁自动清理
```

### 4.2 Workspace 探测

锁定 workspace 后，脚本自动探测开发环境（不消耗 LLM token）：

```json
{
  "workspace": { "name": "alfred", "path": "/Users/xupeng/dev/github/alfred" },
  "git": {
    "branch": "main",
    "dirty": false,
    "remote_url": "git@github.com:user/alfred.git",
    "last_commit": "254c41b fix(paper-discovery): ..."
  },
  "runtime": { "type": "python", "version": "3.12.4", "package_manager": "uv" },
  "project": { "test_command": "pytest", "lint_command": "ruff check ." }
}
```

runtime 和 project 信息通过文件特征自动发现（pyproject.toml / package.json / Cargo.toml 等），扩展配置中的 `test_command` / `lint_command` 可覆盖自动发现结果。

### 4.3 并行任务（v0.2）

主 workspace 被占用时，使用 git worktree 创建隔离副本。v0.1 锁定时直接提示用户等待。

---

## 五、Env 管理

### 5.1 设计思路

Env 是问题排查的入口 — 只读采集信息，不修改运行环境。

用户报告 "线上有 bug" 时，Agent 先去 Env 采集现象（日志、进程状态、错误信息），再带着线索回到 Workspace 分析代码。

### 5.2 访问方式

根据配置的 type 自动选择：

- **本地 Env**（path 是绝对路径）：直接 `subprocess.run(cmd, cwd=path)`
- **SSH Env**（`user@host:path` 格式）：`ssh user@host 'cd path && cmd'`

SSH 依赖 `~/.ssh/config` 和密钥认证，不支持密码交互。

### 5.3 自动探测

到了目标目录后，**自动发现**项目结构和运行状态，不需要用户配置模块列表：

```python
def auto_probe(env_path: str) -> EnvSnapshot:
    """自动探测，不消耗 LLM token"""

    # 1. 多模块发现（零配置）
    if exists("docker-compose.yml"):
        modules = parse_docker_compose()
    elif exists("Procfile"):
        modules = parse_procfile()
    else:
        modules = [{"name": basename(env_path)}]

    # 2. 每个模块探测进程、日志、错误
    for module in modules:
        module["process"] = ps_grep(module["name"])
        module["log"] = find_logs(module["path"])  # logs/*.log, /var/log/{name}/*.log
        module["errors"] = grep_errors(module["log"])

    # 3. 通用信息
    return {
        "modules": modules,
        "uptime": run("uptime"),
        "disk": run(f"df -h {env_path}"),
    }
```

多模块系统（如微服务）的发现策略：

| 标志文件 | 识别方式 |
|----------|----------|
| `docker-compose.yml` | 解析 services |
| `Procfile` | 解析进程定义 |
| `systemd/*.service` 或配置中的 `service` 字段 | 查询 systemd |
| 无特殊标志 | 当作单模块处理 |

扩展配置中的 `log` / `service` 字段可以覆盖自动发现。

### 5.4 定向探测

Coding Engine 分析时如果需要更多线索，Agent 可以执行定向命令：

```bash
python dispatch.py env-probe --env alfred-prod \
  --commands "journalctl -u alfred --since '2 hours ago'" \
             "cat /opt/alfred/config.yaml"
```

### 5.5 Env Snapshot 结构

```json
{
  "env": { "name": "alfred-prod", "type": "ssh", "connect": "deploy@prod-server:/opt/alfred" },
  "probed_at": "2026-02-24T10:30:00Z",
  "modules": [
    {
      "name": "daemon",
      "process": { "running": true, "pid": 5678, "uptime": "3 days" },
      "recent_errors": [
        "10:15 ERROR heartbeat: Task 'daily-report' skipped",
        "09:45 ERROR heartbeat: Task 'paper-digest' skipped"
      ],
      "log_tail": "... (最近 50 行) ..."
    }
  ],
  "disk_usage": "45% of 100GB",
  "custom_probes": {}
}
```

---

## 六、Coding Engine 集成

### 6.1 Engine 抽象

```python
class CodingEngine(ABC):
    @abstractmethod
    async def run(self, repo_path: str, task: str, context: dict,
                  max_turns: int = 30) -> EngineResult: ...

@dataclass
class EngineResult:
    success: bool
    summary: str           # 人类可读的执行摘要
    files_changed: list    # 修改的文件列表
    error: str | None
```

### 6.2 Claude Code Engine

```bash
claude -p "<prompt>" \
  --allowedTools "Bash,Read,Edit,Write,Glob,Grep" \
  --output-format json \
  --max-turns 30
```

使用 `asyncio.create_subprocess_exec` 异步执行，超时默认 10 分钟。

Workspace 探测结果和 Env Snapshot 注入到 prompt 中，避免 engine 浪费 turns 探测环境。

### 6.3 Codex Engine（v0.2）

```bash
codex --approval-mode full-auto --quiet "<prompt>"
```

### 6.4 Engine 选择

| 场景 | 推荐 Engine | 原因 |
|------|-------------|------|
| 复杂 debug / 多文件分析 | Claude Code | 上下文理解深，工具调用强 |
| 明确的单文件修改 | Codex | 快，token 成本低 |
| 对比方案 | 两者都跑 | v0.2 |

v0.1 默认 Claude Code。

---

## 七、工作流设计

### 7.1 阶段总览

```
Phase 0           Phase 1         Phase 2        Phase 3        Phase 4        Phase 5
Workspace 确认 →  Env 探测    →   问题分析   →   方案确认   →   编码开发   →   提交 PR
(脚本)            (脚本/SSH)       (engine)       (人工)         (engine)       (脚本)
  │                 │                │              │              │              │
  ▼                 ▼                ▼              ▼              ▼              ▼
Workspace 快照   Env Snapshot     诊断报告      用户确认       代码变更       PR URL
```

每个阶段结果都发到 Telegram，等用户确认后再继续。

### 7.2 Phase 0: Workspace 确认

**执行者**：脚本（不消耗 token）

匹配 workspace → 检查 lock → 探测 git/runtime/project → 报告状态。

**阻断条件**：path 不存在、不是 git repo、已被 lock、有未提交变更。

### 7.3 Phase 1: Env 探测

**执行者**：脚本/SSH（不消耗 token）

通过命名约定匹配 env（或用户指定）→ 自动探测模块/进程/日志/错误。

**可跳过**：功能开发等不需要排查现象的场景，Agent 智能判断。

### 7.4 Phase 2: 问题分析

**执行者**：Coding Engine

将 Workspace 快照 + Env Snapshot + 用户描述注入 prompt，Engine 在 workspace 中分析代码：

```
## 开发环境 (Workspace)
{workspace_snapshot}

## 运行环境观测 (Env)
{env_snapshot}

## 任务
分析以下问题，不要修改任何代码。
问题描述：{user_issue}

请输出：
1. 问题定位：涉及哪些文件、函数
2. 根因分析：结合运行环境日志
3. 修复方案（可多个）
4. 影响范围
5. 风险评估（低/中/高）
6. 是否需要更多 Env 信息
```

如果 Engine 请求更多 Env 信息，Agent 执行定向 env-probe 后再次喂给 Engine（迭代分析）。

### 7.5 Phase 3: 方案确认

**执行者**：用户（Telegram）

- "继续" → Phase 4
- "用方案 2" → 指定方案后 Phase 4
- "再看看线上日志" → 定向 env-probe → 补充后重跑 Phase 2
- "取消" → 释放 lock

### 7.6 Phase 4: 编码开发

**执行者**：Coding Engine

前置：`git checkout -b fix/{issue-slug}`

Engine 根据诊断报告编码修复。

后置：自动运行 test + lint，结果反馈到 Telegram。

### 7.7 Phase 5: 提交 PR

**执行者**：脚本

`git add` → `git commit` → `git push` → `gh pr create`

PR body 包含诊断摘要、变更列表、测试结果。PR URL 发回 Telegram。

释放 workspace lock。

---

## 八、模块设计

### 8.1 目录结构

```
skills/coding-master/
├── SKILL.md
├── scripts/
│   ├── dispatch.py             # 统一 CLI 入口
│   ├── workspace.py            # Workspace 管理 + lock
│   ├── env_probe.py            # Env 探测（本地 + SSH + 自动发现）
│   ├── config_manager.py       # 配置 CRUD（供 Telegram 操作）
│   ├── git_ops.py              # Git 操作（分支、提交、PR）
│   └── engine/
│       ├── __init__.py         # CodingEngine 抽象
│       ├── claude_runner.py    # Claude Code headless
│       └── codex_runner.py     # Codex CLI（v0.2）
└── README.md
```

### 8.2 dispatch.py — 统一 CLI 入口

```bash
# 配置管理
dispatch.py config-list
dispatch.py config-add workspace alfred ~/dev/github/alfred
dispatch.py config-add env alfred-prod deploy@prod-server:/opt/alfred
dispatch.py config-set workspace alfred test_command "pytest -x"
dispatch.py config-remove env alfred-staging

# 工作流
dispatch.py workspace-check --workspace alfred
dispatch.py env-probe --env alfred-prod
dispatch.py env-probe --env alfred-prod --commands "journalctl -u alfred ..."
dispatch.py analyze --workspace alfred --env alfred-prod --task "..." --engine claude
dispatch.py develop --workspace alfred --task "..." --branch fix/xxx --engine claude
dispatch.py submit-pr --workspace alfred --title "..." --body "..."
dispatch.py release --workspace alfred
```

所有输出统一 JSON stdout。

### 8.3 config_manager.py

```python
class ConfigManager:
    """config.yaml 的 coding_master 段 CRUD"""

    def __init__(self, config_path="~/.alfred/config.yaml"): ...
    def list_all(self) -> dict:                              ...
    def add_workspace(self, name: str, value: str) -> None:  ...
    def add_env(self, name: str, value: str) -> None:        ...
    def set_field(self, kind: str, name: str, key: str, value: str) -> None:
        """设置扩展字段，自动将极简配置升级为扩展配置"""
        ...
    def remove(self, kind: str, name: str) -> None:          ...
    def _atomic_write(self) -> None:
        """写临时文件 → rename，防止写坏"""
        ...
```

### 8.4 workspace.py

```python
class WorkspaceManager:
    def list_workspaces(self) -> list[dict]:    ...
    def resolve(self, hint: str) -> dict|None:  # 名称/关键词模糊匹配
    def acquire(self, name, task, engine) -> bool:
    def release(self, name) -> bool:
    def check(self, name) -> dict:              # 探测 git/runtime/project
```

### 8.5 env_probe.py

```python
class EnvProber:
    def list_envs(self) -> list[dict]:          ...
    def probe(self, name, extra_commands=None) -> dict:
        """自动探测 + 可选定向命令"""
    def _auto_discover_modules(self, path) -> list:
        """docker-compose / Procfile / systemd 自动发现"""
    def _probe_local(self, path, commands) -> dict:
    def _probe_ssh(self, connect, commands) -> dict:
    def _filter_dangerous(self, commands) -> list:
```

---

## 九、Telegram 交互协议

### 9.1 Bug 修复（远程 Env）

```
用户: alfred 线上的 heartbeat 有 bug，定时任务没触发

Agent: 📁 Workspace: alfred (~/dev/github/alfred)
       🖥️ Env: alfred-prod (ssh → prod-server:/opt/alfred)
       正在确认环境...

Agent: [Workspace] main 分支，无未提交变更，Python 3.12.4
       [Env: alfred-prod]
         daemon: 运行中 (pid 5678, 3d uptime)
         最近错误:
           10:15 ERROR heartbeat: Task 'daily-report' skipped
           09:45 ERROR heartbeat: Task 'paper-digest' skipped
       是否开始分析？

用户: 开始

Agent: 分析完成：
       📍 heartbeat.py:142 HeartbeatRunner._should_run_task()
       🔍 naive datetime 与 UTC 比较导致时区偏移
       💡 统一 timezone-aware datetime，风险: 低
       是否修复？

用户: 修吧

Agent: ✅ pytest 42 passed, ruff clean
       📝 heartbeat.py (+3, -2)
       是否提交 PR？

用户: 提交

Agent: PR 已创建: https://github.com/.../pull/15
       已释放 workspace alfred。
```

### 9.2 功能开发（跳过 Env）

```
用户: 帮我在 alfred 加个 workspace list 命令

Agent: 📁 Workspace: alfred, main 分支 ✓
       功能开发，跳过 Env 探测。开始分析？

用户: 开始
...
```

### 9.3 配置管理

```
用户: 添加 workspace my-app ~/dev/my-app
Agent: ✅ my-app → ~/dev/my-app

用户: 添加 env my-app-prod root@server:/opt/my-app
Agent: ✅ my-app-prod → root@server:/opt/my-app, SSH ✅ 可达

用户: 列出环境
Agent: Workspaces: alfred [idle], my-app [idle]
       Envs: alfred-local [local], alfred-prod [ssh ✅], my-app-prod [ssh ✅]
```

### 9.4 中断与恢复

| 场景 | Agent 行为 |
|------|-----------|
| Phase 0-2 中取消 | 释放 lock，无需 git 清理 |
| Phase 4 中取消 | 释放 lock，删除本地分支，checkout 回原分支 |
| Phase 5 后取消 | PR 已创建，提示用户手动 close |
| 长时间无回复 | lock 保留，下次对话时提示未完成任务 |

---

## 十、安全与约束

### 10.1 Coding Engine 权限

`--allowedTools "Bash,Read,Edit,Write,Glob,Grep"`，prompt 约束只在 workspace 内操作。

禁止：`rm -rf`、`git push --force`、`git reset --hard`、修改 `.env` / credentials。

### 10.2 Env 访问安全

- 严格只读，禁止写入/重启/部署
- 命令黑名单：`rm`、`kill`、`systemctl restart/stop`、`deploy`、`> file`、`chmod`
- 敏感信息自动过滤：SECRET/PASSWORD/TOKEN/KEY 值替换为 `***`
- 超时：单次命令 30s，整体探测 120s

### 10.3 Git 安全

- 只允许 feature/fix 分支，不直接 push main
- PR 不自动 merge，必须人工 review
- force push 默认禁止

### 10.4 成本控制

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `max_turns` | 30 | 单次 engine 调用最大轮次 |
| `timeout` | 600s | 单次 engine 调用超时 |
| `max_retries` | 1 | 失败重试次数 |

### 10.5 .gitignore

所有 workspace 需包含：`.coding-master.lock`

---

## 十一、实现路线图

### v0.1 — 基础能力

- [ ] SKILL.md
- [ ] config_manager.py — 极简/扩展配置解析 + Telegram CRUD
- [ ] workspace.py — 注册、lock、探测
- [ ] env_probe.py — 本地/SSH 探测 + 多模块自动发现
- [ ] dispatch.py — config-* + workspace-check + env-probe
- [ ] engine/claude_runner.py — Claude Code headless
- [ ] git_ops.py — 分支、提交、PR
- [ ] 端到端验证：Telegram → 配置 → 探测 → 分析 → 开发 → PR

### v0.2 — 扩展

- [ ] engine/codex_runner.py
- [ ] Engine 选择策略
- [ ] Git worktree 并行任务
- [ ] 双 engine 对比模式

### v0.3 — 增强

- [ ] CI 状态监控
- [ ] 任务历史与统计
- [ ] HEARTBEAT 集成（定期检查 issue 自动修复）
- [ ] Env 探测缓存
