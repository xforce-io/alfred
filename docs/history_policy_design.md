# History Policy：统一会话历史管理

## 1. 背景与问题

### 1.1 现状

Alfred 通过 Dolphin SDK 的 `portable_session` 协议与 LLM agent 交互。Dolphin 是通用 SDK，不为 Alfred 定制 context 策略，因此 Alfred 必须在调用 `import_portable_session` 之前，自行确保 `history_messages` 是合理的。

当前历史消息的写入和读出有三条独立路径，各自有独立的策略：

```
路径 1: inject_history_message (心跳注入)
  写入方式: update_atomic → 直接 append
  容量保护: 无
  压缩: 无
  频率: 高（每 2 分钟一次心跳任务结果）

路径 2: save_session (用户交互后保存)
  写入方式: export_portable_session → compress → atomic_save
  容量保护: COMPRESS_THRESHOLD=80, WINDOW_SIZE=60
  压缩: LLM 摘要（SessionCompressor）
  频率: 低（用户主动交互时）

路径 3: restore_to_agent (恢复到 Dolphin)
  读出方式: load → filter_heartbeat → compact → import_portable_session
  容量保护: MAX_RESTORED_HISTORY_MESSAGES=120
  过滤: 字符串匹配去除心跳消息
  频率: 每次处理消息前
```

### 1.2 核心问题

**1. 高频写入路径无保护**

`inject_history_message` 是心跳系统注入结果的唯一路径，每次例行任务完成后调用。该路径直接 append 到 `history_messages`，不触发任何压缩或裁剪逻辑。

实际案例：`web_session_demo_agent` 积累了 85 条消息，其中约 80 条是心跳注入的轨迹检测结果，但压缩从未触发——因为用户几乎不通过 web 交互。

**2. 心跳堆积触发不必要的压缩**

心跳注入（高频、低价值）和用户对话共享同一个 `history_messages`，心跳消息的堆积会把整个会话推过压缩阈值，逼着用户主对话历史一起被 LLM 摘要重写。这是策略边界没有隔离的问题。

**3. 心跳消息靠 content 前缀识别**

注入时加 `"[此消息由心跳系统自动执行例行任务生成]\n\n"` 前缀，过滤时靠字符串匹配。脆弱且语义不明确。

## 2. 设计方案

核心思路：**隔离心跳 + 标准 compact**。

业界主流的 context 管理就是到了一定 size 做 compact（LLM 摘要），没必要过度设计。Alfred 的特殊性在于高频异步注入（heartbeat）会污染主对话的 compact 决策，所以需要做的就是把心跳隔离出来，其余走标准 compact。

### 2.1 inject：限制心跳数量

`inject_history_message` 在 append 新消息后，检查心跳消息数量。超过上限（默认 20 条）时，淘汰最老的心跳消息及其占位消息（`(acknowledged)` + `[Background notification follows]`）。

```python
MAX_HEARTBEAT_MESSAGES = 20

def _evict_oldest_heartbeat(history: list[dict]) -> list[dict]:
    """淘汰最老的心跳消息及其占位消息。

    算法：两遍扫描。
    第一遍：找到需要淘汰的心跳消息，收集其 run_id（新格式）或 index（旧格式）。
    第二遍：过滤掉这些心跳消息 + 绑定的占位消息。
    """
    heartbeat_indices = [i for i, m in enumerate(history) if _is_heartbeat(m)]
    if len(heartbeat_indices) <= MAX_HEARTBEAT_MESSAGES:
        return history

    # 需要淘汰的心跳消息 index（最老的 N 条）
    to_evict = set(heartbeat_indices[:len(heartbeat_indices) - MAX_HEARTBEAT_MESSAGES])

    # 收集被淘汰心跳的 run_id（用于匹配新格式占位消息）
    evict_run_ids = set()
    for idx in to_evict:
        meta = history[idx].get("metadata")
        if isinstance(meta, dict) and meta.get("run_id"):
            evict_run_ids.add(meta["run_id"])

    # 过滤：删除被淘汰的心跳 + 其绑定的占位消息
    result = []
    for i, msg in enumerate(history):
        if i in to_evict:
            continue  # 心跳本体

        # 新格式占位：metadata.run_id 在淘汰集中
        meta = msg.get("metadata")
        if isinstance(meta, dict) and meta.get("run_id") in evict_run_ids:
            if meta.get("category") == "placeholder":
                continue

        # 旧格式占位：紧接在被淘汰心跳之前的 (acknowledged) / [Background notification follows]
        if _is_placeholder(msg) and (i + 1) in to_evict:
            continue
        if _is_placeholder(msg) and (i + 2) in to_evict:
            # (acknowledged) 在心跳前两个位置
            if i + 1 < len(history) and _is_placeholder(history[i + 1]):
                continue

        result.append(msg)
    return result
```

