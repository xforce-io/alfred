# Phantom Tool Guard — 未注册工具拦截

## 问题

当 resource skill 的 SKILL.md 描述了虚拟工具名（如 `_cm_next`），模型会尝试调用这些工具。但这些工具未在 Dolphin GlobalSkills 中注册，调用会失败。当前框架没有有效拦截机制：

1. Dolphin SDK 执行失败后返回的 tool result 内容异常（reasoning_content 泄露，已提给 Dolphin 团队）
2. Alfred 的 intent 去重被微小参数差异绕过
3. 模型无法从异常的 tool result 中识别出"工具不存在"，持续重试

实际案例：demo_agent 调用 `_cm_next` 9 次，全部失败，浪费整个 turn。

## 设计

### 核心机制

在 TurnOrchestrator 的事件流监听中，增加一个 **phantom tool guard**：当模型调用的 tool_name 不在 Dolphin 已注册工具列表中时，注入纠正提示并在超限后终止 turn。

### 改动范围

仅 Alfred 框架层，不涉及 Dolphin SDK。

### 接口

`TurnOrchestrator.__init__` 新增参数：

```python
get_registered_tools: Optional[Callable[[], set[str]]] = None
```

- 回调函数，每次调用返回当前已注册的工具名集合
- 实时查询而非静态快照，支持运行时动态注册的工具
- 为 None 时 guard 不生效（向后兼容）

`TurnPolicy` 新增参数：

```python
max_phantom_tool_calls: int = 1
```

- 同一未注册工具名允许的最大调用次数
- 默认 1：第 1 次放行（Dolphin 已在执行，无法阻止），tool_output 阶段注入纠正提示；第 2 次在 tool_call 阶段直接 TURN_ERROR

### 拦截流程

```
stage == "tool_call":
    if get_registered_tools is None:
        # 未传回调，跳过检查
        pass
    else:
        registered = get_registered_tools()
        if t_name not in registered:
            phantom_tool_counts[t_name] += 1
            if phantom_tool_counts[t_name] > policy.max_phantom_tool_calls:
                yield TURN_ERROR(
                    "PHANTOM_TOOL: tool `{t_name}` is not registered, "
                    "called {count} times, limit={limit}"
                )
                return
            else:
                # 标记此 pid，在 tool_output 阶段注入纠正提示
                phantom_pids.add(pid)

stage == "tool_output":
    if pid in phantom_pids:
        # 在原始 output 末尾追加纠正提示
        t_output_raw += (
            "\n⚠️ Tool `{t_name}` is not a registered tool and cannot be called. "
            "Please use registered tools (_bash, _python, _grep, etc.) to complete the task."
        )
```

### 调用链

```
TurnExecutor.stream_turn()
  → agent = await get_or_create_agent(session)
  → get_tools = lambda: set(agent.get_skillkit_raw().getSkillNames())
  → orchestrator = TurnOrchestrator(policy, get_registered_tools=get_tools)
  → orchestrator.run_turn(agent, message, ...)
```

### 状态跟踪

`_run_attempt()` 内新增两个局部变量：

```python
phantom_tool_counts: dict[str, int] = {}  # tool_name → 调用次数
phantom_pids: set[str] = set()            # 需要注入纠正提示的 pid
```

### 文件清单

| 文件 | 改动 |
|------|------|
| `src/everbot/core/runtime/turn_policy.py` | 新增 `max_phantom_tool_calls: int = 1` |
| `src/everbot/core/runtime/turn_orchestrator.py` | `__init__` 接收回调；`_run_attempt` 增加 phantom tool 检查逻辑 |
| `src/everbot/core/runtime/turn_executor.py` | 构造回调，传给 orchestrator |
| `tests/unit/test_turn_orchestrator.py` | 新增测试用例 |

### 测试用例

1. **未注册工具第 1 次调用** → tool_output 包含纠正提示，turn 继续
2. **未注册工具第 2 次调用** → TURN_ERROR("PHANTOM_TOOL: ...")
3. **动态注册后调用** → 第 1 次未注册被纠正，动态注册后第 2 次正常通过
4. **不传回调** → guard 不生效，行为与当前一致
5. **已注册工具调用** → 不受影响

### 不做的事

- 不解析 SKILL.md 提取虚拟工具名（脆弱、过度工程）
- 不修改 Dolphin SDK（由 Dolphin 团队处理 reasoning 泄露 bug）
- 不做模糊匹配或相似度检测（准确率问题）
