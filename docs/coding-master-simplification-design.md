# Coding-Master Dispatch 简化设计

## 1. 问题陈述

### 1.1 核心问题：Agent 不走 dispatch

从 demo_agent 会话轨迹观察到：

- Agent 在 review 阶段正确输出了 `workspace-check → develop → test → submit-pr` 流程
- 但在实际开发阶段，**全程用 `_bash` 自己写代码**，完全绕过 dispatch
- 连续触发 `REPEATED_TOOL_INTENT` 和 `REPEATED_TOOL_FAILURES`
- 最终 27 个测试只过 6 个，任务未完成

### 1.2 原因分析

| 原因 | 权重 | 证据 |
|------|------|------|
| **命令太多太复杂** | 高 | 26 个命令，agent 需要记住读写分离、lock 管理、5 步写入流程 |
| **Skill 指令是一次性的** | 高 | `_load_resource_skill` 返回内容在上下文压缩后丢失 |
| **develop 和 test 割裂** | 中 | DEVELOP_PROMPT 明确写 "Do NOT run tests"，engine 写完就停 |
| **dispatch 路径摩擦大** | 中 | 完成一次开发需要 5 步（workspace-check → develop → test → submit-pr → release） |
| **模型倾向直接行动** | 低 | kimi-code 看到"开发"直接 bash，不走间接路线 |

### 1.3 次要问题

- **pytest 找不到**：uv venv 无 pip，agent 用 `python -m pytest` 失败 6 次（已修复）
- **路径幻觉**：agent 猜测不存在的文件路径（已通过 directory_structure 修复）
- **Daemon 隔离失败**：从 workspace clone 启动 daemon 导致主控崩溃（待修复）

## 2. 设计目标

1. **Agent 只需记住 6 个命令**，覆盖 90% 场景
2. **一步完成标准开发**：`auto-dev` 封装 workspace-check → develop(含test循环) → final test → report
3. **Engine 内部迭代**：write → test → fix 循环在 engine 层完成
4. **按需发现**：复杂命令通过返回值引导、`--help`、full mode 发现
5. **复杂任务自动升级**：auto-dev 检测到需要拆分时，先落到 workspace，再引导 agent 走 feature 流程
6. **锁生命周期可收尾**：标准开发完成后，agent 必须有显式 release 路径；成功提交 PR 默认自动 release

## 3. 关键 Tradeoff

### T1: Engine 内循环 vs Dispatch 层循环

| 方案 | 优点 | 缺点 |
|------|------|------|
| **Engine 内循环（选定）** | engine (claude/codex) 天然具备 write→test→fix 能力；保持上下文连续性 | engine 黑盒，dispatch 无法干预中间过程 |
| Dispatch 层循环 | dispatch 可控制每轮策略 | 每次 develop 是独立 engine 调用，丢失修复上下文；多轮调用慢 |
| Agent LLM 层循环 | 最灵活 | 步骤多→更容易绕过 dispatch；消耗 agent 上下文 |

**决策**：Engine 内循环。DEVELOP_PROMPT 传入 test 命令，engine 自己跑测试并修复。dispatch 只做最终验证。

### T2: auto-dev 粒度 — 含 PR 还是不含

| 方案 | 优点 | 缺点 |
|------|------|------|
| **auto-dev 不含 PR（选定）** | PR 需要 title/body，agent 应有决策权；失败时不会产生垃圾 PR | 多一步 |
| auto-dev 含 PR | 真正一步到位 | agent 失去对 PR 的控制；引擎测试通过≠业务正确 |

**决策**：auto-dev 到"代码写完 + dispatch 最终验证通过"为止。PR 由 agent 决定是否提交（`$D submit`）。

### T3: 复杂度判断时机

| 方案 | 优点 | 缺点 |
|------|------|------|
| auto-dev 前先 analyze | 准确判断 | 多一步调用，90% 任务是 standard 不需要 |
| **auto-dev 内部快速判断（选定）** | 流畅；标准任务零额外开销 | 简单启发式可能误判 |
| Agent 自己判断 | 灵活 | Agent 判断不靠谱 |