### 2.2 save：标准 compact（排除心跳计数）

`save_session` 时，只看用户对话消息数量决定是否触发 compact。触发后，**先过滤心跳再 compact，再把心跳追加到尾部**。

不拆分后 merge 的原因：compact 会把旧 chat 重写为 summary pair + window，原始位置信息丢失，中间插入的心跳消息没有明确的归位规则。既然心跳在 restore 时会被全部过滤掉（不送 LLM），它在 disk 上的位置只影响审计可读性，不影响正确性。追加到尾部是最简实现。

```python
# save_session 中
chat_count = sum(1 for m in history if not _is_heartbeat(m) and not _is_placeholder(m))
if chat_count > COMPRESS_THRESHOLD:
    # 分离心跳
    heartbeat_msgs = [m for m in history if _is_heartbeat(m) or _is_placeholder(m)]
    chat_msgs = [m for m in history if not _is_heartbeat(m) and not _is_placeholder(m)]
    # 只对 chat 部分 compact
    compressed, new_chat = await compressor.maybe_compress(chat_msgs)
    if compressed:
        # 心跳追加到尾部（disk 审计用，restore 时会被过滤）
        history = new_chat + heartbeat_msgs
    else:
        history = chat_msgs + heartbeat_msgs
```

心跳消息保留在 disk 上（可审计），不参与 compact 决策，不参与 compact 内容。

### 2.3 restore：过滤心跳 + 现有 compact

`restore_to_agent` 时，先过滤心跳消息（含占位消息），再走现有的 `DolphinStateAdapter.compact_session_state` 做截断和边界修复。逻辑与现在基本一致，只是心跳识别改用 metadata。

```python
# restore_to_agent 中
history = self._filter_empty_assistant_messages(history)
history = self._filter_heartbeat_messages(history)  # 改用 metadata 识别
history = DolphinStateAdapter.compact_session_state(history, max_messages=120)
```

### 2.4 心跳消息结构化 metadata

**当前**：靠 content 前缀 `"[此消息由心跳系统自动执行例行任务生成]"` 识别。占位消息无标识。

**改进**：统一用 `metadata` 字段：

```python
# 心跳消息
{"role": "assistant", "content": "...",
 "metadata": {"source": "heartbeat", "run_id": "hb_xxx", "task_id": "routine_xxx"}}

# 占位消息（与心跳消息共享 run_id）
{"role": "assistant", "content": "(acknowledged)",
 "metadata": {"source": "system", "category": "placeholder", "run_id": "hb_xxx"}}
{"role": "user", "content": "[Background notification follows]",
 "metadata": {"source": "system", "category": "placeholder", "run_id": "hb_xxx"}}
```

识别逻辑（向后兼容）：

```python
def _is_heartbeat(msg: dict) -> bool:
    """识别心跳消息（新格式 metadata + 旧格式 content 前缀）。"""
    meta = msg.get("metadata")
    if isinstance(meta, dict) and meta.get("source") == "heartbeat":
        return True
    content = msg.get("content") or ""
    return isinstance(content, str) and content.startswith("[此消息由心跳系统自动执行例行任务生成]")

def _is_placeholder(msg: dict) -> bool:
    """识别占位消息（新格式 metadata + 旧格式 content 精确匹配）。

    旧数据中占位消息没有 metadata，只能靠 content 精确匹配。
    这与当前 persistence.py:147-154 的行为一致。
    """
    meta = msg.get("metadata")
    if isinstance(meta, dict) and meta.get("category") == "placeholder":
        return True
    # 向后兼容：旧占位消息靠 content 精确匹配
    _PLACEHOLDER_CONTENTS = {"(acknowledged)", "[Background notification follows]"}
    content = msg.get("content")
    return isinstance(content, str) and content in _PLACEHOLDER_CONTENTS
```

## 3. 调用点改动

### inject_history_message (session_mailbox.py)

```python
# append 之后加一行
session_data.history_messages = _evict_oldest_heartbeat(session_data.history_messages)
```

### save_session (session.py)

