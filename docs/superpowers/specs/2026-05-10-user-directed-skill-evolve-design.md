# User-Directed Skill Evolve: 显式指令通道

## 1. Problem

现有 SLM evolve 链路（见 `2026-04-12-skill-evolve-design.md`）只有一条**被动**通道：

```
skill 跑出输出 → skill_logs 沉淀 → Skill Evaluate (LLM Judge) → 不健康才 rollback + evolve
```

Judge 的输入只有 `context_before / skill_output / context_after` 三个字段，评分逻辑全靠 `context_after`（用户下一轮的反应文本，next-turn backfill）。**用户主动给意见这件事在架构上没有入口**：

- 用户在 Telegram 跟 demo_agent 说"把 paper-discovery 改短"——agent 当场不会调 paper-discovery 的 skill，所以不产生 SLM segment
- 即使产生，意见也只 backfill 到"上一条 segment 的 context_after"，没法直接驱动 evolve
- Judge 视角下 = 啥也没发生

实际数据佐证：`paper-discovery` 的 `consecutive_evolve_count = 1` 在 4-26 之后再没动过；用户在期间多次跟 bot 反馈，全都没进入 SLM 改写循环。

## 2. Goal

加一条**主动**通道：用户用自然语言表达"调整 X skill"的意图，agent 识别后立刻产生新版 SKILL.md，写入 testing。

设计原则：
- **skill 形态实现，框架零改动**——跟 `skill-installer` / `routine-manager` 一脉相承
- **复用现有 SLM 原语**——`VersionManager` / `skill_lock` / `read_frontmatter_version`
- **跟自动 evolve 并行不互覆盖**——版本号前缀区分；同一 testing 槽位串行写
- **可逆**——出问题靠下一轮 auto eval 兜底 rollback

## 3. Architecture

新内置 skill `skills/skill-evolver/`，结构：

```
skills/skill-evolver/
  SKILL.md                  ← 意图识别 + 工作流文档
  scripts/
    prepare.py              ← 读现有 SKILL.md，生成新版本号
    commit.py               ← 校验 + VersionManager.publish 到 testing
```

**触发**：自然语言。Agent 识别"调整/改/优化/把 X 改成 Y"等意图后，主动调 `_load_resource_skill("skill-evolver", mode="full")`。

**框架层零改动**：SLM 原语已经支持独立 import（`routine-manager` 已在用同套机制）。

## 4. Data Flow

```
用户："你那个论文报告太长了，只要前 5 条"
   │
   ▼
Agent 识别意图 ──► _load_resource_skill("skill-evolver")
                    │
                    ▼
按 SKILL.md 三步走：
  ① _bash("python skills/skill-evolver/scripts/prepare.py
            --skill paper-discovery --agent <agent_name>")
       → stdout JSON: {
           "current_skill_md": "<完整内容>",
           "new_version": "2.0.0-userevolve-202605101630",
           "writable_dir": "<workspace>/skills/paper-discovery/"
         }
  ② Agent 在对话上下文里重写 SKILL.md
       - 按用户原话调整内容
       - 把 frontmatter version 改成新版本号
       - 写到临时文件（路径由 prepare.py 建议）
  ③ _bash("python skills/skill-evolver/scripts/commit.py
            --skill paper-discovery --version 2.0.0-userevolve-202605101630
            --content-file <tmp>")
       → 调 VersionManager.publish(skill_id, content, status=testing)
       → current_version 立刻指向新版本
       → stdout: success + 版本号
   │
   ▼
Agent 回："已经把 paper-discovery 改到 2.0.0-userevolve-202605101630（testing）。"
```

`current_version` 被 loader 读取——下次 paper-discovery 加载就是新版。`stable_version` 不动；如果新版翻车，下一轮 Skill Evaluate 自动 rollback。

## 5. Components

### 5.1 `skills/skill-evolver/SKILL.md`

要点：
- frontmatter `name: skill-evolver`，`description` 描述清楚"用户想调整某个 skill 时用我"
- 列出典型触发短语（"调整 X" / "把 X 改成 Y" / "X 报告太长"）
- 三步式工作流（prepare → 重写 → commit）说清楚
- 提示 agent **必须**用 prepare.py 返回的 `new_version` 写入 frontmatter（保证版本号一致）
- 提示 agent 改完之后向用户 ack 带版本号

### 5.2 `scripts/prepare.py`

```python
# 输入: --skill <id> --agent <agent_name>
# 输出: JSON to stdout
# {
#   "current_skill_md": <str>,
#   "new_version": <str>,        # f"{base}-userevolve-{YYYYMMDDHHMM}"
#   "writable_dir": <Path>,      # 提示 agent 把 tmp 文件放哪
#   "tmp_file_suggestion": <str>
# }
```

