# Coding Master 技能设计文档

> **文档版本**: rev8
> **创建时间**: 2026-02-24
> **最后更新**: 2026-02-27
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
8. [Feature 管理](#八feature-管理)
9. [工具接口设计](#九工具接口设计)
10. [模块设计](#十模块设计)
11. [交互协议](#十一交互协议)
12. [安全与约束](#十二安全与约束)
13. [实现路线图](#十三实现路线图)

---

## 一、概述与目标

### 1.1 背景

当前 Alfred 的 skill 体系覆盖了信息获取、数据分析、浏览器自动化等场景，但缺少**自主编码**能力。

**Coding Master** 使 Agent 能够：

- 通过对话接收编码任务（bug 修复、功能开发、代码分析）
- 到运行环境 (Env) 采集问题现象，在开发环境 (Workspace) 分析代码并修复
- 拉分支、开发、测试、提交 PR，全程人在回路

### 1.2 设计原则

1. **极简配置** — 一行能跑，需要时再展开细化
2. **Repo / Workspace / Env 分离** — 什么代码 ≠ 在哪改 ≠ 在哪看问题
3. **Engine 可选** — Claude Code / Codex，按任务特点选择
4. **人在回路** — 每个阶段等用户确认，agent 不擅自推进
5. **对话可操作** — 配置增删改通过对话完成，无需手动编辑文件

### 1.3 范围

**v0.1**：Repo + Workspace + Env 管理（含多 repo）、Claude Code 集成、单任务线性工作流、对话式配置管理

**v0.2**：Codex 集成、Engine 选择策略、Git worktree 并行任务、Repo 新建流程

---

## 二、核心概念

### 2.1 Repo vs Workspace vs Env

```
Repo (什么代码)                  Workspace (在哪改代码)          Env (在哪看问题)
────────────                    ─────────────────              ─────────────────
代码仓库的元信息                  本地工作目录（工作槽）            代码实际运行的环境
├── 远程 URL                    ├── 可容纳多个 repo clone        ├── 日志 / 监控
├── 默认分支                     ├── 编辑 / 构建 / 测试           ├── 进程状态 / 资源占用
├── test/lint 命令              ├── Lock（标记是否被占用）         ├── 配置 / 环境变量
└── 项目类型 (python/node/...)   └── 阶段产物 (.coding-master/)   └── 数据库 / 队列 / 存储
```

| 场景 | Repo | Workspace | Env |
|------|------|-----------|-----|
| 修 myapp 线上 bug | myapp | 自动分配空闲 workspace | myapp-prod (SSH) |
| 开发 myapp 新功能 | myapp | 自动分配空闲 workspace | 无需 |
| 新建项目 | 新建 repo | 自动分配空闲 workspace | 无需 |
| 前后端联调 | frontend + backend | 同一 workspace 内两个 repo | staging |

**目录结构**（workspace 内部）：

```
workspace (e.g., ~/lab/coding_master/env0)/
├── .coding-master.lock          # workspace 级别的 lock
├── .coding-master/              # 阶段产物
│   ├── workspace_snapshot.json
│   ├── env_snapshot.json
│   ├── feature_plan.json        # 任务拆分索引（complex 任务时生成）
│   ├── features/                # feature 级别的产物子目录
│   │   ├── 0/
│   │   │   ├── analysis.md
│   │   │   ├── criteria.json
│   │   │   ├── verification.json
│   │   │   └── test_report.json
│   │   └── 1/
│   │       └── ...
│   └── ...
├── myapp/                       # clone of repos.myapp
│   ├── .git/
│   └── src/...
└── mylib/                      # clone of repos.mylib（多 repo 任务时）
```

### 2.2 Feature Plan（任务拆分）

当任务过大、一次 develop 无法完成时，需要拆分为多个 Feature，每个 Feature 独立走 Phase 4→6。（v0.1 仅支持一层拆分，不支持递归嵌套。）

```
用户任务: "重构认证系统"
         │
         ▼  Phase 2 分析后 complexity=complex，自动生成 feature plan
   ┌─────────────────────────────────┐
   │  Feature Plan (feature_plan.json) │
   │  ┌──────────────────────────┐   │
   │  │ 0: 抽取 auth middleware  │ ──→ develop → verify → PR #15 ✅
   │  │ 1: 添加 JWT 验证        │ ──→ develop → verify → PR #16 ✅
   │  │ 2: 迁移 session 逻辑    │ ──→ develop → verify → (进行中)
   │  └──────────────────────────┘   │
   └─────────────────────────────────┘
```

**设计动机**：Feature Plan 是 Agent 的外部记忆 — 把任务拆分状态从 Agent 上下文卸载到文件里。Agent 不需要"记住"还剩几个 feature、做到哪了，只需要调 `feature-next` 获取下一步。这与 lock 文件帮 Agent 记住"当前 Phase"是同一设计思路。

### 2.3 职责分层

```
EverBot Daemon
├── Channel (Telegram/CLI/...)    ← 用户入口
├── Agent (Dolphin)              ← 意图理解、阶段编排、用户交互
│   └── coding-master skill
│       └── dispatch.py          ← CLI 工具集（无编排逻辑）
│           ├── config-*         ← 配置 CRUD
│           ├── workspace-check  ← 探测 workspace
│           ├── env-probe        ← 探测 env
│           ├── analyze          ← 调用 engine 分析
│           ├── develop          ← 调用 engine 开发
│           ├── test             ← 运行测试 + lint
│           ├── submit-pr        ← git 提交 + PR
│           └── release          ← 释放 lock
└── HeartbeatRunner              ← 定时任务
```

**关键边界**：

- **Agent (SKILL.md)** 是工作流引擎 — 决定下一步做什么、何时等用户确认、如何组装上下文
- **dispatch.py** 是无状态工具集 — 接收参数、执行单步操作、返回 JSON 结果
- Agent 通过 `_bash("python dispatch.py <subcommand> ...")` 逐步调用，每步拿到结果后决定下一步
- dispatch.py **不编排工作流**，不记录阶段状态，不做跨步骤决策

### 2.4 多 Repo 任务

一个任务可以涉及多个 repo。典型场景：前后端联调、库升级后调用方适配。

```
任务: "前后端联调 —— 后端加 API，前端调用"
  │
  ▼
repos: [backend, frontend]     ← workspace-check --repos backend,frontend
workspace: env0/
  ├── backend/    ← engine 需同时访问
  └── frontend/
```

**设计要点**：

- `workspace-check` 的 `--repos` 参数接受逗号分隔的多个 repo 名称
- 所有 repo 在同一 workspace 内 clone/update
- Snapshot 中 `repos` 为列表，第一个为 `primary_repo`（用于 env 匹配、默认分支命名等）
- Engine 的 cwd 为 workspace 根目录（而非单个 repo），可跨 repo 读写
- 每个 repo 独立拉分支、独立 PR
- `test` 按 repo 逐个运行，汇总结果
- `submit-pr` 为每个有变更的 repo 各创建一个 PR

---

## 三、配置系统

### 3.1 极简配置 + 扩展配置

配置存储在 `~/.alfred/config.yaml` 的 `coding_master` 段。支持两种写法，可以混用：

```yaml
coding_master:
  repos:
    myapp:
      url: git@github.com:user/myapp.git
      default_branch: main
      # 以下可选，覆盖自动探测
      test_command: pytest -x
      lint_command: ruff check .
      branch_prefix: fix/
    mylib:
      url: git@github.com:user/mylib.git

  workspaces:
    env0: ~/lab/coding_master/env0    # 工作槽，repos 按需 clone 进来
    env1: ~/lab/coding_master/env1
    env2: ~/lab/coding_master/env2

  envs:
    myapp-prod:
      connect: deploy@prod-server:/opt/myapp
      tier: prod                # prod | staging | local
      log: /opt/myapp/logs/daemon.log
      service: myapp-daemon

  default_engine: claude    # claude | codex
  max_turns: 30
```

关键变化：
- `repos` 新增：存储 repo 元信息（URL、默认分支、test/lint 命令等）
- `workspaces` 简化：只是路径（工作槽），不再绑定特定 repo。不需要 `test_command` 等（这些属于 repo）
- `test_command` / `lint_command` / `branch_prefix` 从 workspace 迁移到 repo

解析规则：值是 string → 极简模式；值是 dict → 扩展模式。

极简 repo 配置（只有 URL）：
```yaml
repos:
  myapp: git@github.com:user/myapp.git        # string → 只有 url
  mylib:                                        # dict → 扩展配置
    url: git@github.com:user/mylib.git
    default_branch: develop
```

极简 workspace 配置（只有路径）：
```yaml
workspaces:
  env0: ~/lab/coding_master/env0                 # string → 只有 path
```

极简 env 格式：

- 本地：`/absolute/path` → type=local
- SSH：`user@host:/path` → type=ssh（scp 风格）

### 3.2 关联关系

**Repo ↔ Env**：沿用命名约定（repo `myapp` 匹配 env `myapp-*`）

**匹配规则**（按优先级）：

1. 扩展配置中的 `default_env` — 最高优先
2. 命名约定：repo `myapp` 匹配所有 env `myapp-*`
3. 用户在对话中指定

**Repo ↔ Workspace**：不绑定，任务开始时自动分配空闲 workspace。用户也可以显式指定 workspace（如有特殊需求）。

**多 Env 消歧**：当 repo `myapp` 匹配到多个 env（如 `myapp-local`、`myapp-prod`、`myapp-staging`）时：

- 根据 env 名称后缀推断 tier：`-prod` → prod, `-staging` → staging, `-local`/`-dev` → local
- 扩展配置中可用 `tier` 字段显式指定
- Agent 根据用户意图选择：提到"线上/生产"→ prod，提到"测试环境"→ staging，未指定 → 列出候选让用户选

### 3.3 通过对话管理配置

dispatch.py 提供 `config-*` 子命令，Agent 通过 `_bash()` 调用：

```bash
# 增
python dispatch.py config-add repo myapp git@github.com:user/myapp.git
python dispatch.py config-add workspace env0 ~/lab/coding_master/env0
python dispatch.py config-add env myapp-prod deploy@prod-server:/opt/myapp

# 改（自动从极简升级为扩展）
python dispatch.py config-set repo myapp test_command "pytest -x"
python dispatch.py config-set env myapp-prod log "/opt/myapp/logs/daemon.log"

# 删
python dispatch.py config-remove repo mylib
python dispatch.py config-remove env myapp-staging

# 查
python dispatch.py config-list
```

对话体验：

```
用户: 添加 repo myapp git@github.com:user/myapp.git

Agent: ✅ 已添加 repo:
  myapp → git@github.com:user/myapp.git

用户: 添加 workspace env0 ~/lab/coding_master/env0

Agent: ✅ 已添加 workspace:
  env0 → ~/lab/coding_master/env0

用户: 添加 env myapp-prod deploy@prod-server:/opt/myapp

Agent: ✅ 已添加 env:
  myapp-prod → deploy@prod-server:/opt/myapp
  正在检查 SSH 连通性... ✅ 可达

用户: 设置 myapp test_command "pytest -x"

Agent: ✅ 已更新 repo myapp:
  myapp:
    url: git@github.com:user/myapp.git
    test_command: pytest -x        ← 新增
  (已从极简升级为扩展配置)

用户: 列出所有环境

Agent:
  Repos:
    myapp      git@github.com:user/myapp.git
    mylib     git@github.com:user/mylib.git

  Workspaces:
    env0       ~/lab/coding_master/env0     [idle]
    env1       ~/lab/coding_master/env1     [idle]
    env2       ~/lab/coding_master/env2     [idle]

  Envs:
    myapp-prod    deploy@prod-server:/opt/myapp       [ssh ✅]
```

### 3.4 无需热加载

dispatch.py 每次通过 `_bash()` 调用都是独立进程，天然读取最新的 config.yaml。不存在缓存、不需要 reload 信号、不需要重启 daemon。

写入 config.yaml 时使用 atomic write（写临时文件 → rename），与现有 session persistence 策略一致。

---

## 四、Workspace 管理

### 4.1 Lock 文件

每个 workspace 使用 `.coding-master.lock` 标记占用状态。

说明：`dispatch.py` 是短生命周期 CLI，**lock 的持有者不是某个子进程 PID**，而是一次对话会话对应的任务会话（task session）。

**位置**：`{workspace_path}/.coding-master.lock`

**内容**：

```json
{
  "task": "fix: heartbeat 定时任务未触发",
  "repos": ["myapp"],
  "branches": {"myapp": "fix/heartbeat-trigger"},
  "engine": "claude",
  "env": "myapp-prod",
  "owner": {
    "session_id": "tg:chat123:task456",
    "daemon_instance": "everbot@host-a",
    "user": "xupeng"
  },
  "phase": "developing",
  "phase_history": [
    {"phase": "workspace-check", "completed_at": "2026-02-24T10:30:00Z"},
    {"phase": "env-probe", "completed_at": "2026-02-24T10:31:00Z"},
    {"phase": "analyzing", "completed_at": "2026-02-24T10:33:00Z"},
    {"phase": "confirmed", "completed_at": "2026-02-24T10:35:00Z"}
  ],
  "artifacts": {
    "workspace_snapshot": ".coding-master/workspace_snapshot.json",
    "env_snapshot": ".coding-master/env_snapshot.json",
    "analysis_report": ".coding-master/phase2_analysis.md",
    "feature_plan": ".coding-master/feature_plan.json",
    "test_report": ".coding-master/test_report.json",
    "env_verify_report": ".coding-master/env_verify_report.json"
  },
  "pushed_to_remote": false,
  "started_at": "2026-02-24T10:30:00Z",
  "updated_at": "2026-02-24T10:35:00Z",
  "lease_expires_at": "2026-02-24T12:30:00Z"
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
  └── Daemon/任务会话异常结束 → 下次 acquire 检测 lease 过期 → 僵尸锁自动清理
```

**原子获取**：使用 `os.open(path, O_CREAT | O_EXCL | O_WRONLY)` 创建 lock 文件，保证并发安全 — 两个会话同时请求同一 workspace 时，只有一个能成功创建文件，另一个收到 `FileExistsError`。

**僵尸锁检测**：基于 `lease_expires_at`（租约过期时间）判断，而不是子进程 PID。任务推进、阶段完成时刷新 `updated_at` 和 `lease_expires_at`。若租约过期则视为僵尸锁并自动清理，同时记录日志。

### 4.2 Workspace 分配流程

```
用户: "修 myapp 的 heartbeat bug"
  │
  ▼
1. 解析 repo 名称列表 → 逐个查 repos 配置 → 得到 URL
  │
  ▼
2. 遍历 workspaces，找第一个未被 lock 的 → 分配
  │
  ▼
3. 锁定 workspace（O_CREAT|O_EXCL）
  │
  ▼
4. 对每个 repo：检查 workspace/{repo_name} 是否存在
   ├── 存在 → git fetch + checkout default_branch + git pull
   └── 不存在 → git clone URL 到 workspace/{repo_name}
  │
  ▼
5. 对每个 repo：探测 workspace/{repo_name} 的 git/runtime/project
  │
  ▼
6. 返回 snapshot（含分配到的 workspace 名称 + 所有 repo 信息）
```

### 4.3 Workspace 探测

锁定 workspace 并确保 repo clone 后，脚本自动探测开发环境（不消耗 LLM token）。

探测路径为 `ws_path/{repo_name}`（而非 `ws_path`），每个 repo 独立探测：

**单 repo 快照**：

```json
{
  "workspace": { "name": "env0", "path": "~/lab/coding_master/env0" },
  "repos": [
    {
      "name": "myapp",
      "url": "git@github.com:user/myapp.git",
      "local_path": "~/lab/coding_master/env0/myapp",
      "git": {
        "branch": "main",
        "dirty": false,
        "remote_url": "git@github.com:user/myapp.git",
        "last_commit": "254c41b fix(paper-discovery): ..."
      },
      "runtime": { "type": "python", "version": "3.12.4", "package_manager": "uv" },
      "project": { "test_command": "pytest", "lint_command": "ruff check ." }
    }
  ],
  "primary_repo": "myapp"
}
```

**多 repo 快照**：

```json
{
  "workspace": { "name": "env0", "path": "~/lab/coding_master/env0" },
  "repos": [
    {
      "name": "backend",
      "url": "git@github.com:user/backend.git",
      "local_path": "~/lab/coding_master/env0/backend",
      "git": { "branch": "main", "dirty": false, "..." : "..." },
      "runtime": { "type": "python", "..." : "..." },
      "project": { "test_command": "pytest", "..." : "..." }
    },
    {
      "name": "frontend",
      "url": "git@github.com:user/frontend.git",
      "local_path": "~/lab/coding_master/env0/frontend",
      "git": { "branch": "main", "dirty": false, "..." : "..." },
      "runtime": { "type": "node", "..." : "..." },
      "project": { "test_command": "npm test", "..." : "..." }
    }
  ],
  "primary_repo": "backend"
}
```

runtime 和 project 信息通过文件特征自动发现（pyproject.toml / package.json / Cargo.toml 等），repo 配置中的 `test_command` / `lint_command` 可覆盖自动发现结果。

### 4.4 Lease 续期策略

Lease 默认时长 **2 小时**，在等待用户输入的阶段（Phase 3、Feature 间隙）足够宽裕。

续期时机：
- **自动续期**：每个 dispatch.py 子命令执行时，`with_lock_update` 自动续期
- **显式续期**：Agent 在用户交互间隙可调用 `dispatch.py renew-lease --workspace env0` 刷新 lease，避免长时间等待用户输入导致 lease 过期

### 4.5 并行任务（v0.2）

主 workspace 被占用时，使用 git worktree 创建隔离副本。v0.1 锁定时直接提示用户等待。

---

## 五、Env 管理

### 5.1 设计思路

Env 是问题排查的入口 — 只读采集信息，不修改运行环境。

用户报告 "线上有 bug" 时，Agent 先去 Env 采集现象（日志、进程状态、错误信息），再带着线索回到 Workspace 分析代码。

### 5.2 访问方式

根据配置的 type 自动选择：

- **本地 Env**（path 是绝对路径）：直接 `subprocess.run(cmd, cwd=path)`
- **SSH Env**（`user@host:path` 格式）：通过 `subprocess.run(["ssh", user_host, cmd_str])` 执行

SSH 依赖 `~/.ssh/config` 和密钥认证，不支持密码交互。

所有 Env 命令均经过 `shlex.quote()` 转义 + 白名单校验（见 [10.2 节](#102-env-访问安全)），防止命令注入。

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
        modules = [{"name": basename(env_path), "path": env_path}]

    # 2. 每个模块探测进程、日志、错误
    for module in modules:
        module["process"] = ps_grep(module["name"])
        module["path"] = module.get("path", env_path)
        module["log"] = find_logs(module["path"])
        # 只取最近 1000 行再 grep，避免大日志文件导致超时
        module["recent_errors"] = grep_errors(module["log"], tail_lines=1000)
        module["log_tail"] = tail_logs(module["log"], lines=50)

    # 3. 通用信息
    return {
        "modules": modules,
        "uptime": run("uptime"),
        "disk_usage": run(f"df -h {shlex.quote(env_path)}"),
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
python dispatch.py env-probe --env myapp-prod \
  --commands "journalctl -u myapp --since '2 hours ago'" \
             "cat /opt/myapp/config.yaml"
```

### 5.5 Env Snapshot 结构

```json
{
  "env": { "name": "myapp-prod", "type": "ssh", "connect": "deploy@prod-server:/opt/myapp" },
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

### 6.2 Phase 间数据结构

不同阶段的 Engine 调用产出不同结构的结果。

阶段产物由 `dispatch.py` 落盘到 workspace 下 `.coding-master/`（同时在 lock 的 `artifacts` 字段记录路径），以支持 Daemon 重启后的恢复。

**Phase 2 (分析) → AnalysisReport**

Engine 的 summary 包含自由文本分析和结构化字段。dispatch.py 从 summary 中解析 `complexity` 分类（trivial / standard / complex）。当 complexity 为 complex 时，summary 中还包含结构化的 Feature Plan JSON，dispatch.py 解析后自动调用 `FeatureManager.create_plan()` 写入 `feature_plan.json` 和对应的 criteria 文件。

Agent (LLM) 负责理解 summary 内容并：
- 格式化后发给用户
- 透传 summary 全文作为 Phase 4 的上下文
- 根据 complexity 决定 Phase 3 的交互方式（见 [7.5 节](#75-phase-3-方案确认)）

```json
{
  "success": true,
  "summary": "## 问题定位\nheartbeat.py:142 ...\n## 根因分析\n...\n## 修复方案\n...\n## Complexity: trivial",
  "complexity": "trivial",
  "files_changed": [],
  "error": null
}
```

当 complexity=complex 时，返回结果额外包含 feature_plan：

```json
{
  "success": true,
  "summary": "## 问题定位\n...\n## 修复方案\n...\n## Complexity: complex\n## Feature Plan\n...",
  "complexity": "complex",
  "feature_plan": [
    {
      "title": "抽取 auth middleware",
      "task": "将 app.py 中的认证逻辑抽取为独立 middleware",
      "depends_on": [],
      "acceptance_criteria": [
        {"type": "test", "target": "tests/unit/test_auth.py", "auto": true},
        {"type": "assert", "description": "middleware registered in app factory", "auto": true}
      ]
    }
  ],
  "files_changed": [],
  "error": null
}
```

**Phase 4 (开发) → DevelopReport**

```json
{
  "success": true,
  "summary": "统一 timezone-aware datetime，修改 HeartbeatRunner._should_run_task()",
  "files_changed": ["src/everbot/core/runtime/heartbeat.py"],
  "error": null
}
```

Phase 4 的 prompt 包含 Phase 2 的完整 summary，由 Agent 在 SKILL.md 中组装：

```
## 诊断报告
{phase2_summary}

## 用户确认的方案
{user_choice}

## 任务
根据以上诊断报告实现修复。
```

### 6.3 Claude Code Engine

```bash
claude -p "<prompt>" \
  --allowedTools "Bash,Read,Edit,Write,Glob,Grep" \
  --output-format json \
  --max-turns 30
```

使用 `asyncio.create_subprocess_exec` 异步执行，超时默认 10 分钟。

Workspace 探测结果和 Env Snapshot 注入到 prompt 中，避免 engine 浪费 turns 探测环境。

### 6.4 Codex Engine（v0.2）

```bash
codex --approval-mode full-auto --quiet "<prompt>"
```

### 6.5 Engine 选择

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
Phase 0          Phase 1        Phase 2       Phase 3       Phase 4       Phase 5         Phase 6       Phase 7
Workspace 确认 → Env 探测   →  问题分析  →   方案确认  →   编码开发  →   测试验证    →   提交 PR   →  Env 验证
(脚本)           (脚本/SSH)     (engine)      (人工)        (engine)      (脚本+engine)   (脚本)       (脚本/SSH)
  │                │              │             │             │             │               │             │
  ▼                ▼              ▼             ▼             ▼             ▼               ▼             ▼
Workspace 快照  Env Snapshot   诊断报告     用户确认      代码变更     测试/lint 报告   PR URL       验证报告
                               +complexity
```

**自适应复杂度**：Phase 2 分析时 Engine 输出 `complexity` 分类（trivial / standard / complex），Phase 3 根据分类决定交互方式：

- **trivial**：跳过 Phase 3 确认，直接进入 Phase 4 开发
- **standard**：标准流程，等待用户确认
- **complex**：Phase 2 同时自动生成 Feature Plan，Phase 3 向用户展示拆分方案并建议分步执行

默认在**关键阶段**等待用户确认，避免无意义往返：

- 必须确认：Phase 0（环境确认）、Phase 3（方案确认，Phase 本身即用户决策点；trivial 除外）、Phase 5（测试结果）、Phase 6（提交 PR 前）
- 自动串行：Phase 4 完成后自动进入 Phase 5
- 可选阶段：Phase 7（Env 验证）仅在有关联 Env 且用户需要部署验证时触发
- 可配置：高风险仓库可切换为"每阶段都确认"

**任务拆分分支**：Phase 2 分析后如果 complexity=complex，自动生成 Feature Plan 并进入 Feature 循环（见[第八节](#八feature-管理)），每个 Feature 独立走 develop → verify → submit-pr（Feature 循环中不包含 Phase 7，部署验证在全部 Feature 完成后统一进行）。

### 7.2 Phase 0: Workspace 确认

**执行者**：脚本（不消耗 token）

```bash
# 单 repo
dispatch.py workspace-check --repos myapp --task "fix: heartbeat bug" --engine claude
# 多 repo
dispatch.py workspace-check --repos backend,frontend --task "前后端联调" --engine claude
# 可选显式指定 workspace
dispatch.py workspace-check --repos myapp --workspace env0 --task "fix: heartbeat bug" --engine claude
```

脚本内部**原子完成**以下步骤：
1. 解析 repo 名称列表 → 逐个查 repos 配置 → 得到 URL
2. 遍历 workspaces，找第一个未被 lock 的 → 分配（或使用显式指定的 workspace）
3. acquire lock（写入 lock 文件 + 设置 lease）
4. 对每个 repo：检查 `workspace/{repo_name}` 是否存在 → clone 或 pull
5. 对每个 repo：探测 `workspace/{repo_name}` 的 git/runtime/project → 生成快照
6. 快照落盘到 `.coding-master/workspace_snapshot.json`

返回值：
```json
{
  "success": true,
  "snapshot": {
    "workspace": {"name": "env0", "path": "~/lab/coding_master/env0"},
    "repos": [
      {
        "name": "myapp",
        "url": "git@github.com:user/myapp.git",
        "local_path": "~/lab/coding_master/env0/myapp",
        "git": {"branch": "main", "dirty": false, "...": "..."},
        "runtime": {"...": "..."},
        "project": {"...": "..."}
      }
    ],
    "primary_repo": "myapp"
  }
}
```

或 `{"success": false, "error": "..."}`. Agent 不需要理解 lock 细节。

**后续 Phase 的 --workspace 参数**：Phase 0 返回分配到的 workspace 后，后续命令继续用 `--workspace env0`（因为 workspace 已确定）。内部执行路径：单 repo 时 cwd 为 `ws_path/{repo_name}`，多 repo 时 cwd 为 `ws_path`（workspace 根目录）。

**阻断条件**（脚本内部判定，直接返回 error）：repo 未配置、所有 workspace 被占用、clone 失败、任意 repo 有未提交变更。

### 7.3 Phase 1: Env 探测

**执行者**：脚本/SSH（不消耗 token）

```bash
dispatch.py env-probe --workspace env0 --env myapp-prod
```

脚本内部：匹配 env → 自动探测模块/进程/日志/错误 → 快照落盘 `.coding-master/env_snapshot.json` → 更新 lock phase。

**可跳过**：功能开发等不需要排查现象的场景，Agent 智能判断，不调用此命令。

### 7.4 Phase 2: 问题分析

**执行者**：Coding Engine

```bash
dispatch.py analyze --workspace env0 --task "heartbeat 定时任务没触发" --engine claude
```

脚本内部：读取 `.coding-master/` 下的 workspace_snapshot + env_snapshot → 注入 prompt → 调用 Engine → 从 Engine 结果中解析 complexity 和可选的 feature plan → 结果落盘 `.coding-master/phase2_analysis.md` → 当 complexity=complex 时自动调用 `FeatureManager.create_plan()` 写入 `feature_plan.json` 及 criteria 文件 → 更新 lock phase。

Engine prompt 模板：

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
3. 修复方案（可多个，标注推荐）
4. 影响范围
5. 风险评估（低/中/高）
6. 复杂度分类（Complexity）: trivial / standard / complex
   - trivial: 重命名、typo 修复、添加 import、单行修复
   - standard: 常规 bug 修复、普通功能开发
   - complex: 跨模块重构、新子系统
7. 是否需要更多 Env 信息

如果 complexity 为 complex，还需输出：
8. Feature Plan (JSON):
[{"title": "...", "task": "...", "depends_on": [], "acceptance_criteria": [{"type": "test", "target": "...", "auto": true}, {"type": "assert", "description": "...", "auto": true}, {"type": "manual", "description": "...", "auto": false}]}]
```

**complexity=complex 时的自动处理**：`cmd_analyze` 解析 Engine 返回的 Feature Plan JSON，自动调用 `FeatureManager.create_plan()` 完成以下操作：
- 写入 `.coding-master/feature_plan.json`（含所有 feature 的核心字段）
- 为每个 feature 创建 `.coding-master/features/{index}/criteria.json`（acceptance criteria）

无需 Agent 手动构造 JSON 或调用 `feature-plan`。

**analyze 返回值**（增加 complexity 和 feature_plan_created 字段）：

```json
{
  "success": true,
  "summary": "## 问题定位\n...\n## 修复方案\n...",
  "complexity": "complex",
  "feature_plan_created": true,
  "feature_count": 3
}
```

如果 Engine 请求更多 Env 信息，Agent 调用 `env-probe --commands ...` 后再次调用 `analyze`（迭代分析，最多 2 轮）。

### 7.5 Phase 3: 方案确认

**执行者**：用户

根据 Phase 2 返回的 complexity 分三种流程：

**trivial（自动跳过）**：
- Agent 向用户展示分析摘要（一行总结）
- 不等待确认，直接进入 Phase 4 开发
- 示例：`分析完成：typo 修复 (config.py:12)，复杂度: trivial，自动开始修复...`

**standard（标准确认）**：
- 现有流程不变，等待用户确认
- "继续" → Phase 4
- "用方案 2" → 指定方案后 Phase 4
- "再看看线上日志" → Agent 调用 `env-probe --commands ...` → 补充后重跑 Phase 2
- "取消" → Agent 调用 `dispatch.py release --workspace env0`

**complex（展示 Feature Plan）**：
- Agent 向用户展示自动生成的 Feature Plan 拆分方案及各 feature 的 acceptance criteria
- 建议用户按 Feature Plan 分步执行
- "按计划推进" → 进入 Feature 循环（见[第八节](#八feature-管理)）
- "调整计划" → Agent 调用 `feature-update` 等命令修改计划后再确认
- "不拆分，一次做" → 按 standard 流程走 Phase 4
- "取消" → Agent 调用 `dispatch.py release --workspace env0`

### 7.6 Phase 4: 编码开发

**执行者**：Coding Engine

```bash
dispatch.py develop --workspace env0 --task "..." --plan "..." --branch fix/heartbeat-tz --engine claude
```

脚本内部：对每个 repo `git checkout -b` → 读取 `.coding-master/phase2_analysis.md` 注入 prompt → 调用 Engine 编码 → 更新 lock phase。多 repo 时 Engine cwd 为 workspace 根目录。

Engine **只负责写代码**，不运行测试。

### 7.7 Phase 5: 测试验证

**执行者**：脚本（优先） + Engine（修复时）

```bash
dispatch.py test --workspace env0
```

脚本内部：对每个 repo 运行 lint + test → 结果落盘 `.coding-master/test_report.json` → 更新 lock phase。

执行内容（每个 repo 独立执行）：

1. **Lint**：运行 `lint_command`（自动发现或配置指定）
2. **Test**：运行 `test_command`（自动发现或配置指定）
3. **汇总**：输出结构化测试报告（多 repo 时按 repo 分组）

```json
{
  "lint": { "passed": true, "output": "All checks passed" },
  "test": { "passed": true, "total": 42, "passed_count": 42, "failed_count": 0, "output": "..." },
  "overall": "passed"
}
```

**测试结果处理**：

| 情况 | Agent 行为 |
|------|------|
| 全部通过 | 报告结果，等用户确认后进入 Phase 6 |
| Lint 失败 | 自动调用 `develop --task "fix lint: ..."` 修复，再调用 `test` |
| 测试失败 | 报告失败详情给用户，由用户决定下一步 |
| 修复后仍失败 | 报告给用户，提供选项 |

**测试失败的用户选项**：

```
Agent: ❌ 测试未通过 (3 failed / 42 total):
       test_heartbeat_timezone: AssertionError ...
       test_heartbeat_skip: ...
       test_heartbeat_retry: ...

       选择：
       1. 让我修一下（Agent 调用 develop + test 再跑一轮）
       2. 我手动看看（暂停，保留分支，Agent 调用 release）
       3. 放弃修改（Agent 调用 release --cleanup 回滚分支）
```

自动修复最多重试 **2 轮**（develop → test → fix → test → fix → test）。超过后必须交给用户。

### 7.8 Phase 6: 提交 PR

**执行者**：脚本

```bash
dispatch.py submit-pr --workspace env0 --title "fix: heartbeat timezone" --body "..."
```

脚本内部：对每个有变更的 repo 执行 `git add` → `git commit` → `git push` → `gh pr create` → 更新 lock phase。

多 repo 时每个 repo 独立 PR，互相在 body 中引用关联 PR。PR URL 发回用户。

**PR 提交后的分支**：

- 有关联 Env → Agent 询问"需要部署验证吗？"，用户选择后进入 Phase 7 或直接 release
- 无关联 Env → 直接 release

```bash
# 不需要 env 验证时，直接释放
dispatch.py release --workspace env0
```

脚本内部：删除 lock 文件 + 清理 `.coding-master/` 目录（所有阶段产物一并删除，不保留历史）。

**清理范围**：`release` 和 `release --cleanup` 都会删除整个 `.coding-master/` 目录。区别在于 `--cleanup` 额外对每个 repo 执行 git 回滚（删除本地分支、checkout 回原分支；若分支已 push 到 remote 则同时 `git push --delete origin <branch>`）。Lock 文件中记录 `pushed_to_remote: bool` 字段以判断是否需要清理远程分支。

### 7.9 Phase 7: Env 验证（可选）

**执行者**：脚本/SSH（不消耗 token）+ 用户协作

**触发条件**：任务有关联 Env 且用户选择进行部署验证。典型场景：修复线上 bug 后需要确认 staging/prod 上确实修好了。

**设计原则**：Env **保持严格只读**。部署由用户通过 CI/CD pipeline 或手动完成，Agent 只负责在部署完成后观测验证。

**流程**：

```
Phase 6 (PR 已创建)
  │
  ├── 无关联 Env / 用户跳过 → release
  │
  └── 用户选择 env 验证
        │
        ▼
      等待用户通知部署完成（Agent 保持 lease，定期 renew）
        │
        ▼
      env-verify：复用 env-probe 采集当前 Env 状态
        │
        ├── 验证通过（相关错误消失 / 新行为符合预期）→ 报告 → release
        │
        └── 验证失败（问题仍存在 / 出现新问题）
              │
              ├── 用户选择"再修一轮" → 回到 Phase 4（develop → test → submit-pr → env-verify）
              ├── 用户选择"我手动处理" → release
              └── 用户选择"回滚" → release --cleanup
```

```bash
dispatch.py env-verify --workspace env0 --env myapp-staging
```

脚本内部：
1. 读取 lock 中的任务描述和 Phase 1 的原始 env_snapshot（作为基线对比）
2. 对目标 Env 执行 `auto_probe()`（与 Phase 1 相同的探测逻辑）
3. 生成验证报告：对比修复前后的错误日志变化
4. 落盘到 `.coding-master/env_verify_report.json`
5. 更新 lock phase

**验证报告结构**：

```json
{
  "env": "myapp-staging",
  "verified_at": "2026-02-24T14:30:00Z",
  "baseline_errors": ["ERROR heartbeat: Task 'daily-report' skipped", "..."],
  "current_errors": [],
  "new_errors": [],
  "resolved": true,
  "modules": [
    {
      "name": "daemon",
      "process": { "running": true, "pid": 7890, "uptime": "5 min" },
      "recent_errors": [],
      "log_tail": "... (最近 50 行) ..."
    }
  ],
  "summary": "修复前 2 个 heartbeat 相关错误，修复后 30 分钟内无相关错误出现"
}
```

Agent 拿到验证报告后，将对比结果格式化发给用户，由用户决定下一步。

**等待部署期间的 Lease 管理**：用户部署可能需要较长时间（CI/CD pipeline、手动操作等）。Agent 在等待期间应调用 `renew-lease` 保持 workspace 锁定，避免 lease 过期被误清理。

### 7.10 Lock 生命周期与各 Phase 的关系

```
dispatch.py 命令                     lock 操作
─────────────────                   ──────────
workspace-check --repos ...         acquire lock → phase="workspace-check"
env-probe                           update phase="env-probe", 续 lease
analyze                             update phase="analyzing", 续 lease
(用户确认)                           Agent 调用任意命令时续 lease
develop                             update phase="developing", 续 lease
test                                update phase="testing", 续 lease
submit-pr                           update phase="submitted", 续 lease, pushed_to_remote=true
(等待部署)                           Agent 调用 renew-lease 保持锁定
env-verify                          update phase="env-verified", 续 lease
renew-lease                         仅续 lease（等待用户输入/部署时调用）
release                             delete lock + 删除 .coding-master/
release --cleanup                   git cleanup（含远程分支）+ delete lock + 删除 .coding-master/
```

每个 dispatch.py 子命令在执行前检查 lock 存在且未过期，执行后更新 `phase` + `updated_at` + `lease_expires_at`。Agent 无需关心 lock 管理。

### 7.11 失败路径总结

```
任意 Phase 异常
  │
  ├── Phase 0 失败（workspace 不可用）
  │   → workspace-check 返回 error + 不获取 lock
  │   → Agent 报错 + 建议（如 "请先提交未保存变更"）
  │
  ├── Phase 1 失败（env 不可达）
  │   → env-probe 返回 error（lock 保留）
  │   → Agent 询问用户是否跳过 env 探测继续
  │
  ├── Phase 2 失败（Engine 分析超时/出错）
  │   → analyze 返回 error
  │   → Agent 调用 release，建议用户提供更多信息后重试
  │
  ├── Phase 4 失败（Engine 编码超时/出错）
  │   → develop 返回 error
  │   → Agent 调用 release --cleanup，提示用户可手动接管
  │
  ├── Phase 5 失败（测试不通过）
  │   → test 返回测试报告（overall=failed）
  │   → Agent 自动修复（最多 2 轮），仍失败则交给用户
  │
  ├── Phase 6 失败（push/PR 创建失败）
  │   → submit-pr 返回 error（lock 保留，本地提交保留）
  │   → Agent 报错，用户可手动 push 后调 release
  │
  └── Phase 7 失败（env 验证不通过）
      → env-verify 返回验证报告（resolved=false）
      → Agent 报告对比结果，提供选项：再修一轮 / 手动处理 / 回滚
      → "再修一轮" → 回到 Phase 4（develop → test → submit-pr → env-verify）
```

---

## 八、Feature 管理

### 8.1 触发时机

Phase 2 分析时 Engine 输出 `complexity` 分类。当 complexity=complex 时，Engine 同时输出结构化的 Feature Plan JSON（含 title、task、depends_on、acceptance_criteria），`cmd_analyze` 自动解析并调用 `FeatureManager.create_plan()` 写入 `feature_plan.json` 和 criteria 文件。

| complexity | Agent 行为 |
|-----------|-----------|
| trivial | 跳过 Phase 3 确认，直接进入 Phase 4 |
| standard | 正常进入 Phase 3→4→5→6 |
| complex | analyze 自动生成 Feature Plan，Phase 3 向用户展示拆分方案，确认后进入 Feature 循环 |

**与 analyze 的集成**：无需 Agent 手动调用 `feature-plan` 创建计划 — analyze 检测到 complexity=complex 时一步完成分析 + 拆分。Agent 只需根据返回的 `feature_plan_created: true` 字段进入 Feature 循环流程。

### 8.2 存储方案

采用**单索引 + 产物子目录**方案（Scheme C）：

```
.coding-master/
  feature_plan.json          # 轻量索引，所有 feature 核心字段
  features/
    0/
      analysis.md            # feature 级别的分析记录
      criteria.json          # acceptance criteria 定义
      verification.json      # 验证执行记录
      test_report.json       # feature 级别的测试报告
      notes.md               # 备注（可选）
    1/
      analysis.md
      criteria.json
      verification.json
      ...
```

**设计要点**：

- `feature_plan.json` 是轻量索引文件，包含所有 feature 的核心字段（status、depends_on、timestamps 等），一次读取即可获取全局进度
- `features/{index}/` 下的产物文件**按需创建**，不预创建空文件
- criteria.json 在 `create_plan()` 时由 analyze 的 Feature Plan JSON 自动写入
- verification.json 在 `feature-verify` 执行时创建/更新

### 8.3 数据结构

**feature_plan.json**（索引文件）：

```json
{
  "origin_task": "重构认证系统",
  "created_at": "2026-02-26T10:00:00Z",
  "features": [
    {
      "index": 0,
      "title": "抽取 auth middleware",
      "task": "将 app.py 中的认证逻辑抽取为独立 middleware",
      "status": "done",
      "depends_on": [],
      "criteria_count": 3,
      "verified_count": 3,
      "branch": "feat/auth-middleware",
      "pr": "#15",
      "created_at": "2026-02-26T10:00:00Z",
      "started_at": "2026-02-26T10:05:00Z",
      "completed_at": "2026-02-26T11:30:00Z",
      "attempts": 1
    },
    {
      "index": 1,
      "title": "添加 JWT 验证",
      "task": "在 auth middleware 中集成 PyJWT，验证 token 签名和有效期",
      "status": "in_progress",
      "depends_on": [0],
      "criteria_count": 2,
      "verified_count": 0,
      "branch": "feat/jwt-auth",
      "pr": null,
      "created_at": "2026-02-26T10:00:00Z",
      "started_at": "2026-02-26T11:35:00Z",
      "completed_at": null,
      "attempts": 1
    },
    {
      "index": 2,
      "title": "迁移 session 逻辑",
      "task": "将现有 session-based auth 迁移到 JWT，移除旧 session 代码",
      "status": "pending",
      "depends_on": [1],
      "criteria_count": 4,
      "verified_count": 0,
      "branch": null,
      "pr": null,
      "created_at": "2026-02-26T10:00:00Z",
      "started_at": null,
      "completed_at": null,
      "attempts": 0
    }
  ]
}
```

字段说明：

- `status`: `pending` → `in_progress` → `done` / `failed` / `skipped`
- `depends_on`: 索引数组，被依赖的 feature 必须 `done` 才能开始
- `criteria_count` / `verified_count`: acceptance criteria 总数和已通过数，从 criteria.json / verification.json 中统计
- `branch` / `pr`: feature 完成后填入，供后续 feature 参考
- `created_at` / `started_at` / `completed_at`: 时间戳，`feature-next` 时写入 `started_at`，`feature-done` 时写入 `completed_at`
- `attempts`: 开发尝试次数，每次调用 develop 时递增。超过阈值（默认 3）时建议用户介入

### 8.4 Acceptance Criteria（验收标准）

每个 feature 的 acceptance criteria 存储在 `features/{index}/criteria.json`，由 `create_plan()` 从 Engine 输出的 Feature Plan JSON 自动写入。

**criteria.json 结构**：

```json
[
  {
    "id": "c0",
    "type": "test",
    "target": "tests/unit/test_auth.py",
    "auto": true
  },
  {
    "id": "c1",
    "type": "assert",
    "description": "middleware registered in app factory",
    "auto": true
  },
  {
    "id": "c2",
    "type": "manual",
    "description": "login flow works on staging",
    "auto": false
  }
]
```

**criteria 类型**：

| type | 说明 | 验证方式 | auto |
|------|------|---------|------|
| `test` | 运行指定测试文件/路径 | `pytest {target}` 或 `npm test {target}` | true |
| `assert` | 代码中存在某个行为/结构 | Engine 检查代码是否满足描述 | true |
| `manual` | 需人工确认的验收项 | 提醒用户确认，不阻塞自动流程 | false |

**追加 criteria**：用户可通过 `feature-criteria` 命令在 feature 执行过程中追加新的验收标准：

```bash
dispatch.py feature-criteria --workspace env0 --index 0 --action view
dispatch.py feature-criteria --workspace env0 --index 0 --action append \
  --type test --target "tests/integration/test_auth_flow.py"
```

### 8.5 验证机制

验证分为两个层级：

**Workspace 级验证（Phase 5，全量测试）**：
- 现有的 Phase 5 `test` 命令，运行全量 lint + test
- 确保代码变更不引入回归
- 作用于整个 workspace（所有 repo）

**Feature 级验证（嵌入 develop 循环）**：
- 针对单个 feature 的 acceptance_criteria 定向验证
- 在每个 feature 的 develop 完成后执行，不是独立的 Phase
- 验证结果记录到 `features/{index}/verification.json`

**verification.json 结构**：

```json
[
  {
    "id": "c0",
    "passed": true,
    "output": "3 passed in 0.12s",
    "run_at": "2026-02-26T11:00:00Z"
  },
  {
    "id": "c1",
    "passed": true,
    "output": "found register_auth_middleware() in create_app()",
    "run_at": "2026-02-26T11:00:00Z"
  },
  {
    "id": "c2",
    "passed": null,
    "note": "awaiting user confirmation"
  }
]
```

**验证流程**（`feature-verify` 内部）：

1. 读取 `features/{index}/criteria.json`
2. 对每个 `auto: true` 的 criteria 执行验证：
   - `test`: 运行 `pytest {target}` 或对应的测试命令，检查退出码
   - `assert`: 构造 Engine prompt，让 Engine 检查代码是否满足描述
3. 对 `auto: false` 的 criteria（manual 类型），标记为 `passed: null`，提醒用户确认
4. 结果写入 `features/{index}/verification.json`
5. 更新 `feature_plan.json` 中的 `verified_count`

### 8.6 生命周期状态机

```
pending → in_progress → [develop → verify → retry if failed] → done / failed / skipped
```

状态转换规则：

| 当前状态 | 触发 | 目标状态 |
|---------|------|---------|
| pending | `feature-next` 选中 | in_progress |
| in_progress | `feature-done` 且所有 auto criteria 通过 | done |
| in_progress | `feature-done --force` 跳过未通过的 criteria | done |
| in_progress | attempts 超限且用户放弃 | failed |
| pending / in_progress | 用户跳过 | skipped |

**`feature-done` 检查逻辑**：

- 所有 `auto: true` 的 criteria 必须 `passed: true`，否则报错
- `auto: false` 的 criteria（manual 类型）不阻塞，但在报告中提醒用户待确认
- 未满足时可用 `--force` 强制完成
- `attempts` 计数器每次 develop 调用时递增，超过阈值（默认 3）时建议用户介入（但不强制）

### 8.7 Feature 循环

```
analyze (complexity=complex, auto-generate feature plan)
     │
     ▼
Phase 3: 用户确认 Feature Plan
     │
     ▼
feature-next ──→ 返回下一个可执行的 feature
     │
     ▼
  develop → feature-verify → [retry if failed] → test (workspace 级) → submit-pr
     │
     ▼
feature-done ──→ 标记完成，记录 branch/pr
     │
     ├── 还有剩余 feature → 回到 feature-next
     └── 全部完成 → release workspace（可选 Phase 7 Env 验证）
```

与 rev7 的区别：
- Feature Plan 由 analyze 自动生成，不需要手动调用 `feature-plan`
- 每个 feature 的 develop 后增加 `feature-verify` 步骤（feature 级定向验证）
- verify 失败时在 feature 内部重试（develop → verify），不需要重走整个流程
- workspace 级 `test`（Phase 5）在 feature-verify 通过后执行，确保无回归

### 8.8 Agent 视角

Agent 的 SKILL.md 只需要以下规则：

> Phase 2 分析后，根据返回的 complexity 字段决定流程：
>
> **trivial**: 展示摘要，跳过确认，直接 develop → test → submit-pr
>
> **standard**: 展示分析，等用户确认，develop → test → submit-pr
>
> **complex**: analyze 已自动生成 Feature Plan。展示拆分方案，等用户确认后：
> 1. 循环：`feature-next` → `develop` → `feature-verify` → `test` → `submit-pr` → `feature-done`
> 2. `feature-verify` 失败时，重试 develop → feature-verify（最多 attempts 阈值次）
> 3. `feature-done` 检查 criteria，auto 全通过才能完成，manual 提醒用户
> 4. 每个 feature 完成后询问用户"继续下一个？"
> 5. 全部完成后 `release`

Agent 不需要管理索引、依赖、状态、criteria 文件 — 这些全部由工具内部处理。

### 8.9 中途调整

用户可以在 feature 间隙调整计划：

```
Agent: ✅ Feature 0 完成 (PR #15)，3/3 criteria 通过。
       下一个: "添加 JWT 验证"，继续？

用户: 先等等，JWT 那个不做了，直接做 session 迁移

Agent: (调用 feature-update 调整计划)
       好的，跳过 feature 1，开始 feature 2: "迁移 session 逻辑"
```

支持的调整操作：

```bash
feature-update --workspace env0 --index 1 --status skipped
feature-insert --workspace env0 --after 0 --title "..." --task "..."
feature-reorder --workspace env0 --order "0,2,1"
feature-criteria --workspace env0 --index 2 --action append --type test --target "..."
```

---

## 九、工具接口设计

### 9.1 设计原则

工具接口面向 Agent（LLM），按**语义域分组、每个工具语义明确**：

- Agent 选工具比拼参数准确率更高 — `feature-next` 比 `dispatch.py feature-next --workspace env0` 更接近自然语言
- 工具名本身携带意图，减少 SKILL.md 中的参数说明篇幅
- 每个域工具数量克制（3-5 个），避免稀释 Agent 注意力

实现上，这些工具背后仍然可以是一个 `dispatch.py` 做路由 — 这是内部实现细节，Agent 不感知。

### 9.2 工具分组

**配置域**（日常管理，不涉及工作流）

| 工具 | 用途 | 关键参数 |
|------|------|----------|
| `config-list` | 列出所有 repo/workspace/env | 无 |
| `config-add` | 添加 repo/workspace/env | kind, name, value |
| `config-set` | 设置扩展字段 | kind, name, key, value |
| `config-remove` | 删除 repo/workspace/env | kind, name |

**工作流域**（线性 Phase 推进）

| 工具 | 用途 | 关键参数 |
|------|------|----------|
| `workspace-check` | Phase 0: 确认 + 锁定 + 探测 | repos（必填，逗号分隔）, task, engine, workspace?（可选） |
| `env-probe` | Phase 1: Env 探测 | workspace, env, commands? |
| `analyze` | Phase 2: Engine 分析 + complexity 分类 | workspace, task, engine |
| `develop` | Phase 4: Engine 编码 | workspace, task, plan, branch, engine |
| `test` | Phase 5: 测试验证（workspace 级） | workspace |
| `submit-pr` | Phase 6: 提交 PR | workspace, title, body |
| `env-verify` | Phase 7: Env 部署验证 | workspace, env |
| `release` | 释放 workspace | workspace, cleanup? |
| `renew-lease` | 续期 lease（等待用户输入/部署时） | workspace |

**Feature 域**（任务拆分管理，v0.1 仅支持串行执行，不并行推进多个 feature）

| 工具 | 用途 | 关键参数 |
|------|------|----------|
| `feature-next` | 获取下一个待执行 feature | workspace |
| `feature-done` | 标记 feature 完成 | workspace, index, branch?, pr?, force? |
| `feature-list` | 查看全局进度 | workspace |
| `feature-update` | 调整单个 feature | workspace, index, ... |
| `feature-criteria` | 查看/追加 feature 的 acceptance criteria | workspace, index, action, type?, target?, description? |
| `feature-verify` | 执行 feature 级 acceptance criteria 验证 | workspace, index |

注：`feature-plan` 不再作为独立工具暴露 — Feature Plan 由 `analyze` 在 complexity=complex 时自动生成。如需手动创建（极少数情况），可通过 `feature-update` 间接实现。

### 9.3 SKILL.md 中的工具描述

每个域一段，重点描述**什么时候用**，而非参数细节：

```markdown
## 配置管理
用户要求添加/修改/删除 workspace 或 env 时使用。不涉及工作流。

## 工作流
线性推进 Phase 0→7。每个工具对应一个 Phase，返回 JSON，只需判断 success。
Phase 2 (analyze) 返回 complexity 字段：trivial 跳过确认，standard 等待确认，complex 自动生成 Feature Plan。
Phase 7 (env-verify) 可选：PR 提交后如需部署验证，等用户通知部署完成后调用。

## Feature 管理
当 analyze 返回 complexity=complex 时，Feature Plan 已自动生成。
循环 (feature-next → develop → feature-verify → test → submit-pr → feature-done) → release
feature-verify 失败时在 feature 内部重试 develop → feature-verify。
feature-criteria 可在执行过程中查看或追加验收标准。
```

---

## 十、模块设计

### 10.1 目录结构

```
skills/coding-master/
├── SKILL.md
├── scripts/
│   ├── dispatch.py             # CLI 路由（仅解析参数 + 分发调用）
│   ├── workspace.py            # Workspace 管理 + lock
│   ├── env_probe.py            # Env 探测（本地 + SSH + 自动发现）
│   ├── config_manager.py       # 配置 CRUD（供对话操作）
│   ├── feature_manager.py      # Feature Plan 管理（任务拆分 + criteria + 验证）
│   ├── test_runner.py          # 测试 + lint 执行与报告
│   ├── git_ops.py              # Git 操作（分支、提交、PR）
│   └── engine/
│       ├── __init__.py         # CodingEngine 抽象
│       ├── claude_runner.py    # Claude Code headless
│       └── codex_runner.py     # Codex CLI（v0.2）
└── README.md
```

### 10.2 dispatch.py — CLI 路由

dispatch.py 做参数解析、模块分发，以及**每步的 lock phase 更新**。不包含跨步骤编排逻辑。

对外暴露多个语义明确的工具名（见 [9.2 节](#92-工具分组)），内部统一路由：

```python
# dispatch.py 伪代码
def main():
    args = parse_args()
    result = COMMANDS[args.command](args)
    print(json.dumps(result))

COMMANDS = {
    # 配置域（不涉及 lock）
    "config-list":      lambda args: ConfigManager().list_all(),
    "config-add":       lambda args: ConfigManager().add(args.kind, args.name, args.value),
    "config-set":       lambda args: ConfigManager().set_field(args.kind, args.name, args.key, args.value),
    "config-remove":    lambda args: ConfigManager().remove(args.kind, args.name),

    # 工作流域（每步内部管理 lock phase + lease 续期）
    "workspace-check":  lambda args: WorkspaceManager().check_and_acquire(
                            repos=args.repos.split(","), task=args.task, engine=args.engine,
                            workspace=getattr(args, 'workspace', None)),
    "env-probe":        lambda args: with_lock_update("env-probe", EnvProber().probe, args),
    "analyze":          lambda args: with_lock_update("analyzing", cmd_analyze, args),
    "develop":          lambda args: with_lock_update("developing", engine_run, "develop", args),
    "test":             lambda args: with_lock_update("testing", TestRunner().run, args),
    "submit-pr":        lambda args: with_lock_update("submitted", GitOps().submit_pr, args),
    "env-verify":       lambda args: with_lock_update("env-verified", EnvProber().verify, args),
    "release":          lambda args: WorkspaceManager().release(args),
    "renew-lease":      lambda args: WorkspaceManager().renew_lease(args),

    # Feature 域（任务拆分管理）
    "feature-next":     lambda args: FeatureManager(args.workspace).next_feature(),
    "feature-done":     lambda args: FeatureManager(args.workspace).mark_done(args),
    "feature-list":     lambda args: FeatureManager(args.workspace).list_all(),
    "feature-update":   lambda args: FeatureManager(args.workspace).update(args),
    "feature-criteria": lambda args: FeatureManager(args.workspace).criteria(args),
    "feature-verify":   lambda args: with_lock_update("verifying-feature",
                            FeatureManager(args.workspace).verify, args),
}

def cmd_analyze(args):
    """Phase 2 分析：调用 Engine + 解析 complexity + 自动生成 feature plan（如 complex）"""
    result = engine_run("analyze", args)
    complexity = result.get("complexity", "standard")

    if complexity == "complex" and "feature_plan" in result:
        fm = FeatureManager(args.workspace)
        fm.create_plan_from_analysis(
            origin_task=args.task,
            features=result["feature_plan"]
        )
        result["feature_plan_created"] = True
        result["feature_count"] = len(result["feature_plan"])
    else:
        result["feature_plan_created"] = False

    return result

def with_lock_update(phase, fn, args):
    """校验 lock 存在且未过期 → 执行 fn → 更新 phase + 续 lease"""
    workspace = args.workspace    # 从 CLI 参数中提取
    lock = LockFile.load(workspace)
    lock.verify_active()          # 不存在或过期则 raise
    result = fn(args)
    lock.update_phase(phase)
    lock.renew_lease()
    lock.save()
    return result
```

CLI 接口：

```bash
# 配置管理（不涉及 lock）
dispatch.py config-list
dispatch.py config-add repo myapp git@github.com:user/myapp.git
dispatch.py config-add workspace env0 ~/lab/coding_master/env0
dispatch.py config-add env myapp-prod deploy@prod-server:/opt/myapp
dispatch.py config-set repo myapp test_command "pytest -x"
dispatch.py config-remove env myapp-staging

# 工作流步骤（每步内部管理 lock，Agent 只看 success/error）
dispatch.py workspace-check --repos myapp --task "fix: heartbeat bug" --engine claude
dispatch.py workspace-check --repos backend,frontend --task "前后端联调" --engine claude
dispatch.py workspace-check --repos myapp --workspace env0 --task "fix: heartbeat bug" --engine claude
dispatch.py env-probe --workspace env0 --env myapp-prod
dispatch.py env-probe --workspace env0 --env myapp-prod --commands "journalctl -u myapp ..."
dispatch.py analyze --workspace env0 --task "..." --engine claude
dispatch.py develop --workspace env0 --task "..." --plan "..." --branch fix/xxx --engine claude
dispatch.py test --workspace env0
dispatch.py submit-pr --workspace env0 --title "..." --body "..."
dispatch.py env-verify --workspace env0 --env myapp-staging
dispatch.py release --workspace env0
dispatch.py release --workspace env0 --cleanup    # 回滚分支（含远程）+ 释放 lock
dispatch.py renew-lease --workspace env0           # 等待用户输入时续期

# Feature 管理
dispatch.py feature-next --workspace env0
dispatch.py feature-done --workspace env0 --index 0 --branch feat/auth --pr "#15"
dispatch.py feature-done --workspace env0 --index 0 --force    # 跳过未通过 criteria
dispatch.py feature-list --workspace env0
dispatch.py feature-update --workspace env0 --index 1 --status skipped
dispatch.py feature-criteria --workspace env0 --index 0 --action view
dispatch.py feature-criteria --workspace env0 --index 0 --action append --type test --target "tests/..."
dispatch.py feature-verify --workspace env0 --index 0
```

所有输出统一 JSON stdout，Agent 只需判断 `success: true/false`。

**错误输出规范**：

```json
{"success": false, "error": "workspace env0 is locked by another session", "error_code": "WORKSPACE_LOCKED"}
```

标准 error_code 枚举：

| error_code | 含义 | Agent 建议行为 |
|------------|------|---------------|
| `WORKSPACE_LOCKED` | 被其他会话占用 | 提示用户等待或查看占用任务 |
| `PATH_NOT_FOUND` | workspace/env 路径不存在 | 提示检查配置 |
| `GIT_DIRTY` | 有未提交变更 | 提示用户先提交或 stash |
| `LOCK_NOT_FOUND` | 执行工作流命令但无 lock | 提示先 workspace-check |
| `LEASE_EXPIRED` | lock 已过期 | 自动清理后重试 |
| `SSH_UNREACHABLE` | SSH 连接失败 | 提示检查网络/密钥配置 |
| `ENGINE_TIMEOUT` | Engine 执行超时 | 建议简化任务或增加 max_turns |
| `ENGINE_ERROR` | Engine 内部错误 | 报告错误详情 |
| `COMMAND_DENIED` | Env 命令不在白名单 | 告知用户该命令被安全策略禁止 |
| `TEST_FAILED` | 测试未通过（非错误） | 报告详情，提供修复/放弃选项 |
| `CRITERIA_NOT_MET` | feature-done 时 criteria 未全部通过 | 报告未通过项，提示 --force 或继续修复 |

### 10.3 config_manager.py

```python
class ConfigManager:
    """config.yaml 的 coding_master 段 CRUD"""

    def __init__(self, config_path="~/.alfred/config.yaml"): ...
    def list_all(self) -> dict:
        """返回 repos/workspaces/envs 全部配置"""
        ...
    def add(self, kind: str, name: str, value: str) -> None:
        """Add repo/workspace/env in minimal format
        kind: "repo" | "workspace" | "env"
        """
        ...
    def set_field(self, kind: str, name: str, key: str, value: str) -> None:
        """设置扩展字段，自动将极简配置升级为扩展配置"""
        ...
    def remove(self, kind: str, name: str) -> None:          ...
    def get_repo(self, name: str) -> dict:
        """返回 {name, url, default_branch, test_command, ...}"""
        ...
    def _atomic_write(self) -> None:
        """写临时文件 → rename，防止写坏"""
        ...
```

### 10.4 workspace.py

```python
class WorkspaceManager:
    def list_workspaces(self) -> list[dict]:    ...
    def check_and_acquire(self, repos: list[str], task: str, engine: str, workspace: str = None) -> dict:
        """Phase 0 一步完成：
        1. 逐个查 repo 配置
        2. 分配空闲 workspace（遍历 workspaces，检查 lock）或使用指定 workspace
        3. acquire lock (O_CREAT|O_EXCL)
        4. 对每个 repo：clone/update 到 workspace
        5. 对每个 repo：探测 ws_path/{repo_name}
        6. 返回快照（含 repos 列表 + primary_repo）或错误
        注：.coding-master.lock 和 .coding-master/ 位于 workspace 根目录（repo 外部），无需 .gitignore"""
    def release(self, args) -> dict:
        """释放 lock + 删除 .coding-master/，--cleanup 时同时回滚每个 repo 的 git 分支（含远程）"""
    def renew_lease(self, args) -> dict:
        """显式续期 lease（等待用户输入时调用）"""
    def _find_free_workspace(self) -> dict | None:
        """遍历 workspaces 找第一个未被 lock 的"""
    def _ensure_repo(self, ws_path: str, repo_config: dict) -> str:
        """确保 repo 在 workspace 中存在：
        - 已存在 → git fetch + checkout default_branch + git pull
        - 不存在 → git clone URL 到 ws_path/{repo_name}
        返回 repo_path"""
    def _check_stale_lock(self, lock_path) -> bool:
        """检测僵尸锁：基于 lease_expires_at 判定是否过期"""
    def _probe(self, repo_path: str) -> dict:
        """探测 git/runtime/project（内部方法），路径为 ws_path/{repo_name}"""
```

### 10.5 env_probe.py

```python
class EnvProber:
    def list_envs(self) -> list[dict]:          ...
    def resolve(self, repo: str, hint: str | None) -> list[dict]:
        """根据 repo 名称 + 用户意图匹配 env 候选（repo `myapp` 匹配 env `myapp-*`）"""
    def probe(self, name, extra_commands=None) -> dict:
        """自动探测 + 可选定向命令"""
    def verify(self, args) -> dict:
        """Phase 7: 对比修复前后的 Env 状态
        1. 读取 .coding-master/env_snapshot.json 作为基线
        2. 对目标 Env 执行 auto_probe()
        3. 对比 baseline_errors vs current_errors
        4. 生成验证报告落盘 .coding-master/env_verify_report.json"""
    def _auto_discover_modules(self, path) -> list:
        """docker-compose / Procfile / systemd 自动发现"""
    def _probe_local(self, path, commands) -> dict:
    def _probe_ssh(self, connect, commands) -> dict:
    def _sanitize_command(self, cmd: str) -> str:
        """白名单校验 + shlex.quote 转义"""
```

### 10.6 feature_manager.py

```python
class FeatureManager:
    """Feature Plan 的 CRUD + acceptance criteria + 验证，管理任务拆分状态"""

    def __init__(self, workspace: str):
        self.ws_path = resolve_workspace_path(workspace)
        self.plan_path = f"{self.ws_path}/.coding-master/feature_plan.json"
        self.features_dir = f"{self.ws_path}/.coding-master/features"

    def create_plan_from_analysis(self, origin_task: str, features: list[dict]) -> dict:
        """从 analyze 的 Engine 输出创建 Feature Plan。
        1. 解析 features 列表（含 title, task, depends_on, acceptance_criteria）
        2. 写入 feature_plan.json（核心字段 + 初始 status/timestamps）
        3. 为每个 feature 创建 features/{index}/criteria.json
        返回 {success, feature_count}"""
        ...

    def next_feature(self) -> dict | None:
        """返回下一个可执行的 feature（status=pending 且 depends_on 全部 done）
        自动将其 status 设为 in_progress，写入 started_at，
        返回 feature 详情 + criteria 摘要"""
        ...

    def mark_done(self, args) -> dict:
        """标记 feature 完成，记录 branch/pr。
        检查 auto criteria 是否全部通过：
        - 全部通过 → status=done, 写入 completed_at
        - 未全部通过且无 --force → 返回 error (CRITERIA_NOT_MET)
        - 未全部通过但有 --force → status=done
        - auto: false 的 criteria 不阻塞，但在返回中提醒
        返回 {completed: N, remaining: M, pending_manual: [...], next: ...}"""
        ...

    def list_all(self) -> dict:
        """返回所有 feature 及状态摘要，含 criteria 统计"""
        ...

    def update(self, args) -> dict:
        """调整单个 feature（skip/修改 task/插入新 feature/调整顺序）"""
        ...

    def criteria(self, args) -> dict:
        """查看或追加 feature 的 acceptance criteria。
        action=view: 返回 criteria.json 内容
        action=append: 追加新 criteria 条目，更新 criteria_count"""
        ...

    def verify(self, args) -> dict:
        """执行 feature 级 acceptance criteria 验证。
        1. 读取 features/{index}/criteria.json
        2. 对每个 auto: true 的 criteria 执行验证：
           - test: 运行 pytest/npm test 指定路径
           - assert: 调用 Engine 检查代码
        3. auto: false 的 criteria 标记 passed=null，提醒用户
        4. 写入 features/{index}/verification.json
        5. 更新 feature_plan.json 中的 verified_count
        返回 {all_auto_passed: bool, results: [...], pending_manual: [...]}"""
        ...

    def _ensure_feature_dir(self, index: int) -> str:
        """确保 features/{index}/ 目录存在，返回路径"""
        ...

    def _load_criteria(self, index: int) -> list[dict]:
        """加载 features/{index}/criteria.json"""
        ...

    def _load_verification(self, index: int) -> list[dict]:
        """加载 features/{index}/verification.json，不存在则返回空列表"""
        ...
```

`feature-next` 的核心逻辑：遍历 features，找到第一个 `status == "pending"` 且 `depends_on` 中所有索引均为 `done` 的 feature。如果没有（全部完成或全部阻塞），返回相应状态让 Agent 知道是"全部完成"还是"有阻塞"。

### 10.7 test_runner.py

```python
class TestRunner:
    def run(self, workspace: str) -> TestReport:
        """执行 lint + test，返回结构化报告"""
    def _detect_commands(self, workspace_path: str) -> dict:
        """自动发现 test/lint 命令（配置优先，文件特征兜底）"""
    def _run_lint(self, workspace_path: str, cmd: str) -> LintResult:
    def _run_test(self, workspace_path: str, cmd: str) -> TestResult:

@dataclass
class TestReport:
    lint: LintResult
    test: TestResult
    overall: str          # "passed" | "failed"
```

---

## 十一、交互协议

### 11.1 Bug 修复（远程 Env）

```
用户: myapp 线上的 heartbeat 有 bug，定时任务没触发

Agent: 📦 Repo: myapp
       📁 Workspace: env0 (自动分配)
       🖥️ Env: myapp-prod (ssh → prod-server:/opt/myapp)
       正在确认环境...

Agent: [Workspace: env0] main 分支，无未提交变更，Python 3.12.4
       [Env: myapp-prod]
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
       复杂度: standard
       是否修复？

用户: 修吧

Agent: 编码完成，正在测试...

Agent: ✅ 测试通过 (42 passed, ruff clean)
       📝 heartbeat.py (+3, -2)
       是否提交 PR？

用户: 提交

Agent: PR 已创建: https://github.com/.../pull/15
       需要部署到 staging 验证吗？

用户: 等我部署看看

       ... (用户通过 CI/CD 部署到 staging) ...

用户: 部署好了，帮我看下

Agent: (调用 env-verify --workspace env0 --env myapp-staging)
       ✅ Env 验证通过:
         修复前: 2 个 heartbeat 相关错误
         修复后: 30 分钟内无相关错误
       确认关闭任务？

用户: 好的

Agent: 已释放 workspace env0。
```

**跳过 Env 验证**（无关联 Env 或用户不需要）：

```
用户: 提交

Agent: PR 已创建: https://github.com/.../pull/15
       已释放 workspace env0。
```

### 11.2 Bug 修复（测试失败场景）

```
用户: 修一下 myapp 的日志轮转问题

Agent: ... (Phase 0-3 同上) ...

用户: 修吧

Agent: 编码完成，正在测试...

Agent: ❌ 测试未通过 (2 failed / 42 total):
       • test_log_rotate_daily: expected 7 files, got 8
       • test_log_rotate_cleanup: FileNotFoundError
       选择：
       1. 让我修一下
       2. 我手动看看
       3. 放弃修改

用户: 1

Agent: 修复完成，重新测试...

Agent: ✅ 测试通过 (42 passed, ruff clean)
       📝 log_handler.py (+12, -5), test_log.py (+3, -1)
       是否提交 PR？
```

### 11.3 功能开发（trivial，跳过确认）

```
用户: 帮我把 myapp 的 config.py 里 DEFAULT_TIMEOUT 改成 30

Agent: 📦 Repo: myapp → 📁 Workspace: env0 (自动分配), main 分支 ✓
       功能开发，跳过 Env 探测。开始分析？

用户: 开始

Agent: 分析完成：config.py:12 DEFAULT_TIMEOUT = 10 → 30，复杂度: trivial
       自动开始修复...

Agent: 编码完成，正在测试...

Agent: ✅ 测试通过 (42 passed, ruff clean)
       📝 config.py (+1, -1)
       是否提交 PR？
```

### 11.4 多 Repo 任务

```
用户: 后端加个 /api/users 接口，前端调用展示用户列表

Agent: 📦 Repos: backend (primary) + frontend
       📁 Workspace: env0 (自动分配)
       backend: main ✓, frontend: main ✓
       功能开发，跳过 Env 探测。开始分析？

用户: 开始

Agent: 分析完成：
       [backend] 新增 GET /api/users 路由，返回用户列表 JSON
       [frontend] 新增 UserList 组件，调用 /api/users
       复杂度: standard
       是否修复？

用户: 修吧

Agent: 编码完成，正在测试...

Agent: ✅ 测试通过
       [backend] pytest: 28 passed, ruff clean
       [frontend] npm test: 15 passed, eslint clean
       📝 backend: routes/users.py (+35), frontend: src/UserList.tsx (+42)
       是否提交 PR？

用户: 提交

Agent: PR 已创建:
       [backend] https://github.com/.../pull/23 (关联: frontend#24)
       [frontend] https://github.com/.../pull/24 (关联: backend#23)
       已释放 workspace env0。
```

### 11.5 配置管理

```
用户: 添加 repo my-app git@github.com:user/my-app.git
Agent: ✅ repo my-app → git@github.com:user/my-app.git

用户: 添加 env my-app-prod root@server:/opt/my-app
Agent: ✅ my-app-prod → root@server:/opt/my-app, SSH ✅ 可达

用户: 列出环境
Agent: Repos: myapp, my-app
       Workspaces: env0 [idle], env1 [idle], env2 [idle]
       Envs: myapp-prod [ssh ✅], my-app-prod [ssh ✅]
```

### 11.6 Feature 循环交互（complex 任务）

```
用户: 帮我重构 myapp 的认证系统

Agent: 📦 Repo: myapp → 📁 Workspace: env0 (自动分配), main 分支 ✓
       功能开发，跳过 Env 探测。开始分析？

用户: 开始

Agent: 分析完成，复杂度: complex。已自动生成 Feature Plan (3 步)：

       Feature 0: 抽取 auth middleware
         ├── criteria: test(test_auth.py) + assert(middleware in app factory)
         └── depends_on: 无
       Feature 1: 添加 JWT 验证
         ├── criteria: test(test_jwt.py) + assert(token validation)
         └── depends_on: [0]
       Feature 2: 迁移 session 逻辑
         ├── criteria: test(test_session_migration.py) + manual(login flow on staging)
         └── depends_on: [1]

       按这个计划推进？

用户: 可以

Agent: 开始 Feature 0/3: 抽取 auth middleware
       编码完成，正在验证 acceptance criteria...

Agent: ✅ Feature 验证通过:
         c0 (test): tests/unit/test_auth.py — 3 passed ✅
         c1 (assert): middleware registered in create_app() ✅
       正在运行全量测试...

Agent: ✅ 全量测试通过 (45 passed, ruff clean)
       📝 auth_middleware.py (+45), app.py (-30)
       是否提交 PR？

用户: 提交

Agent: PR #15 已创建。
       ✅ Feature 0/3 完成 (2/2 criteria 通过)。
       下一个: Feature 1/3 "添加 JWT 验证"，继续？

用户: JWT 那个不做了，直接做 session 迁移

Agent: 好的，跳过 Feature 1，开始 Feature 2/3: "迁移 session 逻辑"
       编码完成，正在验证 acceptance criteria...

Agent: ✅ Feature 验证:
         c0 (test): tests/unit/test_session_migration.py — 5 passed ✅
         c1 (manual): "login flow works on staging" — ⏳ 待用户确认
       正在运行全量测试...

Agent: ✅ 全量测试通过 (48 passed, ruff clean)
       📝 session.py (+20, -35), auth.py (+5, -10)
       注意: 还有 1 个 manual criteria 待确认
       是否提交 PR？

用户: 提交

Agent: PR #16 已创建。
       ✅ Feature 2/3 完成 (1/2 auto criteria 通过, 1 manual 待确认)。
       全部 Feature 完成。已释放 workspace env0。
       提醒: Feature 2 有 manual criteria "login flow works on staging" 待验证。
```

### 11.7 Env 验证失败 → 迭代修复

```
Agent: PR 已创建: https://github.com/.../pull/15
       需要部署到 staging 验证吗？

用户: 部署好了，帮我看下 staging

Agent: (调用 env-verify --workspace env0 --env myapp-staging)
       ❌ Env 验证未通过:
         修复前: ERROR heartbeat: Task 'daily-report' skipped
         修复后: ERROR heartbeat: Task 'daily-report' timeout (新错误)
       选择：
       1. 让我再修一轮
       2. 我手动看看
       3. 回滚修改

用户: 1

Agent: (回到 Phase 4，带着 env 验证报告作为上下文)
       编码完成，正在测试...

Agent: ✅ 测试通过 (43 passed, ruff clean)
       📝 heartbeat.py (+5, -3)
       是否提交 PR？（追加到现有 PR #15）

用户: 提交

Agent: PR #15 已更新 (force push)。部署后要再验证吗？

用户: 部署好了，看看

Agent: ✅ Env 验证通过:
         修复后 30 分钟内无 heartbeat 相关错误
       已释放 workspace env0。
```

### 11.8 Feature 验证失败 → 重试

```
Agent: 开始 Feature 1/3: 添加 JWT 验证
       编码完成，正在验证 acceptance criteria...

Agent: ❌ Feature 验证未通过:
         c0 (test): tests/unit/test_jwt.py — 1 failed ❌
           test_token_expiry: AssertionError: expected 401, got 200
         c1 (assert): token validation in middleware ✅
       尝试修复... (attempt 2/3)

Agent: 修复完成，重新验证...

Agent: ✅ Feature 验证通过:
         c0 (test): tests/unit/test_jwt.py — 4 passed ✅
         c1 (assert): token validation in middleware ✅
       正在运行全量测试...
```

### 11.9 中断与恢复

| 场景 | Agent 行为 |
|------|-----------|
| Phase 0-2 中取消 | 释放 lock，无需 git 清理 |
| Phase 4-5 中取消 | 释放 lock，删除本地分支，checkout 回原分支 |
| Phase 6 后取消 | PR 已创建，提示用户手动 close |
| Phase 7 等待部署中取消 | PR 已创建，释放 lock，提示用户部署后自行验证 |
| Phase 7 验证失败 | 报告对比结果，提供再修一轮/手动处理/回滚选项 |
| Feature 循环中取消 | 已完成的 feature PR 保留，当前 feature 按 Phase 4-5 规则清理 |
| 长时间无回复 | lock 保留，下次对话时 Agent 检测到 lock 提示未完成任务 |
| Daemon 重启 | Agent 通过 lock + `.coding-master/` 阶段产物恢复上下文，向用户确认是否继续 |

---

## 十二、安全与约束

### 12.1 Coding Engine 权限

`--allowedTools "Bash,Read,Edit,Write,Glob,Grep"`，prompt 约束只在 workspace 内操作。

禁止：`rm -rf`、`git push --force`、`git reset --hard`、修改 `.env` / credentials。

### 12.2 Env 访问安全

- **严格只读**，禁止写入/重启/部署
- **白名单机制**：仅允许以下命令前缀
  - 日志类：`cat`, `tail`, `head`, `grep`, `journalctl`, `docker logs`
  - 状态类：`ps`, `uptime`, `df`, `free`, `systemctl status`, `docker ps`
  - 配置类：`printenv`, `env`（自动过滤敏感值）
  - 不在白名单内的命令 → 拒绝执行并报错
- **路径沙箱**：文件类命令（`cat`, `tail`, `head`, `grep`）的路径参数必须在 env 配置的根目录下，禁止访问 `/etc/shadow`、`~/.ssh/` 等系统敏感路径。路径经 `os.path.realpath()` 解析后校验，防止符号链接逃逸
- **非交互约束**：仅允许非交互命令；禁止需要 TTY/分页器的命令（如 `less`, `top`）
- **参数转义**：所有命令参数经 `shlex.quote()` 处理，防止注入
- **敏感信息过滤**：输出中 SECRET/PASSWORD/TOKEN/KEY 等 pattern 的值替换为 `***`
- **日志消毒**：Env 采集的日志内容注入 Engine prompt 前，截断单条日志至 500 字符，总日志量上限 10KB，降低间接 prompt injection 风险（线上日志可能包含用户可控的 HTTP 请求体等）
- **超时**：单次命令 30s，整体探测 120s，超时后优雅降级（返回已采集部分 + 超时提示）

### 12.3 Git 安全

- 只允许 feature/fix 分支，不直接 push main
- PR 不自动 merge，必须人工 review
- force push 默认禁止

### 12.4 成本控制

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `max_turns` | 30 | 单次 engine 调用最大轮次 |
| `timeout` | 600s | 单次 engine 调用超时 |
| `max_test_fix_rounds` | 2 | 测试失败后自动修复最大轮次 |
| `max_feature_attempts` | 3 | 单个 feature develop 重试上限 |
| `lease_duration` | 7200s (2h) | Lock 租约时长 |

### 12.5 .gitignore

`.coding-master.lock` 和 `.coding-master/` 位于 workspace 根目录（而非 repo 内部），不会被 git 跟踪，无需 `.gitignore` 配置。

---

## 十三、实现路线图

### v0.1 — 基础能力

- [ ] SKILL.md — Agent 工作流编排 prompt（含 complexity 自适应流程）
- [ ] config_manager.py — 极简/扩展配置解析 + 对话式 CRUD（含 repos CRUD）
- [ ] workspace.py — Workspace 自动分配（找空闲）、lock（含僵尸锁检测）、单/多 repo clone/update、探测
- [ ] env_probe.py — 本地/SSH 探测 + 多模块自动发现 + 命令白名单
- [ ] feature_manager.py — Feature Plan CRUD + acceptance criteria + feature-level verification
- [ ] test_runner.py — test + lint 执行与结构化报告
- [ ] dispatch.py — CLI 路由（多工具入口，仅参数解析 + 模块分发，含 cmd_analyze complexity 解析）
- [ ] engine/claude_runner.py — Claude Code headless（analyze prompt 含 complexity 分类）
- [ ] git_ops.py — 分支、提交、PR
- [ ] 端到端验证：对话 → 配置 → 探测 → 分析 → 开发 → 测试 → PR
- [ ] 端到端验证：complexity=trivial 跳过确认 → 自动开发
- [ ] 端到端验证：complexity=complex → 自动 Feature Plan → Feature 循环（含 criteria 验证）→ 多 PR

### v0.2 — 扩展

- [ ] Repo 新建流程（`gh repo create` + 初始化）
- [ ] engine/codex_runner.py
- [ ] Engine 选择策略
- [ ] Git worktree 并行任务
- [ ] 双 engine 对比模式

### v0.3 — 增强

- [ ] CI 状态监控（PR 创建后跟踪 CI 结果）
- [ ] 任务历史与统计
- [ ] HEARTBEAT 集成（定期检查 issue 自动修复）
- [ ] Env 探测缓存