```python
# Before: 直接 compact 全量 history
compressed, new_history = await compressor.maybe_compress(serializable_history)

# After: 计数排除心跳，compact 排除心跳，心跳追加尾部
chat_count = sum(1 for m in serializable_history
                 if not _is_heartbeat(m) and not _is_placeholder(m))
if chat_count > COMPRESS_THRESHOLD:
    heartbeat_msgs = [m for m in serializable_history
                      if _is_heartbeat(m) or _is_placeholder(m)]
    chat_msgs = [m for m in serializable_history
                 if not _is_heartbeat(m) and not _is_placeholder(m)]
    compressed, new_chat = await compressor.maybe_compress(chat_msgs)
    if compressed:
        serializable_history = new_chat + heartbeat_msgs
```

### restore_to_agent (persistence.py)

```python
# 改用 metadata 识别心跳，逻辑不变
history = [m for m in history if not _is_heartbeat(m) and not _is_placeholder(m)]
history = DolphinStateAdapter.compact_session_state(history, max_messages=120)
```

## 4. 文件清单

| 操作 | 文件 | 说明 |
|------|------|------|
| 改动 | `src/everbot/core/session/session_mailbox.py` | inject 后加心跳数量限制 |
| 改动 | `src/everbot/core/session/session.py` | save 时排除心跳再判断 compact |
| 改动 | `src/everbot/core/session/persistence.py` | restore 时用 metadata 过滤心跳 |
| 改动 | `src/everbot/core/runtime/cron_delivery.py` | 注入消息用结构化 metadata |
| 改动 | `src/everbot/core/runtime/heartbeat.py` | 同上 |
| 保留 | `src/everbot/infra/dolphin_state_adapter.py` | restore 继续复用其 compact 逻辑 |
| 新增 | `tests/unit/test_history_policy.py` | 单元测试 |

## 5. 迁移策略

### Phase 1：隔离心跳（立即止血）

- `inject_history_message` 加心跳消息上限（20 条），超限淘汰最老的（连带占位消息）
- 心跳注入改用结构化 metadata（保留向后兼容的 content 前缀识别）
- `save_session` compact 决策排除心跳消息计数
- `restore_to_agent` 过滤改用 metadata 识别

预期效果：心跳不再推动主对话触发压缩，消除最大的问题源。

### Phase 2：token 预算驱动（可选，未来）

- 引入轻量 token 计数（tiktoken 或字符数估算）
- compact 阈值改为 token 数（如 `compress_token_budget=60000`）
- 心跳消息按 task_id 去重（每个任务只保留最新一条结果）

## 6. 测试方案

测试文件：`tests/unit/test_history_policy.py`

### 6.1 测试辅助工厂

```python
def _user(content, **meta):
    msg = {"role": "user", "content": content}
    if meta: msg["metadata"] = meta
    return msg

def _assistant(content, **meta):
    msg = {"role": "assistant", "content": content}
    if meta: msg["metadata"] = meta
    return msg

def _heartbeat_msg(run_id, content="检测正常"):
    return _assistant(content, source="heartbeat", run_id=run_id)

def _placeholder_ack(run_id):
    return _assistant("(acknowledged)", source="system", category="placeholder", run_id=run_id)

def _placeholder_bg(run_id):
    return _user("[Background notification follows]", source="system", category="placeholder", run_id=run_id)

def _heartbeat_turn(run_id, content="检测正常"):
    """一个完整的心跳 turn：占位 + 占位 + 心跳消息。"""
    return [_placeholder_ack(run_id), _placeholder_bg(run_id), _heartbeat_msg(run_id, content)]

def _legacy_heartbeat(content="结果正常"):
    """旧格式心跳消息（content 前缀识别）。"""
    return _assistant(f"[此消息由心跳系统自动执行例行任务生成]\n\n{content}")
```

### 6.2 心跳识别测试（`class TestIsHeartbeat`）

| 测试 | 输入 | 预期 |
|------|------|------|
| `test_metadata_heartbeat` | `metadata.source == "heartbeat"` | True |
| `test_legacy_prefix` | content 以 `[此消息由心跳系统自动执行例行任务生成]` 开头 | True |
| `test_normal_assistant` | 普通 assistant 消息 | False |
| `test_placeholder_not_heartbeat` | `metadata.source == "system"` | False |
| `test_no_metadata` | 无 metadata 字段 | False |

### 6.2b 占位消息识别测试（`class TestIsPlaceholder`）

| 测试 | 输入 | 预期 |
|------|------|------|
| `test_metadata_placeholder` | `metadata.category == "placeholder"` | True |
| `test_legacy_acknowledged` | 裸 `{"role": "assistant", "content": "(acknowledged)"}` 无 metadata | True |
| `test_legacy_bg_notification` | 裸 `{"role": "user", "content": "[Background notification follows]"}` 无 metadata | True |
| `test_normal_user_message` | `{"role": "user", "content": "hello"}` | False |
| `test_partial_match_not_placeholder` | content 是 `"(acknowledged) and more"` | False |

