# Skill Evolve: 自动化技能改进闭环

## 1. Problem

`skill_evaluate` 每 2h 对技能调用打分（LLM Judge），生成 `eval_report.json`。但评估之后没有任何消费者：不健康的技能不会被回退，也不会被改进。评估报告只是静静地躺在磁盘上。

实际数据佐证：`paper-discovery` 的 `critical_rate=50%`, `satisfaction=0.51`，`is_healthy=False`，但系统无任何反应。

## 2. Design

### 2.1 闭环流程

在 `skill_evaluate` 的 `_evaluate_one()` 中，评估完成后增加两个动作：

```
skill_evaluate（每2h）
  → _evaluate_one(skill_id)
      → judge 打分 → 写 eval_report.json

      → 版本是 testing 且 is_healthy?
          YES → activate，evolve_count 清零，通知用户

      → not is_healthy?
          → evolve_count > MAX_CONSECUTIVE_EVOLVE?
              YES → suspend 技能，通知用户
          → rollback to stable
          → _maybe_evolve():
              → 收集 LLM 输入（当前 SKILL.md + 失败 segments）
              → LLM 生成新版 SKILL.md
              → publish(testing)
              → evolve_count += 1
              → 通知用户
```

### 2.2 版本状态流转

```
active/baseline ──(evaluate不健康)──→ rollback到stable
                                      ↓
                               evolve生成新版
                                      ↓
                                   testing ──(下轮evaluate健康)──→ active
                                      │
                                      ├──(下轮evaluate不健康)──→ rollback → 再次evolve
                                      │
                                      └──(连续evolve > 2)──→ suspended
```

Suspended 恢复：用户手动编辑 SKILL.md → 下次 `skill_evaluate` 检测到版本不一致 → `check_consistency` 修正 → 重新进入正常循环。

### 2.3 Evolve 生成逻辑（`_maybe_evolve`）

输入：
- 当前 SKILL.md 全文
- 失败 segments（`has_critical_issue=True` 或 `satisfaction < 0.5`）
  - 每条包含：`context_before`、`skill_output`、`context_after`、judge `reason`

Prompt 要求：
- 分析失败原因
- 输出改进后的完整 SKILL.md
- 只修改导致问题的部分，保持其他内容不变

输出校验：
- 非空
- 包含 frontmatter（`---` 包裹）
- frontmatter 中有 `version` 字段

版本号策略：基于时间戳，如 `2.0.1-evolve-20260412`，避免与手动版本冲突。

LLM 选择：复用 `SkillContext.llm`（fast model）。evolve 改动受约束且有验证兜底，不需要强模型。

### 2.4 保护机制

| 机制 | 实现 |
|------|------|
| 自动止血 | evaluate 不健康 → 立即 rollback 到 stable |
| evolve 次数上限 | `consecutive_evolve_count > 2` → suspend，不再尝试 |
| 版本隔离 | 新版以 testing 状态发布，下轮 evaluate 验证后才 activate |
| 用户可干预 | 每次 rollback/evolve/suspend 都通过 mailbox 通知用户 |
| 手动恢复 | 用户编辑 SKILL.md 后自动恢复正常循环 |

### 2.5 通知策略

通过 `SkillContext.mailbox` 通知用户，`skill_evaluate` 已有此依赖。

| 事件 | 通知内容 |
|------|---------|
| rollback + evolve | "技能 {skill} 评估不达标（satisfaction={score}），已回退并生成改进版 v{ver}，进入验证阶段" |
| activate | "技能 {skill} v{ver} 验证通过，已生效" |
| suspend | "技能 {skill} 连续 {n} 次改进仍不达标，已暂停。请手动检查 SKILL.md" |

## 3. Data Model Changes

### 3.1 CurrentPointer 扩展

```python
@dataclass
class CurrentPointer:
    current_version: str
    stable_version: str
    repo_baseline: bool = True
    consecutive_evolve_count: int = 0  # activate 时清零
```

序列化兼容：新字段有默认值，旧数据 `from_dict` 时自然填充。

### 3.2 常量

```python
MAX_CONSECUTIVE_EVOLVE = 2   # 连续 evolve 超过此值 → suspend
```

## 4. Modified Files

| 文件 | 改动 |
|------|------|
| `src/everbot/core/slm/models.py` | `CurrentPointer` 新增 `consecutive_evolve_count` 字段 |
| `src/everbot/core/slm/version_manager.py` | `activate()` 时清零 `consecutive_evolve_count` |
| `src/everbot/core/jobs/skill_evaluate.py` | `_evaluate_one` 末尾：不健康 → rollback → `_maybe_evolve`；testing 且健康 → activate |