**决策**：auto-dev 内部做轻量复杂度判断（task 文本信号 + 计划规模启发式）。明显复杂的任务返回 `TASK_TOO_COMPLEX` + hint。需要时允许 `--allow-complex` 强制放行。模糊地带放行，让 engine 尝试。

### T4: SKILL.md 分层 — short/full vs 单一文档

| 方案 | 优点 | 缺点 |
|------|------|------|
| **short/full 双模式（选定）** | 复用现有 `_load_resource_skill(name, mode)` 机制 | 两份文档需同步维护 |
| 单一文档 + 折叠 | 一份文档 | LLM 不理解"折叠"，全部注入或全部不注入 |

**决策**：SKILL.md 保持为 short 版（6 个命令，含 `release`）。full 内容移到 `references/full-command-reference.md`。

## 4. 逻辑架构

```
Agent (kimi-code / any LLM)
  │
  │  只需知道 6 个命令
  │
  ▼
┌─────────────────────────────────────────────────────┐
│ SKILL.md (short mode) — 常驻认知                      │
│                                                     │
│  status   — 看状态                                   │
│  find     — 搜代码                                   │
│  analyze  — 深度分析                                  │
│  test     — 跑测试                                   │
│  auto-dev — 开发（一步到位）                           │
│  release  — 释放 workspace 锁                          │
│                                                     │
│  > 更多命令: load full mode 或 $D --help              │
└──────────────┬──────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────┐
│ dispatch.py — 路由 + 编排                             │
│                                                     │
│  auto-dev 内部流程:                                   │
│  ┌─────────────────────────────────────────┐        │
│  │ 1. 复杂度 + 目标 repo 快速判断              │        │
│  │    → complex? 返回 TASK_TOO_COMPLEX       │        │
│  │    → multi-repo? 返回 NEED_EXPLICIT_REPO  │        │
│  │    → standard/trivial? 继续               │        │
│  │                                          │        │
│  │ 2. workspace-check (获取锁)               │        │
│  │                                          │        │
│  │ 3. resolve target_repo + engine.run(...)  │        │
│  │    ┌─ engine 内部 ───────────┐           │        │
│  │    │ write code              │           │        │
│  │    │ run test_cmd            │           │        │
│  │    │ if fail → fix → rerun   │ ← 循环    │        │
│  │    │ until pass/max_turns    │           │        │
│  │    └─────────────────────────┘           │        │
│  │                                          │        │
│  │ 4. final test on target_repo              │        │
│  │                                          │        │
│  │ 5. report + sync CODING_STATS.md         │        │
│  └─────────────────────────────────────────┘        │
│                                                     │
│  返回值驱动导航:                                      │
│  ok=true  → {data, next_step: "submit or continue"} │
│  ok=false → {error_code, hint: "下一步建议"}          │
│                                                     │
└─────────────────────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────┐
│ Engine (claude / codex)                              │
│  接收: prompt + test_cmd + target_repo               │
│  自主: write → test → fix 循环                        │
│  返回: EngineResult(success, summary, files_changed) │
└─────────────────────────────────────────────────────┘
```

### 4.1 复杂任务升级路径

```
Agent: $D auto-dev --repos alfred --task "重构整个 inspector 模块"
                │
                ▼
        dispatch 快速判断: task 描述含"重构"+"整个"→ 可能 complex
                │
                ▼
        自动申请 workspace + 返回:
        {"ok": false, "error_code": "TASK_TOO_COMPLEX",
         "data": {"workspace": "env0"},
         "hint": "建议先分析: $D analyze --workspace env0 --task '...'"}
                │
                ▼
Agent: $D analyze --workspace env0 --task "重构整个 inspector 模块"
                │
                ▼
        engine 分析后返回 complexity=complex + feature_plan
        dispatch 自动创建 feature plan（写入 env0/.coding-master）
                │
                ▼
        返回: {"ok": true, "data": {
                 "complexity": "complex",
                 "feature_plan_created": true,
                 "feature_count": 4,
                 "next_step": "$D auto-dev --workspace <ws> --feature next"
               }}
                │
                ▼
Agent: $D auto-dev --workspace env0 --feature next
                │
                ▼
        dispatch 取 feature-next → 读取 feature.index + feature.task 调 engine
        final test 通过后自动 feature-done(index=current)
                │
                ▼
        返回: {"ok": true, "data": {
                 "feature": "1/4: SessionScanner 覆盖扩展",
                 "tests_passed": true,
                 "next_step": "$D auto-dev --workspace env0 --feature next"
               }}
                │
                ▼
        Agent 重复调用直到所有 feature 完成
```

