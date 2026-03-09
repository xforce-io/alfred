# Coding Master v4 优化计划：从"能跑通"到"能证明跑通了"

> **创建时间**: 2026-03-09
> **状态**: Draft
> **前置**: coding-master-v3-design.md（v3.6）
> **核心主题**: 强反馈 · 证据层 · 闭环恢复 · progress 升级

---

## 背景

行业调研（Cursor Swarm / Anthropic C Compiler / OpenAI Harness Engineering）揭示的核心共识：

> Agent 的能力上限 = 验证器的质量。

CM v3 的架构方向（薄编排 + 厚环境 + convention-driven）与行业一致，但反馈层太薄：
- `cm test` 只跑 pytest，不跑 lint/typecheck
- `cm done` 只检查 test_status=passed，不要求结构化证据
- `cm progress` 列状态但不给唯一推荐动作
- 集成失败后缺少精准的故障定位信息

本计划分 3 个 Phase，每个 Phase 独立可交付、向后兼容。

**Compatibility policy**:
- New verification contract applies only after a feature has run v4 `cm test` once.
- Existing v3 sessions remain valid: if a feature has no `evidence/` file yet, `cm done` falls back to the legacy `test_status/test_commit` gate.
- Once `evidence/N-verify.json` exists for a feature, it becomes the source of truth for that feature's verification state.

---

## Phase 1: 强反馈 — 证据层 + verify 升级

> **目标**: `cm done` 从"检查一个 bool"变成"检查一组结构化证据"
> **改动量**: ~200 行新增/修改，0 个新命令

### 1.1 `cm test` 写结构化证据文件

**现状**: `cmd_test` (tools.py:698-751) 把测试结果写到 claims.json 的嵌套字段里，500 字符截断。

**改为**:

```
cm test --feature N 执行后:

1. 跑 lint (ruff check / npm run lint / cargo clippy)  ← 复用 test_runner.py 已有逻辑
2. 跑 typecheck (mypy / tsc --noEmit / 无)             ← 新增检测
3. 跑 test (pytest / npm test / cargo test)             ← 现有逻辑
4. 写 .coding-master/evidence/N-verify.json            ← 新增
5. 写 claims.json (保持现有字段兼容)
```

**evidence/N-verify.json schema**:

```json
{
  "feature_id": "1",
  "created_at": "ISO timestamp",
  "commit": "a1b2c3d",
  "lint": {
    "passed": true,
    "command": "ruff check .",
    "output": "All checks passed. 42 files checked."
  },
  "typecheck": {
    "passed": true,
    "command": "mypy src/",
    "output": "Success: no issues found in 15 source files"
  },
  "test": {
    "passed": true,
    "command": ".venv/bin/pytest tests/",
    "total": 37,
    "passed_count": 37,
    "failed_count": 0,
    "output": "37 passed in 4.2s"
  },
  "overall": "passed"
}
```

**实现变更**:

| 文件 | 变更 |
|------|------|
| `tools.py:_run_tests()` (L352-377) | 重命名为 `_run_verify()`，增加 lint + typecheck 步骤 |
| `tools.py:cmd_test()` (L698-751) | 调用 `_run_verify()`；结果同时写 `evidence/N-verify.json` 和 `claims.json` |
| `tools.py` 顶部常量 | 新增 `EVIDENCE_DIR = "evidence"` |
| `test_runner.py` | 新增 `_resolve_typecheck_command(cwd)` 函数 |

**typecheck 命令检测逻辑** (新增到 test_runner.py):

```python
def _resolve_typecheck_command(cwd: Path) -> str | None:
    if (cwd / "pyproject.toml").exists():
        # 检查是否有 mypy 配置
        if _has_tool(cwd / "pyproject.toml", "mypy"):
            return _resolve_mypy_command(cwd)
        # 检查 mypy 是否可用（venv 或 PATH）
        venv_mypy = cwd / ".venv" / "bin" / "mypy"
        if venv_mypy.is_file():
            return f"{venv_mypy.resolve()} ."
    elif (cwd / "tsconfig.json").exists():
        return "npx tsc --noEmit"
    return None  # 无 typecheck → 跳过，不阻断
```