### 6.3 心跳淘汰测试（`class TestEvictOldestHeartbeat`）

| # | 测试 | 场景 | 验证 |
|---|------|------|------|
| 1 | `test_under_limit_no_change` | 5 条心跳，上限 20 | history 不变 |
| 2 | `test_evict_oldest_fifo` | 25 条心跳，上限 20 | 保留最新 20 条，最老 5 条淘汰 |
| 3 | `test_placeholder_evicted_with_heartbeat_new_format` | 25 个心跳 turn（新格式，含 run_id 占位），上限 20 | 通过 run_id 匹配，淘汰心跳连带占位一起删，无孤立占位 |
| 3b | `test_placeholder_evicted_with_heartbeat_legacy` | 25 个心跳 turn（旧格式，裸占位），上限 20 | 通过位置启发式，淘汰心跳连带前面的占位一起删，无孤立占位 |
| 3c | `test_placeholder_evicted_mixed_format` | 混合新旧格式心跳 turn | 两种格式的占位都被正确清理 |
| 4 | `test_chat_messages_preserved` | 25 条心跳 + 10 条 user_chat | 心跳淘汰到 20，chat 全部保留 |
| 5 | `test_legacy_heartbeat_evicted` | 混合新旧格式心跳 | 旧格式也能被识别和淘汰 |
| 6 | `test_empty_history` | `[]` | `[]` |
| 7 | `test_no_heartbeat` | 全是 user_chat | 不变 |
| 8 | `test_interleaved_order_preserved` | 心跳和 chat 交替排列 | 淘汰后剩余消息相对顺序不变 |

### 6.4 save compact 隔离测试（`class TestSaveCompact`）

```
test_chat_below_threshold_no_compact
  - 20 条 chat + 30 条心跳 → 总量 50 超旧阈值，但 chat 只有 20 条
  - compressor.maybe_compress 未被调用
  → 验证心跳不影响 compact 决策

test_chat_above_threshold_triggers_compact
  - 90 条 chat + 10 条心跳
  - compressor.maybe_compress 被调用，且只传入 chat 部分
  → 验证只对 chat 做 compact

test_heartbeat_preserved_on_disk_after_compact
  - compact 触发后
  - 最终 history 仍包含心跳消息
  → 验证心跳不被 compact 吃掉

test_heartbeat_appended_to_tail_after_compact
  - compact 后 chat 变少（摘要 + window）
  - 最终 history = compacted_chat + 心跳（尾部）
  - 验证: 心跳全部在 compacted chat 之后

test_no_heartbeat_same_as_before
  - 无心跳时，行为与现有逻辑完全一致
  → 回归验证
```

### 6.5 restore 过滤测试（`class TestRestoreFilter`）

```
test_heartbeat_removed
  - user_chat + heartbeat_turn + user_chat
  - restore 后无心跳消息

test_placeholder_removed_with_heartbeat
  - heartbeat_turn（3 条消息，新格式 metadata）
  - restore 后 3 条全部移除

test_legacy_placeholder_removed
  - 旧格式占位消息：裸 (acknowledged) + [Background notification follows]（无 metadata）
  - restore 后被 _is_placeholder content 精确匹配过滤

test_legacy_heartbeat_removed
  - 旧格式心跳（content 前缀）
  - restore 后被过滤

test_chat_preserved
  - 混合 history
  - restore 后 chat 消息完整保留

test_compact_still_works
  - 100 条 chat（无心跳）
  - DolphinStateAdapter.compact_session_state 正常截断到 120
```

### 6.6 端到端测试（`class TestEndToEnd`）

```
test_inject_accumulation_then_save_then_restore
  - 从空 history 开始
  - 交替注入 30 次心跳和 5 次 user_chat
  - 每次 inject 后调用 _evict_oldest_heartbeat
  - 验证: 心跳不超过 20 条
  - save: compact 决策只看 chat 数量
  - restore: 无心跳、无占位、DolphinStateAdapter 校验通过

test_metadata_roundtrip
  - 注入带 metadata 的心跳 → save → load → restore
  - metadata 在 save/load 过程中保留
  - restore 时能通过 metadata 正确过滤

test_backward_compatibility
  - 混合旧格式（content 前缀）和新格式（metadata）心跳
  - 全链路正常：inject 淘汰、save 隔离、restore 过滤
```
