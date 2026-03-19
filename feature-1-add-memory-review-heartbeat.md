# Feature 1: 添加 memory_review 心跳任务

## Spec
修改 HEARTBEAT.md，在"待办"部分添加 memory_review 任务配置，使 heartbeat 能每 2 小时调度执行一次记忆审查。

**Acceptance Criteria**:
- [ ] HEARTBEAT.md 的"待办"部分包含 memory_review 任务
- [ ] 任务配置格式符合 cron.py 的 ALLOWED_SKILLS 要求

## Analysis

### HEARTBEAT.md 结构

HEARTBEAT.md 是 agent workspace 下的任务清单文件（路径：`<workspace>/HEARTBEAT.md`）。它包含两部分：

1. **Markdown 部分**：人类可读的标题和章节（`# HEARTBEAT`、`## Tasks` 等），其中"待办"（`## 待办`）是用户侧文档中展示的章节名。
2. **JSON 任务块**：嵌在 Markdown 中的 ` ```json ... ``` ` 围栏块，是 CronExecutor 实际解析和调度的结构化数据。格式为 `{"version": 2, "tasks": [...]}`。

`RoutineManager` 的默认内容模板为 `"# HEARTBEAT\n\n## Tasks\n\n"`（`routine_manager.py:62`）。

### 任务数据模型

`Task` dataclass（`task_manager.py:33-53`）定义了任务的所有字段。与 skill 任务相关的关键字段：

| 字段 | 类型 | 用途 |
|------|------|------|
| `id` | str | 任务唯一 ID |
| `title` | str | 任务标题 |
| `schedule` | str | 调度表达式（cron 表达式或间隔字符串如 `"2h"`） |
| `skill` | str \| None | 技能名称（如 `"memory-review"`），使用连字符 |
| `scanner` | str \| None | 可选的 scanner gate 类型（如 `"session"`） |
| `execution_mode` | str | `"inline"` 或 `"isolated"` |
| `timeout_seconds` | int | 超时秒数 |
| `source` | str | 来源（`"system"` / `"manual"`） |
| `min_execution_interval` | str \| None | 可选的最小执行间隔 |

### Skill 白名单机制

`cron.py:32-36` 定义了 `ALLOWED_SKILLS` 白名单：

```python
ALLOWED_SKILLS: frozenset[str] = frozenset({
    "health_check",
    "memory_review",
    "task_discover",
})
```

`_invoke_skill`（`cron.py:555`）在执行时将 skill 名称中的连字符转换为下划线：`module_name = skill_name.replace("-", "_")`，然后校验 `module_name in ALLOWED_SKILLS`。

因此：任务 JSON 中 `skill` 字段使用连字符格式 `"memory-review"`，运行时转换为 `"memory_review"` 后匹配白名单，并 import `everbot.core.jobs.memory_review` 模块。

### 已有参考配置

`docs/evolving_design.md:515-522` 提供了 memory_review 的标准任务配置：

```json
{
  "id": "reflection_memory_review",
  "title": "记忆整合优化",
  "schedule": "2h",
  "skill": "memory-review",
  "execution_mode": "inline",
  "timeout_seconds": 120,
  "source": "system"
}
```

测试中也使用了相同的配置格式（`tests/unit/test_self_reflection.py:333-338`）。

### memory_review 技能模块

`src/everbot/core/jobs/memory_review.py` 实现了记忆审查功能：
- 使用 `SessionScanner` 扫描可回顾的 session
- 使用 `ReflectionState` 管理 watermark
- 支持通过 `scan_result` 复用 gate 预检结果
- 功能：整合现有记忆条目，压缩到 USER.md profile

### HEARTBEAT.md 当前状态

当前 workspace 下不存在 HEARTBEAT.md 文件。需要创建一个包含 memory_review 任务的新文件。

### "待办"部分的含义

根据 `src/everbot/README.md:61` 的示例，用户文档中使用 `## 待办` 作为章节名来描述待执行任务。但实际代码中，`RoutineManager.DEFAULT_CONTENT` 使用 `## Tasks`，且 JSON 解析器 (`parse_heartbeat_md`) 只关心 ` ```json ``` ` 围栏块的内容，不依赖章节标题名称。

因此"在待办部分添加"意味着：在 HEARTBEAT.md 中添加一个包含 memory_review 任务的 JSON 任务块。

## Plan

1. **创建 HEARTBEAT.md** — 在 workspace 根目录创建 `HEARTBEAT.md`，包含：
   - 标题 `# HEARTBEAT`
   - `## 待办` 章节
   - JSON 任务块，包含 memory_review 任务配置：
     ```json
     {
       "version": 2,
       "tasks": [
         {
           "id": "reflection_memory_review",
           "title": "记忆整合优化",
           "schedule": "2h",
           "skill": "memory-review",
           "execution_mode": "inline",
           "timeout_seconds": 120,
           "source": "system"
         }
       ]
     }
     ```
2. **验证** — 确认 JSON 中 `skill: "memory-review"` 经 `replace("-", "_")` 转换后为 `"memory_review"`，匹配 `ALLOWED_SKILLS` 白名单。确认 `schedule: "2h"` 表示每 2 小时调度一次。

## Test Results

## Dev Log