## 5. 详细方案

### 5.1 SKILL.md (short 版) 重写

```markdown
---
name: coding-master
description: "Code review, development, and ops for registered repos"
version: "0.2.0"
---

# Coding Master Skill

`$D = python $SKILL_DIR/scripts/dispatch.py`

所有命令返回 JSON `{"ok": true, "data": {...}}` 或 `{"ok": false, "error_code": "...", "hint": "..."}`.
失败时按 `hint` 操作。

## 核心命令

| 命令 | 说明 | timeout |
|------|------|---------|
| `$D status --repos <name>` | git 状态 / diff 概览 | 默认 |
| `$D find --repos <name> --query <pattern>` | 搜索代码 | 默认 |
| `$D analyze --repos <name> --task "<desc>"` | 引擎深度分析 | 600 |
| `$D test --repos <name>` | 跑测试 | 默认 |
| `$D auto-dev --repos <name> --task "<desc>"` | 单 repo 开发+测试（一步到位） | 600 |
| `$D release --workspace <ws>` | 释放 workspace 锁 | 默认 |

> 未标注 timeout 的命令默认 120s。

### auto-dev 选项

- `--branch <name>` — 指定分支名（默认自动生成）
- `--engine <claude|codex>` — 指定引擎（默认读配置）
- `--feature next` — 开发 feature plan 中的下一个任务
- `--workspace <name>` — 指定已有 workspace（feature mode 必填）
- `--repo <name>` — 在 multi-repo workspace 中指定目标 repo
- `--plan "<desc>"` — 指定实现方案（默认用 analyze 报告）
- `--allow-complex` — 跳过复杂度拦截，强制尝试单次 auto-dev
- `--reset-worktree` — 清空当前 workspace repo 的未提交改动后重新开发

### auto-dev 边界

- `auto-dev` 的执行单位是 **单个目标 repo**
- `--repos <name>` 仅支持单 repo；传多个 repo 直接返回 `TASK_TOO_COMPLEX`
- `--workspace <ws>` 模式下，如果 workspace 中包含多个 repo，必须显式传 `--repo <name>`；否则返回 `NEED_EXPLICIT_REPO`
- engine 开发 cwd、开发时 test 命令、dispatch final test 三者都必须绑定到同一个 `target_repo`

### 提交 PR

auto-dev 不自动提交 PR。测试通过后手动提交：

- repo mode: `$D submit --repos <name> --title "<title>"`
- workspace / feature mode: `$D submit --workspace <ws> --title "<title>"`

默认行为：`submit` 成功后自动执行 `release`。如需保留 workspace 继续工作，显式加 `--keep-lock`。

## 规则

- **所有代码操作必须通过 $D 命令**，禁止直接 _bash 操作代码仓库
- engine 命令（analyze, auto-dev）需 `timeout=600`
- 失败时检查 `error_code` + `hint`，按提示操作
- `auto-dev` / `submit` 完成后若未继续开发，必须 `release`

> 更多命令（workspace 管理、feature 拆分、环境探测等）：
> `_load_skill_resource("coding-master", "references/full-command-reference.md")`
> 或 `$D --help`
```

### 5.2 DEVELOP_PROMPT 改造