**向后兼容**:
- `claims.json` 的字段不变，`evidence/` 是纯新增。
- 旧 session 若尚未生成 `evidence/N-verify.json`，仍可按 v3 逻辑 `cm done`。
- 新 session 或旧 session 中已执行过 v4 `cm test` 的 feature，`cm done` 优先读取 evidence。

### 1.2 `cm done` 检查证据文件

**现状**: `cmd_done` (L754-813) 只检查 `test_status == "passed"` 和 `test_commit == HEAD`。

**改为**:

```python
# cmd_done 内的验证逻辑（伪代码）
evidence_path = repo / CM_DIR / EVIDENCE_DIR / f"{fid}-verify.json"

if evidence_path.exists():
    evidence = json.loads(evidence_path.read_text())

    # Hard gate: evidence 的 commit 必须等于当前 HEAD
    if evidence["commit"] != current_head:
        return {"ok": False, "error": "Evidence is stale (code changed after test). Re-run cm test."}

    # Hard gate: overall 必须 passed
    if evidence["overall"] != "passed":
        failed = [k for k in ("lint", "typecheck", "test") if not evidence.get(k, {}).get("passed", True)]
        return {"ok": False, "error": f"Verification failed: {', '.join(failed)}. Fix and re-run cm test."}
else:
    # Legacy fallback for pre-v4 features/sessions
    if test_status == "pending":
        return {"ok": False, "error": "no test record, run cm test first"}
    if test_status == "failed":
        return {"ok": False, "error": "last test failed. Fix and run cm test again"}
    if test_commit != current_head:
        return {"ok": False, "error": "code changed after last test, run cm test again"}
```

**实现变更**:

| 文件 | 变更 |
|------|------|
| `tools.py:cmd_done()` (L754-813) | 优先检查 evidence；无 evidence 时降级到现有 `test_status/test_commit` 检查 |

### 1.3 `cm progress` 输出 `next_action`

**现状**: `_generate_action_steps` (L1052-1080) 返回 steps 列表；`_generate_suggestions` (L1083-1098) 返回建议列表。agent 需要自己判断优先级。

**改为**: progress 输出新增两个字段，区分当前 agent 视角和全局视角：

```json
{
  "ok": true,
  "data": {
    "session_phase": "working",
    "next_action": {
      "command": "cm test --feature 2",
      "reason": "Feature 2 has commits since last test (stale)",
      "worktree": "../alfred-feature-2",
      "scope": "local"
    },
    "session_next_action": {
      "command": "cm claim --feature 4",
      "reason": "Feature 4 is unblocked and unclaimed",
      "scope": "session"
    },
    "features": [...],
    "suggestions": [...]
  }
}
```

定义：
- `next_action`: for current agent 的唯一推荐动作，只会返回当前 agent 已认领的 feature，或当前 agent 可以安全执行的无 owner 动作。
- `session_next_action`: for the session 的全局推荐动作，供 orchestrator / supervisor / 人类查看，不要求当前 agent 一定执行。

**next_action 优先级算法**:

```
Local (`next_action`):
1. 当前 agent 名下有 developing + test_status=failed 的 feature → "fix and re-test"
2. 当前 agent 名下有 developing + test stale 的 feature → "cm test"
3. 当前 agent 名下有 developing + verification passed + HEAD matched 的 feature → "cm done"
4. 当前 agent 名下有 analyzing 的 feature → "write Analysis/Plan, then cm dev"
5. 当前 agent 没有持有中的 feature，且存在 unclaimed + unblocked feature → "cm claim --feature N"
6. 当前 agent 无安全可执行动作 → null

Session (`session_next_action`):
1. 任意 feature verify failed/stale → 推荐对应 owner 修复
2. 有 analyzing feature → 推荐对应 owner 推进
3. 有 pending/unblocked feature → 推荐任一空闲 agent 认领
4. 所有 feature done → "cm integrate"
5. session_phase=integrating → "cm submit"
6. 无可执行动作 → null
```

