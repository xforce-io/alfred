# Feature 1: Refine 可观测性增强

## Spec
当前 refine 逻辑虽然工作，但完全静默：
- 代码中 stats["refined"] 统计了但用户看不到
- 没有日志记录哪些 entry 被 refined
- entropy 约束检查未考虑 refine（虽然设计如此，但可能让 LLM 不积极）

需要添加轻量级可观测性，避免日志爆炸。

**Acceptance Criteria**:
- [ ] 在 `apply_review()` 中添加精简日志：记录被 refined 的 entry id 和 content 长度变化（before→after），不记录完整内容
- [ ] 在 memory-review 的返回结果中显示具体 refine 统计："X entries refined"
- [ ] 确保 entropy 约束提示词正确处理 refine（不改变 entry 数量，但应在约束说明中提及）
- [ ] 保持日志简洁，避免输出完整 entry 内容

## Analysis

### 1. apply_review() 中的 refine 逻辑（manager.py:195-203）

当前 refine 处理：
```python
for item in review.get("refined_entries", []):
    eid = item.get("id", "")
    content = item.get("content", "")
    if eid not in entry_map:
        logger.warning("Refine references missing ID: %s", eid)
        continue
    entry_map[eid].content = content
    stats["refined"] += 1
```

**问题**：直接覆盖 `content`，没有记录旧内容长度。需要在覆盖前读取 `len(entry_map[eid].content)`，覆盖后记录 `len(content)`，然后用 `logger.info()` 输出 `id` + 长度变化。不需要记录完整内容文本。

### 2. memory-review 返回结果（memory_review.py:77）

当前返回：
```python
return f"Memory review: {review_stats}, profile: {compress_result}"
```
其中 `review_stats` 是 `apply_review()` 返回的 dict：`{"merged": 0, "deprecated": 0, "reinforced": 0, "refined": 0}`。

**问题**：这个 dict 直接 str 化了，用户看到的是 `{'merged': 0, 'deprecated': 0, 'reinforced': 0, 'refined': 0}` 这种原始 Python dict 表示。需要格式化为更可读的摘要字符串，显式包含 "X entries refined"。

### 3. entropy 约束提示词（memory_review.py:108-113）

当前约束说明：
```
## Constraints
- merge: creates 1 new entry, removes 2 → net -1
- deprecate: reduces score (accelerates natural decay)
- reinforce: boosts score of existing entry
- refine: updates content in-place, no score change
- Total effect must be entropy-reducing: merge_count + deprecate_count >= reinforce_count
```

**问题**：refine 已经在约束说明中提及了（第 4 条），但总效果公式 `merge_count + deprecate_count >= reinforce_count` 没有提到 refine。这在语义上是正确的（refine 不影响 entry 数量也不影响 score，所以不需要参与 entropy 计算），但缺少一句明确说明 "refine 不受此约束限制，可以自由使用"，可能导致 LLM 保守地避免 refine。

### 4. entropy 校验逻辑（memory_review.py:129-139）

```python
merge_count = len(result.get("merge_pairs", []))
deprecate_count = len(result.get("deprecate_ids", []))
reinforce_count = len(result.get("reinforce_ids", []))
if merge_count + deprecate_count < reinforce_count:
    ...trim reinforcements...
```

refine 完全不参与 entropy 校验，这是正确的。无需修改校验逻辑本身。

### 5. 日志模式

项目统一使用 `logging.getLogger(__name__)` 模块级 logger。级别使用规范：
- `logger.debug()` — 详细调试信息
- `logger.info()` — 正常操作结果（如 `manager.py:131` 的 memory processing complete）
- `logger.warning()` — 缺失 ID、约束违反等异常但可恢复的情况
- `logger.error()` — 操作失败

refine 日志应使用 `logger.info()` 级别，与其他正常操作一致。

### 6. 测试覆盖

`test_self_reflection.py:282-291` 已有 `test_refine_updates_content` 测试。需要新增测试验证：
- refine 时产生正确的日志输出（可用 `caplog` fixture 捕获）
- 返回结果字符串中包含 refine 统计

### 7. 影响范围

仅需修改 2 个文件：
- `src/everbot/core/memory/manager.py` — apply_review() 中添加 refine 日志
- `src/everbot/core/jobs/memory_review.py` — 格式化返回结果 + 调整 entropy 约束提示词

## Plan

1. **manager.py:apply_review() — 添加 refine 日志**
   - 在 `entry_map[eid].content = content` 之前，记录旧内容长度 `old_len = len(entry_map[eid].content)`
   - 覆盖后，记录 `new_len = len(content)`
   - 添加 `logger.info("Refined entry %s: %d→%d chars", eid, old_len, new_len)`
   - 位置：manager.py 第 201-202 行之间

2. **memory_review.py:run() — 格式化返回结果**
   - 将 `review_stats` dict 格式化为可读字符串，例如：`"merged 2, deprecated 1, reinforced 1, refined 3"`
   - 替换第 77 行的原始 dict 转字符串
   - 使用列表推导过滤掉 count=0 的项，保持简洁

3. **memory_review.py:_analyze_memory_consolidation() — 调整 entropy 约束提示词**
   - 在 refine 描述行后追加一句：`"refine is entropy-neutral and not capped — use it freely to improve clarity"`
   - 位置：memory_review.py 第 112 行之后
   - 目的：明确告诉 LLM refine 不受 entropy 约束，鼓励积极使用

4. **测试 — 验证日志和返回格式**
   - 新增测试 `test_refine_logs_length_change`：用 `caplog` 验证 refine 时输出包含 entry id 和长度变化
   - 新增测试 `test_review_stats_format`：验证格式化后的返回字符串包含 "refined" 关键字和正确数字

## Test Results

## Dev Log