```python
DEVELOP_PROMPT = """\
## Development Environment (Workspace)
{workspace_snapshot}

## Diagnosis Report
{analysis}

## User-Confirmed Plan
{plan}

## Task
Implement the fix/feature based on the diagnosis report above.
Task: {task}

## Test Command
{test_command}

Rules:
- Only modify files within this repository
- After implementing, run the test command to verify
- If tests fail, fix the code and re-run until tests pass
- Do NOT commit — that will be done separately
- Keep changes minimal and focused
"""
```

### 5.3 CodingEngine 接口扩展

```python
# engine/__init__.py
class CodingEngine(ABC):
    @abstractmethod
    def run(
        self,
        repo_path: str,
        prompt: str,
        max_turns: int = 30,
        timeout: int = 600,
    ) -> EngineResult:
        ...
```

接口不变。test 命令通过 prompt 传入，engine 自然会执行。claude/codex 都具备
read→write→run→fix 的迭代能力，不需要在 runner 层额外编排。

### 5.4 cmd_auto_dev 实现

```python
def cmd_auto_dev(args) -> dict:
    config = ConfigManager()
    mgr = WorkspaceManager(config)

    # ── 0. Resolve feature mode first ──
    feature_mode = getattr(args, "feature", None) == "next"
    current_feature = None

    if feature_mode:
        ws_name = getattr(args, "workspace", None)
        if not ws_name:
            return {
                "ok": False,
                "error": "--feature next requires --workspace",
                "error_code": "MISSING_WORKSPACE",
                "hint": "用 analyze 返回的 workspace 名称: $D auto-dev --workspace env0 --feature next",
            }
        ws = config.get_workspace(ws_name)
        if ws is None:
            return {
                "ok": False,
                "error": f"workspace '{ws_name}' not found",
                "error_code": "NO_WORKSPACE",
            }
        ws_path = ws["path"]

        fm = FeatureManager(ws_path)
        feat = fm.next_feature()
        if not feat.get("ok"):
            _sync_coding_stats()
            return feat

        current_feature = feat["data"].get("feature")
        if current_feature is None:
            return {
                "ok": False,
                "error": "no executable feature available",
                "error_code": "NO_FEATURE",
                "hint": "运行 $D feature-list 查看当前 plan 状态",
            }

    # ── 1. 复杂度 + target repo 快速判断 ──
    task = current_feature["task"] if current_feature else args.task
    if not getattr(args, "allow_complex", False) and _looks_complex(task):
        workspace_hint = None
        if getattr(args, "repos", None):
            repo_names = [r.strip() for r in args.repos.split(",") if r.strip()]
            ws_result = mgr.check_and_acquire_for_repos(repo_names, task, args.engine or config.get_default_engine())
            if ws_result.get("ok"):
                workspace_hint = ws_result["data"]["snapshot"]["workspace"]["name"]
        return {
            "ok": False,
            "error_code": "TASK_TOO_COMPLEX",
            **({"data": {"workspace": workspace_hint}} if workspace_hint else {}),
            "hint": (
                f"建议先分析: $D analyze --workspace {workspace_hint} --task \"{task}\""
                if workspace_hint
                else f"建议先分析: $D analyze --repos {args.repos} --task \"{task}\""
            ),
        }

    # ── 2. workspace-check ──
    engine_name = args.engine or config.get_default_engine()
    engine = _get_engine(engine_name)
    if engine is None:
        return {"ok": False, "error": f"unknown engine: {engine_name}",
                "error_code": "ENGINE_ERROR"}

    if feature_mode:
        pass  # ws_path, ws_name already resolved in step 0
    elif getattr(args, "workspace", None):
        ws_name = args.workspace
        ws = config.get_workspace(ws_name)
        if ws is None:
            return {
                "ok": False,
                "error": f"workspace '{ws_name}' not found",
                "error_code": "NO_WORKSPACE",
            }
        ws_path = ws["path"]
    else:
        repo_names = [r.strip() for r in args.repos.split(",") if r.strip()]
        ws_result = mgr.check_and_acquire_for_repos(repo_names, task, engine_name)
        if not ws_result.get("ok"):
            return ws_result

        ws_path = ws_result["data"]["snapshot"]["workspace"]["path"]
        ws_name = ws_result["data"]["snapshot"]["workspace"]["name"]

    # ── 2.5. Reset worktree if requested ──
    if getattr(args, "reset_worktree", False):
        clean_result = _clean_workspace_repos(ws_path, _load_artifact(ws_path, "workspace_snapshot.json"))
        if not clean_result.get("ok"):
            return clean_result

    # ── 3. Resolve target repo + test command ──
    target = _resolve_dev_target(
        ws_path=ws_path,
        ws_name=ws_name,
        repos_arg=getattr(args, "repos", None),
        repo_arg=getattr(args, "repo", None),
    )
    if not target.get("ok"):
        return target
    repo_path = target["data"]["repo_path"]
    repo_name = target["data"]["repo_name"]
    test_cmd = _resolve_test_command(repo_path, config, ws_name, repo_name)

    # ── 4. Build prompt + run engine ──
    ws_snapshot = _load_artifact(ws_path, "workspace_snapshot.json")
    analysis = _load_artifact(ws_path, "phase2_analysis.md")

    prompt = DEVELOP_PROMPT.format(
        workspace_snapshot=ws_snapshot,
        analysis=analysis,
        plan=current_feature.get("plan") if current_feature else getattr(args, "plan", None) or "(proceed with recommended approach)",
        task=task,
        test_command=test_cmd or "(no test command available)",
    )

    max_turns = config.get_max_turns()

    def do_dev():
        # Branch (create_branch 应为幂等：已存在则 checkout)
        if args.branch:
            git = GitOps(ws_path)
            br_result = git.create_or_checkout_branch(args.branch)
            if not br_result.get("ok"):
                return br_result

        result = engine.run(repo_path, prompt, max_turns=max_turns)
        if not result.success:
            return {
                "ok": False,
                "error": result.error,
                "error_code": "ENGINE_ERROR",
                "hint": "引擎失败，可换引擎重试: --engine codex 或 --engine claude",
            }

        # ── 5. Final test (dispatch 独立验证) ──
        runner = TestRunner(config)
        test_result = runner.run_repo(ws_name, repo_name)
        tests_passed = test_result.get("ok") and test_result.get("data", {}).get("overall") == "passed"

        if not tests_passed:
            return {
                "ok": False,
                "error": "final verification failed after engine completed development",
                "error_code": "FINAL_TEST_FAILED",
                "data": {
                    "summary": result.summary,
                    "files_changed": result.files_changed,
                    "test_report": test_result.get("data", {}),
                },
                "hint": "查看失败报告后重试 auto-dev，或手动运行 $D test / $D analyze",
            }

        # ── 6. Feature done ──
        if feature_mode and tests_passed:
            fm = FeatureManager(ws_path)
            fm.mark_done(index=current_feature["index"], force=False)

        return {
            "ok": True,
            "data": {
                "summary": result.summary,
                "files_changed": result.files_changed,
                "tests_passed": tests_passed,
                "test_report": test_result.get("data", {}),
            },
            "next_step": _suggest_next(args, feature_mode=feature_mode, tests_passed=tests_passed),
        }

    result = with_lock_update(ws_path, "developing", do_dev)
    _sync_coding_stats()
    return result


def _looks_complex(task: str) -> bool:
    """Heuristic: does the task description suggest multi-feature complexity?"""
    # v1 启发式，后续可引入 engine 轻量预判替代
    complex_signals = ["重构", "refactor", "redesign", "整个", "所有", "全部",
                       "多模块", "cross-cutting", "新子系统", "new subsystem"]
    task_lower = task.lower()
    return sum(1 for s in complex_signals if s in task_lower) >= 2


def _resolve_dev_target(
    ws_path: str,
    ws_name: str,
    repos_arg: str | None,
    repo_arg: str | None,
) -> dict:
    """Resolve the single repo that auto-dev will modify and test."""
    if repos_arg:
        repo_names = [r.strip() for r in repos_arg.split(",") if r.strip()]
        if len(repo_names) != 1:
            return {
                "ok": False,
                "error": "auto-dev only supports a single repo target",
                "error_code": "TASK_TOO_COMPLEX",
                "hint": "先运行 analyze 拆分任务，或改为单 repo 调用 auto-dev",
            }
        return {"ok": True, "data": {
            "repo_name": repo_names[0],
            "repo_path": str(Path(ws_path) / repo_names[0]),
        }}

    snapshot = json.loads(_load_artifact(ws_path, "workspace_snapshot.json"))
    repos = snapshot.get("repos", [])
    if len(repos) == 1:
        only = repos[0]
        return {"ok": True, "data": {
            "repo_name": only["name"],
            "repo_path": only["path"],
        }}

    if repo_arg:
        for repo in repos:
            if repo["name"] == repo_arg:
                return {"ok": True, "data": {
                    "repo_name": repo["name"],
                    "repo_path": repo["path"],
                }}

    return {
        "ok": False,
        "error": "multi-repo workspace requires explicit --repo",
        "error_code": "NEED_EXPLICIT_REPO",
        "hint": f"示例: $D auto-dev --workspace {ws_name} --repo <name> --task \"...\"",
    }


def _resolve_test_command(repo_path: str, config: ConfigManager, ws_name: str, repo_name: str) -> str | None:
    """Get the test command for the target repo only."""
    from test_runner import _resolve_pytest_command
    p = Path(repo_path)
    ws = config.get_workspace(ws_name)
    if ws and ws.get("repos", {}).get(repo_name, {}).get("test_command"):
        return ws["repos"][repo_name]["test_command"]
    if (p / "pyproject.toml").exists():
        return _resolve_pytest_command(p)
    return None


def _clean_workspace_repos(ws_path: str, workspace_snapshot: str) -> dict:
    """Reset tracked/untracked changes for repos inside the workspace snapshot."""
    try:
        snapshot = json.loads(workspace_snapshot)
    except Exception:
        return {
            "ok": False,
            "error": "workspace snapshot unavailable for reset",
            "error_code": "NO_SNAPSHOT",
            "hint": "先运行 workspace-check，或手动清理 workspace 后重试",
        }

    repos = snapshot.get("repos", [])
    for repo in repos:
        repo_path = repo.get("path")
        if not repo_path:
            continue
        result = GitOps.force_clean(repo_path)
        if not result.get("ok"):
            return {
                "ok": False,
                "error": result.get("error", f"failed to clean repo: {repo_path}"),
                "error_code": "GIT_ERROR",
                "hint": f"手动检查仓库后重试: {repo_path}",
            }

    return {"ok": True}


def _suggest_next(args, feature_mode: bool, tests_passed: bool) -> str:
    workspace_mode = bool(getattr(args, "workspace", None)) and not feature_mode

    if feature_mode:
        if tests_passed:
            return (
                f"当前 feature 完成。继续下一个 feature: "
                f"$D auto-dev --workspace {args.workspace} --feature next"
            )
        return (
            f"当前 feature 验证失败（next 会重试同一个未完成 feature）。两个选择：\n"
            f"1. 继续修复（保留已有改动）: "
            f"$D auto-dev --workspace {args.workspace} --feature next\n"
            f"2. 清空当前 workspace 未提交改动后重试: "
            f"$D auto-dev --workspace {args.workspace} --feature next --reset-worktree"
        )

    if workspace_mode:
        if tests_passed:
            return f"测试通过。提交 PR: $D submit --workspace {args.workspace} --title \"<title>\""
        return (
            f"测试未全部通过。两个选择：\n"
            f"1. 继续修复（保留已有改动）: "
            f"$D auto-dev --workspace {args.workspace} --task \"fix failing tests\"\n"
            f"2. 清空当前 workspace 未提交改动后重试原任务: "
            f"$D auto-dev --workspace {args.workspace} --task \"{args.task}\" --reset-worktree"
        )

    if tests_passed:
        return f"测试通过。提交 PR: $D submit --repos {args.repos} --title \"<title>\""

    return (
        f"测试未全部通过。两个选择：\n"
        f"1. 继续修复（保留已有改动）: "
        f"$D auto-dev --repos {args.repos} --task \"fix failing tests\"\n"
        f"2. 清空 workspace 未提交改动后重试原任务: "
        f"$D auto-dev --repos {args.repos} --task \"{args.task}\" --reset-worktree"
    )
```