**实现变更**:

| 文件 | 变更 |
|------|------|
| `tools.py` | 新增 `_compute_next_action(..., agent)` 与 `_compute_session_next_action(...)` |
| `tools.py:cmd_progress()` (L981-1035) | 写入 `data["next_action"]` 和 `data["session_next_action"]` |

### 1.4 SKILL.md 更新

在 Tools 表中更新 `cm test` 的描述：

```
| `$CM test --feature <n>` | Run lint+typecheck+tests → write evidence/N-verify.json + update claims |
```

在 Working Directory 表新增：

```
| `evidence/XX-verify.json` | JSON | lint+typecheck+test 结构化结果 | tools |
```

### Phase 1 测试计划

| 测试 | 验证点 |
|------|--------|
| `test_cm_test_writes_evidence` | cm test 后 evidence/N-verify.json 存在且 schema 正确 |
| `test_cm_done_rejects_stale_evidence` | evidence commit ≠ HEAD 时 cm done 返回 ok=false |
| `test_cm_done_rejects_failed_lint` | lint failed 时 cm done 返回 ok=false |
| `test_cm_test_lint_not_configured_passes` | 无 lint 命令时 lint.passed=true (不阻断) |
| `test_cm_test_typecheck_not_configured_passes` | 无 typecheck 时 typecheck.passed=true |
| `test_progress_next_action_priority` | 各种状态组合下 next_action 返回当前 agent 可安全执行的正确命令 |
| `test_progress_next_action_skips_other_owner` | 其他 agent 持有的 feature 不会出现在当前 agent 的 next_action 中 |
| `test_progress_next_action_null_when_blocked` | 当前 agent 无可执行动作时 next_action=null |
| `test_progress_session_next_action` | session_next_action 能正确反映全局最优推进动作 |
| `test_evidence_backward_compat` | 旧 session（无 evidence/）仍然可以 cm done（降级到 test_status 检查） |

---

## Phase 2: 闭环恢复 — 前置断言 + 集成证据 + reopen 上下文

> **目标**: 失败后能精准恢复，不丢上下文
> **改动量**: ~150 行新增/修改，0 个新命令

### 2.1 命令前置 precondition assert

**问题**: agent 可能在错误的目录、错误的 branch、过期的 lease 下执行命令，导致隐性状态损坏。

**方案**: 在每个 mutation 命令（claim/dev/test/done/integrate/reopen）执行前，跑一组轻量断言。

**边界要说清楚**:
- 这些断言主要防的是 state drift（lease 过期、记录 branch 与实际 branch 不一致、session 已结束）。
- 它们不能证明“调用者当前 shell 就站在正确目录里”，因为命令实现本身并不依赖调用 cwd。
- 因此这里的目标不是“校验人在正确目录”，而是“校验工具操作的目标 worktree / repo 仍与记录一致”。

```python
def _precondition_check(repo: Path, feature_id: str | None = None) -> dict | None:
    """Returns error dict if precondition violated, None if OK."""

    # 1. lease 没过期 (已有 _check_lease，复用)
    lease = _check_lease(repo)
    if not lease["ok"]:
        return lease

    # 2. 目标 worktree 的 git branch 和 claims 记录一致
    if feature_id:
        claims = _atomic_json_read(repo / CM_DIR / "claims.json")
        feat = claims.get("features", {}).get(feature_id, {})
        expected_branch = feat.get("branch")
        if expected_branch:
            actual_branch = _git_current_branch(feat.get("worktree", str(repo)))
            if actual_branch != expected_branch:
                return {"ok": False, "error": f"Branch mismatch: expected {expected_branch}, on {actual_branch}"}

    # 3. .coding-master/ 目录状态一致性快检
    lock = _atomic_json_read(repo / CM_DIR / "lock.json")
    if lock.get("session_phase") == "done":
        return {"ok": False, "error": "Session already done. Start a new session with cm lock."}

    return None  # all good
```

