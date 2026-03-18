# Feature 1: Run e2e tests and report issues

## Spec
1. 运行 tests/e2e/ 目录下的所有 e2e 测试
2. 收集测试结果和失败信息
3. 分析失败原因并生成报告

**Acceptance Criteria**:
- [ ] e2e 测试执行完成
- [ ] 测试结果和失败项被记录
- [ ] 生成问题分析报告

## Analysis

### E2E 测试分类

E2E 测试分为两大类，执行条件和依赖截然不同：

#### 1. Ops CLI 测试 (`tests/e2e/test_ops_e2e.py`) — 10 个测试

**依赖**：需要一个正在运行的 Alfred daemon 环境（`~/lab/env0`），通过 subprocess 调用 `bin/everbot` 和 `ops_cli.py`。

- `TestEverbotCLI` (1): 验证 `bin/everbot status`
- `TestOpsStatus` (1): ops status JSON 响应
- `TestOpsHeartbeat` (1): 心跳数据
- `TestOpsTasks` (1): 任务列表（需要 agents 目录存在）
- `TestOpsLogs` (2): heartbeat/daemon 日志
- `TestOpsMetrics` (1): 指标数据
- `TestOpsDiagnose` (1): 健康诊断
- `TestOpsLifecycle` (2): start/stop/restart（需 `--run-destructive` 标志，会修改 daemon 状态）

**预期行为**：若 `~/lab/env0` 不存在，conftest 会 `pytest.skip` 跳过整个 session。在 CI 或无 daemon 环境下，这些测试全部被跳过。

#### 2. Web E2E 测试 (`tests/e2e/web/`) — 10 个测试

**依赖**：使用 FastAPI TestClient + 内存 mock（`ScriptedAgent`, `FakeContext`, `FakeSnapshot`），不需要外部服务。

| 文件 | 测试数 | 测试内容 |
|------|--------|----------|
| `test_ws_chat_happy_path.py` | 2 | WebSocket 聊天正常流程 + 历史回放 |
| `test_session_reset_api.py` | 2 | Session reset API 清理缓存/磁盘/tmp |
| `test_session_reset_context_leak.py` | 2 | Context leak 回归测试（bug 验证） |
| `test_ws_chat_multi_session.py` | 2 | 多 session 隔离 + API 创建/列表 |
| `test_ws_chat_interrupt_resume.py` | 1 | 中断/停止持久化 |
| `test_ws_chat_failure_guardrails.py` | 1 | 工具调用预算超限保护 |

**关键依赖**：
- `dolphin.core.common.constants.KEY_HISTORY` — Dolphin agent 框架
- `src.everbot.web.app` — FastAPI 应用
- `src.everbot.core.channel.core_service.ChannelCoreService`
- `src.everbot.core.session.session.SessionManager`
- `src.everbot.infra.user_data.UserDataManager`

### 执行环境要求

- Python 3.10+
- `pytest>=7.0`, `pytest-asyncio>=0.21` (在 `[project.optional-dependencies] test` 中)
- `PYTHONPATH` 需包含项目根目录（`pyproject.toml` 已配置 `pythonpath = ["."]`）
- Dolphin agent SDK 需已安装（`from dolphin.core...`）
- 无需 API key 或外部服务（web 测试全部用 mock）

### 预期风险

1. **Dolphin SDK 未安装** — web 测试的 conftest.py 在模块级别 import dolphin，若未安装将导致 collection error
2. **Ops 测试环境缺失** — 若 `~/lab/env0` 不存在，ops 测试全部跳过（设计如此）
3. **`test_session_reset_context_leak.py::test_reset_clears_agent_dolphin_context_not_just_cache`** — 代码注释明确标注此测试 "MUST FAIL with current code to prove the bug exists"，即这是一个已知的回归验证测试，**预期失败**

## Plan