### 5.4.1 Feature 状态承接约束

`auto-dev --feature next` 不引入“current feature”隐式全局状态，而是复用 `next_feature()` 已返回的结构化结果：

- `next_feature()` 返回 `feature.index`、`feature.title`、`feature.task`
- `cmd_auto_dev` 在本次调用内保存 `current_feature["index"]`
- final test 通过后显式执行 `mark_done(index=current_feature["index"])`

这样可以避免把“当前 feature”额外写入 lock 或 plan，减少隐藏耦合。

### 5.4.2 三种调用模式的参数约定

| 模式 | 必需参数 | 典型下一步 |
|------|----------|------------|
| repo mode | `--repos`, `--task` | `submit --repos` |
| workspace mode | `--workspace`, `--task` | `submit --workspace` 或 `release --workspace` |
| feature mode | `--workspace`, `--feature next` | `auto-dev --workspace <ws> --feature next` |

约束：

- `feature mode` 不依赖 `--repos`，所有状态从已有 workspace 和 feature plan 推导
- `workspace mode` 复用已有 workspace，不重新执行 repo clone / acquire
- `repo mode` 仅支持单 repo，允许触发复杂度升级，并在返回值中带出 `workspace`
- `workspace mode` / `feature mode` 下的提示命令不得再引用 `args.repos`
- `workspace mode` / `feature mode` 若 workspace 含多个 repo，必须显式传 `--repo`