**成本**: 2-3 个 file stat + 1 个 git 命令，< 50ms。

**实现变更**:

| 文件 | 变更 |
|------|------|
| `tools.py` | 新增 `_precondition_check()` 函数 (~25 行) |
| `tools.py:cmd_claim/dev/test/done/integrate/reopen` | 开头加 `err = _precondition_check(repo, fid); if err: return err` |

### 2.2 `cm integrate` 写集成证据

**现状**: `cmd_integrate` (L854-915) 跑测试后只在 lock.json 记 `integration_passed_at`，测试输出截断到 1000 字符。

**改为**: 写 `evidence/integration-report.json`:

```json
{
  "created_at": "ISO timestamp",
  "dev_branch": "dev/alfred-0309-1000",
  "merge_order": ["1", "3", "2", "4"],
  "merge_results": [
    {"feature": "1", "branch": "feat/1-scanner", "status": "merged", "commit": "abc123"},
    {"feature": "3", "branch": "feat/3-parser", "status": "merged", "commit": "def456"},
    {"feature": "2", "branch": "feat/2-lexer", "status": "merged", "commit": "ghi789"},
    {"feature": "4", "branch": "feat/4-codegen", "status": "merged", "commit": "jkl012"}
  ],
  "test": {
    "passed": true,
    "command": ".venv/bin/pytest tests/",
    "total": 142,
    "passed_count": 142,
    "failed_count": 0,
    "output": "142 passed in 12.3s"
  },
  "overall": "passed"
}
```

**集成失败时的报告**:

```json
{
  "overall": "failed",
  "failure_type": "merge_conflict",
  "failed_feature": "2",
  "failed_branch": "feat/2-lexer",
  "error": "CONFLICT (content): Merge conflict in src/lexer.py",
  "merge_results": [
    {"feature": "1", "status": "merged"},
    {"feature": "3", "status": "merged"},
    {"feature": "2", "status": "conflict", "error": "..."}
  ]
}
```

或测试失败时:

```json
{
  "overall": "failed",
  "failure_type": "test_failure",
  "all_merged": true,
  "test": {
    "passed": false,
    "output": "FAILED tests/test_codegen.py::test_emit_x86 - AssertionError..."
  }
}
```

**实现变更**:

| 文件 | 变更 |
|------|------|
| `tools.py:cmd_integrate()` (L854-915) | 收集 merge_results 列表；写 evidence/integration-report.json |

### 2.3 `cm reopen` 携带失败上下文

**现状**: `cm reopen --feature N` 只把 phase 从 done 改回 developing，不携带任何失败信息。

**改为**: reopen 时从 evidence/integration-report.json 提取相关信息，写入返回值：

```json
{
  "ok": true,
  "data": {
    "feature": "2",
    "phase": "developing",
    "failure_context": {
      "type": "merge_conflict",
      "error": "CONFLICT (content): Merge conflict in src/lexer.py",
      "conflicting_with": "feat/3-parser"
    }
  }
}
```

agent 拿到这个上下文后，可以更快定位问题，而不是盲目 debug。

**归因边界**:
- `merge_conflict` 可稳定归因到某个 feature branch。
- `test_failure` 只在能明确映射到单个 feature 时返回 feature 级上下文。
- 若无法可靠归因，`failure_context` 返回 session 级失败摘要，不伪造 `conflicting_with`。

**实现变更**:

| 文件 | 变更 |
|------|------|
| `tools.py:cmd_reopen()` | 读 evidence/integration-report.json，提取该 feature 的失败信息 |

### Phase 2 测试计划