实现：
- 通过 `UserDataManager` 定位 agent workspace
- 用 `VersionManager` 的 read chain 找当前 SKILL.md（layered: workspace → user → repo）
- 提取 `base` version（去掉 `-evolve-*` / `-userevolve-*` 后缀）
- 拼新版本号

### 5.3 `scripts/commit.py`

```python
# 输入: --skill <id> --agent <agent_name>
#       --version <new>
#       --content-file <path>
# 输出: JSON to stdout {"status": "ok", "version": <new>} 或错误
```

实现：
- 读 content-file
- 校验：frontmatter 合法 + frontmatter.version == --version 参数（防错位）
- `VersionManager.publish(skill_id, content, status=VersionStatus.TESTING)`
  - 内部 `skill_lock` 串行化（跟 auto evolve 互斥）
  - 落点：`skill_eval/<skill>/versions/v<new>/skill.md` + 更新 `current.json`
  - 可写层（workspace skills/）也同步写一份（loader 优先级最高）
- 把 `consecutive_evolve_count` 重置为 0（用户驱动是明确意图，不应继承自动 evolve 的失败计数）

## 6. Boundary with Auto Evolve

| 维度 | 自动（已有） | 用户驱动（本设计） |
|---|---|---|
| 触发源 | `routine_fe2d192d` 每 2h | Agent 识别意图 |
| 改写动机 | failure cases | user instruction |
| LLM 实施者 | judge + evolve 内置 client | 主对话 agent |
| 版本号前缀 | `<base>-evolve-<ts>` | `<base>-userevolve-<ts>` |
| 状态 | testing | testing |
| Rollback 兜底 | 下轮 eval 不健康自动 rollback | 同 |
| `consecutive_evolve_count` | 失败 +1 | **重置为 0** |

并发：两条路都通过 `VersionManager.publish` → `skill_lock` 串行；不会 race。

## 7. Known Interaction (Watchpoint)

**用户驱动 evolve 后被 auto eval rollback 的可能**：

场景：用户说"报告改短"，新版只显示 5 篇。隔天用户回了"怎么少了几条"。Judge 把这条 backfill 进 `context_after` → 解读为 critical issue → rollback 到 stable（10 篇）。**用户的指令被自动抹了**。

v1 不预防，先观察。如果实际频繁踩，可加缓冲：带 `-userevolve-` 前缀的版本前 N 轮 eval 仅产报告不 rollback。属于 YAGNI 范畴，等数据说话。

设计文档显式标记此交互点，方便后续观察。

## 8. Out of Scope (YAGNI)

明确不在 v1 做：
- ❌ slash 命令 `/evolve`（用户已否定框架层方案）
- ❌ LLM 意图分类器（agent 加载 skill 已是显式意图识别）
- ❌ 提交前 diff 预览 + confirm（已选择直接 commit + 事后 ack）
- ❌ 给 agent 加新 tool（用 skill loader 通道就够）
- ❌ 直接写 stable（始终 testing，靠 auto eval 鉴定）
- ❌ 框架层修改（保持纯 skill 形态）
- ❌ 多 skill 批量 evolve（一次一个，串行调多次）
- ❌ user-directed 版本免疫 auto rollback（见第 7 节）

## 9. Testing

`tests/unit/`：
- `test_skill_evolver_prepare.py`
  - prepare.py 在不同输入下输出正确 new_version（base 解析、tag 拼接）
  - 找不到 skill 时返回错误
- `test_skill_evolver_commit.py`
  - commit.py frontmatter 校验：合法/缺失 version/version 不匹配
  - 正常路径：调用 `VersionManager.publish` + `consecutive_evolve_count` 重置
  - 与 auto evolve 并发场景：mock skill_lock 验证串行
- `test_skill_evolver_e2e.py`（可选）
  - 端到端：mock agent 重写步骤，验证 prepare → 写入 → commit → loader 看到新版

`tests/integration/`：暂无（v1 主要靠单测）。

## 10. Files Touched

新增：
- `skills/skill-evolver/SKILL.md`
- `skills/skill-evolver/scripts/prepare.py`
- `skills/skill-evolver/scripts/commit.py`
- `tests/unit/test_skill_evolver_prepare.py`
- `tests/unit/test_skill_evolver_commit.py`

修改：无（框架层零改动）。

## 11. Migration / Rollout

- 部署即生效（agent 加载 skills 列表会自动发现 `skill-evolver`）
- 不需要 daemon 重启（skill 是热加载的）
- 不影响现有 routines / auto evolve / skill_logs 写入
- 可灰度：先用 demo_agent 验证一周，再推 main agent