### 5.5 submit 命令简化

现有 `submit-pr` 需要 `--workspace`。新增 `submit` 别名，同时支持 `--repos` 和 `--workspace`；成功后默认自动 `release`：

```python
def cmd_submit(args) -> dict:
    """Simplified submit for repo mode and workspace mode."""
    if args.workspace:
        return cmd_submit_pr(args)

    if args.repos:
        # 从 active workspaces 中找到包含目标 repos 的 workspace
        repo_names = [r.strip() for r in args.repos.split(",") if r.strip()]
        ws_name = config.find_active_workspace_by_repos(repo_names)
        if not ws_name:
            return {
                "ok": False,
                "error": f"no active workspace found for repos: {args.repos}",
                "error_code": "NO_WORKSPACE",
                "hint": "先运行 auto-dev 创建 workspace，或用 --workspace 指定",
            }
        if isinstance(ws_name, list):
            return {
                "ok": False,
                "error": f"multiple active workspaces found for repos: {args.repos}",
                "error_code": "AMBIGUOUS_WORKSPACE",
                "hint": "请显式传 --workspace，避免提交到错误分支",
            }
        args.workspace = ws_name
        result = cmd_submit_pr(args)
        if result.get("ok") and not getattr(args, "keep_lock", False):
            cmd_release(argparse.Namespace(workspace=ws_name, all=False, cleanup=False))
        return result

    return {
        "ok": False,
        "error": "either --workspace or --repos is required",
        "error_code": "INVALID_ARGS",
    }
```