| 测试 | 验证点 |
|------|--------|
| `test_precondition_rejects_expired_lease` | lease 过期时命令被拦截 |
| `test_precondition_rejects_branch_mismatch` | claims 记录与目标 worktree branch 不一致时命令被拦截 |
| `test_precondition_rejects_done_session` | session=done 时命令被拦截 |
| `test_integrate_writes_report` | 集成成功后 evidence/integration-report.json 存在且 schema 正确 |
| `test_integrate_failure_writes_report` | 集成失败后 report 含 failure_type 和错误信息 |
| `test_reopen_carries_failure_context` | reopen 返回值包含 failure_context |
| `test_reopen_without_report_still_works` | 无 integration-report.json 时 reopen 降级正常工作 |

---

## Phase 3: 平台对接 — Contract 定义 + progress 驱动循环

> **目标**: CM 可被外部 workflow 调用，也可被 agent 自主循环驱动
> **改动量**: ~100 行新增，1 个新命令

### 3.1 CM Input/Output Contract

定义 CM 作为 workflow consumer 的标准接口（不改现有代码，只增加入口）：

**新增命令 `cm start`** — 一键启动 session：

```bash
cm start --repo alfred --task "重构 inspector 模块" --plan-file /path/to/plan.md
```

逻辑上等价于:

```
cm lock --repo alfred
cp /path/to/plan.md .coding-master/PLAN.md
cm plan-ready
```

但实现上必须保证**失败原子性**：
- `cm lock` 成功后，若 `plan copy` 或 `plan-ready` 失败，`cm start` 必须 best-effort 回滚。
- 回滚顺序：删除 `.coding-master/PLAN.md`（若由本次创建）→ `cm unlock`。
- `cm start` 不删除已创建的 dev branch；branch 可以复用，避免额外 destructive cleanup。
- 返回值必须显式标注 `rolled_back: true/false`，让调用方知道是否留下半初始化状态。

**session 结束后的标准输出 contract** (`cm submit` 返回值扩展):

```json
{
  "ok": true,
  "data": {
    "pr_url": "https://github.com/user/repo/pull/42",
    "branch": "dev/alfred-0309-1000",
    "evidence_dir": "/path/to/.coding-master/evidence/",
    "features_completed": 4,
    "features_total": 4,
    "exit_status": "success",
    "journal": "/path/to/.coding-master/JOURNAL.md"
  }
}
```

`status` 的派生字段必须有固定优先级，避免“看起来结构化但不可依赖”：

```
blocking_reason priority:
1. expired lease
2. session_phase=integrating and integration-report overall=failed
3. any claimed feature with failed/stale verification
4. all remaining features blocked by dependencies
5. no blocking reason
```

`resume_hint` 必须与 `blocking_reason` 同源推导，不能独立猜测。

**失败退出 contract** (session 未完成时 `cm status` 返回):

```json
{
  "ok": true,
  "data": {
    "exit_status": "partial",
    "features_completed": 2,
    "features_total": 4,
    "blocking_reason": "integration test failure on feature 3",
    "evidence_dir": "/path/to/.coding-master/evidence/",
    "resume_hint": "cm reopen --feature 3, fix, then cm integrate"
  }
}
```

**实现变更**:

| 文件 | 变更 |
|------|------|
| `tools.py` | 新增 `cmd_start()` (~30 行) |
| `tools.py:cmd_submit()` | 扩展返回值，增加 evidence_dir / features count / exit_status |
| `tools.py:cmd_status()` | 增加 exit_status / blocking_reason / resume_hint |
| `SKILL.md` Tools 表 | 新增 `cm start` |

### 3.2 `cm progress` 驱动 agent 自主循环

基于 Phase 1 的 `next_action`，agent 的主循环变成：

```
# Agent 的 SKILL.md 里描述的自主工作模式（convention，不是代码）:
#
# 1. Run cm progress
# 2. If next_action exists: execute next_action.command
# 3. If next_action is null: inspect session_next_action / wait / escalate
# 4. If session_phase=done: stop
# 5. Goto 1
```