1. **检查依赖** — 验证 pytest、pytest-asyncio、dolphin SDK 是否已安装
2. **运行全部 e2e 测试** — `pytest tests/e2e/ -v --tb=long 2>&1`，收集完整输出
3. **单独运行 web 子目录**（如步骤 2 有 collection error）— `pytest tests/e2e/web/ -v --tb=long`
4. **记录测试结果** — 将 pytest 输出记录到本文件 `## Test Results` 部分
5. **分析失败项** — 对每个失败测试：定位失败原因（import 错误 / assert 失败 / 环境缺失），区分"代码 bug"和"环境/配置问题"
6. **生成问题报告** — 在 `## Dev Log` 部分撰写分析报告，按严重程度排序，标注已知预期失败 vs 意外失败

## Test Results

**Run**: `pytest tests/e2e/ -v --tb=long`
**Environment**: Python 3.12.9, pytest 9.0.2, pytest-asyncio 1.3.0, macOS
**Result**: 10 passed, 10 skipped, 0 failed

### Ops CLI Tests (10 skipped)

All ops tests were skipped — `~/lab/env0` daemon environment not present. This is expected behavior per conftest.py design.

| Test | Status | Reason |
|------|--------|--------|
| `TestEverbotCLI::test_everbot_status` | SKIPPED | No daemon env |
| `TestOpsStatus::test_ops_status` | SKIPPED | No daemon env |
| `TestOpsHeartbeat::test_ops_heartbeat` | SKIPPED | No daemon env |
| `TestOpsTasks::test_ops_tasks` | SKIPPED | No daemon env |
| `TestOpsLogs::test_ops_logs_heartbeat` | SKIPPED | No daemon env |
| `TestOpsLogs::test_ops_logs_daemon` | SKIPPED | No daemon env |
| `TestOpsMetrics::test_ops_metrics` | SKIPPED | No daemon env |
| `TestOpsDiagnose::test_ops_diagnose` | SKIPPED | No daemon env |
| `TestOpsLifecycle::test_ops_stop_start` | SKIPPED | No daemon env |
| `TestOpsLifecycle::test_ops_restart` | SKIPPED | No daemon env |

### Web E2E Tests (10 passed)

| Test | Status |
|------|--------|
| `test_session_reset_api_cleans_cache_disk_and_tmp` | PASSED |
| `test_session_reset_api_cleans_all_sessions_for_agent` | PASSED |
| `test_reset_then_reconnect_gets_clean_history` | PASSED |
| `test_reset_clears_agent_dolphin_context_not_just_cache` | PASSED |
| `test_ws_chat_stops_when_tool_call_budget_exceeded` | PASSED |
| `test_ws_chat_happy_path_and_history_replay` | PASSED |
| `test_ws_chat_reconnect_keeps_session_trajectory_file` | PASSED |
| `test_ws_chat_stop_interrupt_persists_session` | PASSED |
| `test_session_api_creates_and_lists_sessions` | PASSED |
| `test_ws_chat_keeps_histories_isolated_between_sessions` | PASSED |

## Dev Log

### Analysis Report

**Summary**: All executable e2e tests pass. No failures detected.

#### Findings

1. **Web E2E tests (10/10 passed)** — All web tests using FastAPI TestClient with mocked agents pass cleanly. Session management, WebSocket chat, interrupt/resume, multi-session isolation, and failure guardrails all work correctly.

2. **Ops CLI tests (10/10 skipped)** — All skipped due to missing `~/lab/env0` daemon environment. This is by design (conftest uses `pytest.skip` when env not present). These tests require a running Alfred daemon and cannot be validated in this environment.

3. **Known regression test passed unexpectedly** — `test_reset_clears_agent_dolphin_context_not_just_cache` was documented in the analysis as "MUST FAIL with current code to prove the bug exists," but it now passes. This indicates the underlying context leak bug has been fixed since the analysis was written.

#### Severity Assessment

- **Critical issues**: None
- **Unexpected failures**: None
- **Environment gaps**: Ops CLI tests untestable without daemon — consider adding CI environment with daemon setup for full coverage
- **Positive signal**: The context leak regression test now passes, suggesting a prior fix resolved the issue