零新建文件。

## 5. Skill Evaluate 改动伪代码

```python
async def _evaluate_one(context, seg_logger, ver_mgr, skill_id, sessions_dir):
    # ... 现有评估逻辑 ...
    report = await evaluate_skill(context.llm, skill_id, target_version, segments)
    ver_mgr.save_eval_report(skill_id, target_version, report)

    # --- 新增：evolve 闭环 ---

    meta = ver_mgr.get_metadata(skill_id, target_version)

    # testing 版本验证通过 → activate
    if meta and meta.status == VersionStatus.TESTING and report.is_healthy:
        ver_mgr.activate(skill_id, target_version)
        pointer = ver_mgr.get_pointer(skill_id)
        if pointer:
            pointer.consecutive_evolve_count = 0
            # persist pointer
        await context.mailbox.deposit(
            summary=f"技能 {skill_id} v{target_version} 验证通过，已生效",
        )
        return f"v{target_version}: activated"

    # 不健康 → rollback + evolve
    if not report.is_healthy:
        pointer = ver_mgr.get_pointer(skill_id)

        # 检查 evolve 上限
        if pointer and pointer.consecutive_evolve_count > MAX_CONSECUTIVE_EVOLVE:
            # suspend
            cur_meta = ver_mgr.get_metadata(skill_id, pointer.current_version)
            if cur_meta:
                cur_meta.status = VersionStatus.SUSPENDED
                cur_meta.suspended_reason = "consecutive evolve limit exceeded"
                # persist
            await context.mailbox.deposit(
                summary=f"技能 {skill_id} 连续改进仍不达标，已暂停",
            )
            return f"v{target_version}: suspended"

        # rollback
        try:
            rolled_to = ver_mgr.rollback(skill_id, reason="auto-evolve")
        except ValueError:
            return None

        # evolve
        new_version = await _maybe_evolve(
            context, ver_mgr, seg_logger, skill_id, report,
        )
        if new_version:
            if pointer:
                pointer.consecutive_evolve_count += 1
                # persist pointer
            await context.mailbox.deposit(
                summary=f"技能 {skill_id} 评估不达标，已改进为 v{new_version}，进入验证阶段",
            )

    return f"v{target_version}: ..."


async def _maybe_evolve(context, ver_mgr, seg_logger, skill_id, report):
    """基于失败 segments 生成改进版 SKILL.md。"""
    # 读当前 SKILL.md
    skill_md = ver_mgr._skill_md(skill_id)
    if not skill_md.exists():
        return None
    current_content = skill_md.read_text(encoding="utf-8")

    # 收集失败 segments（report.results 与 target_entries 一一对应）
    target_entries = seg_logger.load_by_version(skill_id, report.skill_version)
    failed = [
        e for e, r in zip(target_entries, report.results)
        if r.has_critical_issue or r.satisfaction < 0.5
    ]
    if not failed:
        return None

    # 构建 prompt
    prompt = _build_evolve_prompt(current_content, failed, report.results)
    new_content = await context.llm.complete(prompt, system=_EVOLVE_SYSTEM)

    # 校验
    if not _validate_skill_md(new_content):
        return None

    # 生成版本号
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    base = report.skill_version.split("-evolve-")[0]
    new_version = f"{base}-evolve-{ts}"

    # publish
    ver_mgr.publish(skill_id, new_version, new_content)
    return new_version
```

## 6. Future Optimization

**Dense 验证**：当 skill 调用频率高到 2h 批量评估不够及时时，可在 `SkillLogRecorder.maybe_record()` 之后注入实时评估回调，对 testing 版本的每次调用立即 judge，一次 critical 就 rollback。当前阶段不需要。

## 7. Verification

1. 不健康技能 → 自动 rollback → 版本回到 stable
2. Rollback 后 → LLM 生成新版 → publish 为 testing
3. Testing 版本下轮评估健康 → activate，evolve_count 清零
4. Testing 版本下轮仍不健康 → 再次 rollback + evolve
5. 连续 evolve > 2 → suspend，通知用户
6. Suspended 技能用户手动编辑后 → check_consistency 恢复
7. 序列化兼容：旧 current.json 无 consecutive_evolve_count → 默认 0
8. evolve LLM 生成格式错误 → 校验失败 → 不 publish，下次重试