这不需要新代码，只需要在 SKILL.md 的 Development Flow 部分增加一段：

```markdown
### Autonomous Mode
After `cm plan-ready`, you can enter an autonomous loop:
1. `$CM progress` → read `next_action`
2. Execute `next_action` if present
3. If `next_action` is null, inspect `session_next_action` for global status
4. Repeat until `session_phase` = `done`

This is the recommended way to work. `cm progress` always knows the best local next step and the best session-level next step.
```

### 3.3 SKILL.md 规则增补

```markdown
## Rules (新增)
11. **Evidence is mandatory after first v4 verify** — once a feature has produced `evidence/N-verify.json`, `cm done` must trust that file
12. **Trust local progress first** — when unsure what to do next, run `cm progress` and follow `next_action`
13. **Do not steal others' work** — `session_next_action` may describe a global need; only act on it if it is owner-safe
```

### Phase 3 测试计划

| 测试 | 验证点 |
|------|--------|
| `test_cm_start_creates_session` | cm start 一步完成 lock + plan copy + plan-ready |
| `test_cm_start_validates_plan` | plan-file 内容不合法时 cm start 报错并释放 lock，返回 rollback 状态 |
| `test_cm_submit_returns_contract` | submit 返回值包含 pr_url, evidence_dir, exit_status |
| `test_cm_status_partial` | 未完成 session 的 status 返回 partial + resume_hint |
| `test_cm_status_blocking_reason_priority` | 多个阻塞条件并存时按固定优先级返回 blocking_reason |

---

## 实施顺序与依赖

```
Phase 1 (强反馈)           Phase 2 (闭环恢复)         Phase 3 (平台对接)
┌─────────────────┐      ┌──────────────────┐      ┌──────────────────┐
│ 1.1 evidence/   │      │ 2.1 precondition │      │ 3.1 cm start     │
│     N-verify    │─┐    │     assert       │      │     + contract   │
│                 │ │    │                  │      │                  │
│ 1.2 cm done     │◄┘    │ 2.2 integration  │      │ 3.2 autonomous   │
│     gate check  │      │     report       │─┐    │     mode doc     │
│                 │      │                  │ │    │                  │
│ 1.3 progress    │      │ 2.3 cm reopen    │◄┘    │ 3.3 rules update │
│     next_action │      │     with context │      │                  │
│                 │      │                  │      │                  │
│ 1.4 SKILL.md    │      │                  │      │                  │
└─────────────────┘      └──────────────────┘      └──────────────────┘
       ▲                         ▲                         ▲
       │                         │                         │
   可独立交付              依赖 Phase 1              依赖 Phase 1+2
                        (evidence/ 目录)          (evidence + report)
```

## 不做的事情（显式排除）

| 排除项 | 原因 |
|--------|------|
| 新增 `cm review` 命令 | 判断反馈现阶段靠 agent 自驱即可，不需要工具化 |
| 多 agent 角色分化 (Planner/Worker/Judge) | 当前规模不需要，增加复杂度 |
| UI screenshot / DOM diff evidence | 不是前端项目，无实际需求 |
| 与 phase_runner.py 直接集成 | 先定义 contract，不急着耦合 |
| 新增 `cm verify` 独立命令 | verify 逻辑合并到 `cm test` 里，不增加命令数 |
| evidence/ 目录预创建空结构 | 按需生成，避免空壳 |

## 代码量估算

| Phase | 新增/修改行数 | 新增文件 | 新增命令 |
|-------|-------------|---------|---------|
| Phase 1 | ~200 行 | 0 | 0 |
| Phase 2 | ~150 行 | 0 | 0 |
| Phase 3 | ~100 行 | 0 | 1 (`cm start`) |
| **合计** | **~450 行** | **0** | **1** |

工具代码从 ~1335 行 → ~1785 行，仍远低于 v2 的 4800 行。
新增的全是证据写入和状态检查逻辑，零编排代码。