### 5.6 status / find 别名

`quick-status` → `status`，`quick-find` → `find`，`quick-test` → `test --repos`。
旧命令保留为别名，新命令名更短更直觉。

### 5.7 返回值规范

所有命令统一返回值结构：

```python
# 成功
{
    "ok": True,
    "data": { ... },
    "next_step": "建议的下一步命令"  # 可选
}

# 失败
{
    "ok": False,
    "error": "人类可读错误描述",
    "error_code": "MACHINE_READABLE_CODE",
    "hint": "具体的恢复操作建议"  # 可选
}

# 复杂度升级
{
    "ok": False,
    "error_code": "TASK_TOO_COMPLEX",
    "data": {"workspace": "env0"},
    "hint": "建议先分析: $D analyze --workspace env0 ..."
}
```

## 6. 实施计划

### Phase 1: auto-dev 核心（高优先）

| 序号 | 改动 | 文件 |
|------|------|------|
| 1.1 | DEVELOP_PROMPT 加入 test_command | `dispatch.py:66-85` |
| 1.2 | 新增 `cmd_auto_dev` | `dispatch.py` |
| 1.3 | 新增 `_looks_complex` / `_resolve_dev_target` / `_resolve_test_command` / `_suggest_next` | `dispatch.py` |
| 1.4 | argparse 注册 `auto-dev` 子命令 | `dispatch.py:980+` |
| 1.5 | 验证 claude/codex engine 在含 test 命令的 prompt 下能正确迭代 | 手动测试 |
| 1.6 | final test 失败时返回 `FINAL_TEST_FAILED`，不视为成功 | `dispatch.py` |
| 1.7 | `--reset-worktree` 复用现有 `GitOps.force_clean()`，不新增危险接口 | `dispatch.py` |
| 1.8 | 新增 target repo 解析，禁止 multi-repo 隐式开发 | `dispatch.py` + `test_runner.py` |

### Phase 2: SKILL.md 简化（高优先）

| 序号 | 改动 | 文件 |
|------|------|------|
| 2.1 | 重写 SKILL.md 为 short 版（6 个命令，含 `release`） | `SKILL.md` |
| 2.2 | 现有完整文档移至 `references/full-command-reference.md` | 新文件 |
| 2.3 | 命令别名 status/find/submit | `dispatch.py` argparse |
| 2.4 | `submit` 默认自动 release，支持 `--keep-lock` | `dispatch.py` argparse + handler |

### Phase 3: 返回值导航（中优先）

| 序号 | 改动 | 文件 |
|------|------|------|
| 3.1 | 所有 cmd_* 返回值加 `next_step` / `hint` | `dispatch.py` 各 handler |
| 3.2 | analyze 返回 complex 时加引导 | `cmd_analyze` |
| 3.3 | auto-dev --feature next 串联 feature 流程 | `cmd_auto_dev` |
| 3.4 | auto-dev 增加 `--allow-complex` override | `dispatch.py` argparse |
| 3.5 | `submit` 同时支持 `--repos` / `--workspace` 两种入口 | `dispatch.py` argparse + handler |

### Phase 4: 基础设施修复（独立）

| 序号 | 改动 | 文件 |
|------|------|------|
| 4.1 | Daemon 启动防护：拒绝从 workspace clone 启动 | CLI start 命令 |
| 4.2 | 从主仓库重启 daemon | 运维操作 |

## 7. 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| Engine 跑测试时改了不该改的测试文件 | 中 | DEVELOP_PROMPT 加 "不要修改测试文件除非测试本身有 bug" |
| _looks_complex 误判标准任务为复杂 | 低 | 启发式保守（需 >=2 个信号词），并提供 `--allow-complex` override |
| 旧命令名 breaking change | 低 | 保留旧名为别名 |
| auto-dev 超时（engine 循环太多次） | 中 | max_turns 限制 + timeout=600s |
| final test 失败导致 agent 误以为已完成 | 中 | 明确返回 `FINAL_TEST_FAILED`，并附 `test_report` + `hint` |
| `--reset-worktree` 误清理用户改动 | 中 | 仅清理当前 workspace 未提交改动；命名显式；默认不启用 |
| multi-repo workspace 被错误当作单 repo 开发 | 高 | `auto-dev` 强制解析单一 `target_repo`，无法唯一确定时直接报错 |
| PR 提交后 workspace 锁遗留 | 高 | `submit` 成功默认自动 `release`；未提交时要求显式 `release` |
| Agent 仍然绕过 dispatch | 中 | CODING_STATS.md Rules（每轮可见）+ SKILL.md 规则双重约束 |
