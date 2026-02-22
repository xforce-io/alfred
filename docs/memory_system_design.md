# Memory System Design

> EverBot 长期记忆系统设计文档

## 1. 概述

### 1.1 背景

当前 EverBot 的"记忆"能力停留在原始层面：

- **HistoryManager**: 对话超过 10 轮后，将旧消息原文截断追加到 MEMORY.md
- **SessionCompressor**: 基于 LLM 的滑动窗口摘要，压缩 session 内历史
- **WorkspaceLoader**: 将 MEMORY.md 前 50 行注入 system prompt

这些机制本质上是**对话历史的被动归档**，缺乏：

- 结构化的知识提取与组织
- 基于重要性的排序和淘汰
- 跨会话的知识积累与召回
- 语义检索能力

### 1.2 目标

构建一个**结构化的长期记忆系统**，使 Agent 能够：

1. 在会话结束时自动提取有价值的记忆条目（包含事实、偏好、以及对某些内容处理流程的经验等）
2. 对记忆进行评分、合并、排序（支持正向累积与时间衰减）
3. 高分记忆常驻 prompt，低分记忆按需召回
4. 随着交互积累，逐渐形成对用户习惯、偏好以及各类问题处理经验的深度理解

---

## 2. 核心数据结构

### 2.1 Memory Entry（记忆条目）

每条记忆由内容、分数、激活状态及元数据组成：

| 字段                 | 类型         | 说明                            |
| -------------------- | ------------ | ------------------------------- |
| `id`               | `str`      | 唯一标识符（uuid4 短格式）      |
| `content`          | `str`      | 记忆的自然语言描述              |
| `category`         | `str`      | 分类标签（见 2.2）              |
| `score`            | `float`    | 重要性分数，范围 `[0.0, 1.0]` |
| `created_at`       | `datetime` | 创建时间                        |
| `last_activated`   | `datetime` | 上次被命中/激活的时间           |
| `activation_count` | `int`      | 累计被激活次数                  |
| `source_session`   | `str`      | 来源会话 ID                     |

### 2.2 Memory Categories（记忆分类）

| 分类            | 说明          | 示例                                        |
| --------------- | ------------- | ------------------------------------------- |
| `preference`  | 用户偏好      | "用户喜欢用中文交流"                        |
| `fact`        | 事实信息      | "用户的公司叫 xxx"                          |
| `experience`  | 经验/处理流程 | "遇到 Nginx 502 时，常采用的排查与处理流程" |
| `workflow`    | 工作习惯      | "用户每天早上查看股票行情"                  |
| `decision`    | 重要决策      | "项目决定使用 FastAPI 而非 Flask"           |
| `skill_usage` | 技能使用模式  | "用户经常让我帮忙写 Python 脚本"            |
| `todo`        | 待办/跟进     | "用户提到下周要准备演示材料"                |

### 2.3 MEMORY.md 存储格式

MEMORY.md 作为 memory 的**持久化主存储**，采用纯 Markdown 格式：

```markdown
# Agent Memory

<!-- Last updated: 2026-02-20T10:30:00 -->
<!-- Total entries: 15 -->

## Active Memories

### [a1b2c3] preference | 0.92 | 2026-02-20 | 12
用户喜欢简洁的代码风格，不喜欢过多注释

### [d4e5f6] fact | 0.88 | 2026-02-19 | 8
用户的主要开发语言是 Python，常用 FastAPI 框架

### [g7h8i9] workflow | 0.85 | 2026-02-20 | 15
用户每天早上 9 点查看 A 股行情，关注新能源板块

## Archived Memories

### [x1y2z3] fact | 0.18 | 2026-01-10 | 2
用户曾尝试用 Vue 写前端，但后来放弃了
```

**标题行格式**：`### [id] category | score | last_activated | hits`

**设计要点**：

- **人类可读、可审计**：用户可以直接查看和编辑
- **解析简单**：按 `###` 分割，标题行按 `|` split 即可
- **容错解析**：解析失败的条目跳过并 warn，不影响其他条目
- **全量写入**：每次 save 重新生成完整文件，自动修复被破坏的格式
- **写入前备份**：写入前将当前文件备份为 `MEMORY.md.bak`，防止写入异常丢数据
- Active / Archived 两区划分，控制注入 prompt 的范围
- 按 score 降序排列

---

## 3. 记忆生命周期

### 3.1 整体流程

```
┌─────────────────────────────────────────────────────────────┐
│                    会话结束 / 会话总结                         │
│                                                             │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐              │
│  │  Extract  │───▶│  Match   │───▶│  Merge   │──▶ MEMORY.md│
│  │  识别新记忆 │    │ 匹配已有  │    │ 合并排序  │              │
│  └──────────┘    └──────────┘    └──────────┘              │
│       │                │                                    │
│       ▼                ▼                                    │
│   新记忆条目       命中的已有记忆                               │
│                    (强化加分)                                 │
└─────────────────────────────────────────────────────────────┘
```

### 3.2 Step 1: Extract（提取）

**触发时机**：

- 会话被 close / new（用户主动结束）
- 会话被自动总结（SessionCompressor 触发时）
- 心跳会话完成一轮执行后
- 会话空闲超时自动归档

**提取方式**：
用 LLM 扫描会话历史，输出结构化的记忆候选：

```
Prompt:
请分析以下对话记录，提取值得长期记忆的信息。

对于每条记忆，输出：
- content: 简洁的自然语言描述（一句话）
- category: preference / fact / experience / workflow / decision / skill_usage / todo
- importance: high / medium / low

对话记录：
{conversation_history}

已有记忆（仅用于去重参考）：
{existing_memories_summary}

输出 JSON 数组：
```

**Token 控制**：`existing_memories_summary` 仅传入 Active 区的记忆（score >= 0.2），截断为最多 50 条单行摘要，避免输入 token 过长。对话历史超过 8000 tokens 时，先由 SessionCompressor 摘要后再传入。

**LLM 输出示例**：

```json
[
  {
    "content": "用户偏好用 pytest 而非 unittest",
    "category": "preference",
    "importance": "medium"
  },
  {
    "content": "用户提到下周三有产品评审会议",
    "category": "todo",
    "importance": "high"
  }
]
```

### 3.3 Step 2: Match（匹配）

将提取出的候选记忆与已有记忆进行匹配，识别：

1. **新记忆**：在已有记忆中找不到语义相似的条目
2. **强化命中**：已有记忆被当前对话再次提及或验证

**匹配策略**：

- **第一期**：在 Extract 的 LLM 调用中一并完成——prompt 中提供已有记忆摘要，LLM 在提取时自然去重，不需要额外调用
- **后续**：引入 embedding + 向量相似度做更精确的匹配

### 3.4 Step 3: Merge（合并与排序）

将 Extract + Match 的结果与现有 MEMORY.md 合并：

**新记忆（初始给分）**：

- `importance: high` → score = 0.8
- `importance: medium` → score = 0.6
- `importance: low` → score = 0.4

**强化命中（正向累积，边际递减）**：

- `last_activated` 更新为当前时间
- `activation_count += 1`
- `score = score + (1.0 - score) * 0.2`
  越接近 1.0 加得越少，天然收敛，无需截断。*(例如：0.6 → 0.68 → 0.744)*

**时间衰减机制（指数衰减）**：

- 每次 Merge 触发时，对全局记忆基于最后激活的时间点执行指数衰减：
  ```python
  days_since_activated = (now - last_activated).days
  # 7 天为记忆保护期，不发生衰减。每日衰减率设定为 1% (即 0.99)
  decay_factor = 0.99
  effective_days = max(0, days_since_activated - 7)
  score = score * (decay_factor ** effective_days)
  ```
- score 低于 **0.2** 的记忆标记为 Archived，不再进入 System Prompt，仅供按需召回
- score 低于 **0.05** 的记忆视为彻底遗忘，清除删除

**排序**：

- Active 区按 score 降序排列
- 写回 MEMORY.md

---

## 4. 记忆的使用

### 4.1 高分记忆：Prompt 常驻（第一期）

WorkspaceLoader 加载 MEMORY.md 时，提取 Active 区中 **score >= 0.5** 的记忆条目，注入 system prompt 的 `# 历史记忆` 区域。

**控制机制**：

- 最多注入 **20 条**高分记忆（防止 prompt 过长）
- 如果超过 20 条，按 score 取 top-20

**注入格式**（精简，节省 token）：

```
# 历史记忆

关于用户的关键信息：
- 用户喜欢简洁的代码风格，不喜欢过多注释
- 用户的主要开发语言是 Python，常用 FastAPI 框架
- 用户每天早上 9 点查看 A 股行情，关注新能源板块
- ...
```

### 4.2 低分记忆：按需召回（后续实现）

score 在 0.2 ~ 0.5 之间的记忆保留在 MEMORY.md 的 Archived 区，后续通过 `recall_memory` 工具按需召回。

---

## 5. 与现有系统的集成

### 5.1 触发点

| 触发场景                      | 调用方                | 说明                   |
| ----------------------------- | --------------------- | ---------------------- |
| 用户在 Web UI 点击 "新会话"   | `ChatService`       | 总结当前会话并提取记忆 |
| 用户在 Telegram 发送 `/new` | `TelegramChannel`   | 同上                   |
| SessionCompressor 压缩触发    | `SessionCompressor` | 压缩同时提取记忆       |
| 心跳执行完成                  | `HeartbeatRunner`   | 从心跳会话中提取记忆   |
| 会话空闲超时自动归档          | `SessionManager`    | 被动触发               |

### 5.2 模块划分

```
src/everbot/core/memory/
├── __init__.py
├── models.py          # MemoryEntry 数据模型
├── store.py           # MemoryStore: MEMORY.md 的读写、解析、备份
├── extractor.py       # MemoryExtractor: LLM 提取记忆
├── merger.py          # MemoryMerger: 匹配、合并、衰减、排序
└── manager.py         # MemoryManager: 对外统一接口
```

### 5.3 MemoryManager 核心接口

```python
class MemoryManager:
    """记忆系统统一入口"""

    def __init__(self, memory_path: Path, llm_context: Any):
        self.store = MemoryStore(memory_path)
        self.extractor = MemoryExtractor(llm_context)
        self.merger = MemoryMerger()

    async def process_session_end(
        self,
        history_messages: List[Dict],
        session_id: str
    ) -> dict:
        """
        会话结束时处理记忆。

        Returns: {"new": 新增条目数, "updated": 更新条目数}
        """
        # 1. 加载现有记忆
        existing = self.store.load()

        # 2. 从对话中提取候选记忆
        candidates = await self.extractor.extract(
            history_messages,
            existing_summary=self._summarize_existing(existing, max_entries=50)
        )

        # 3. 匹配 + 合并 + 衰减 + 排序
        result = self.merger.merge(existing, candidates, session_id)

        # 4. 持久化（含备份）
        self.store.save(result.entries)

        return result.stats

    def get_prompt_memories(self, top_k: int = 20) -> str:
        """获取注入 prompt 的高分记忆文本"""
        entries = self.store.load_active(min_score=0.5)
        entries.sort(key=lambda e: e.score, reverse=True)
        lines = [e.content for e in entries[:top_k]]
        return "\n".join(f"- {line}" for line in lines)

    def _summarize_existing(self, entries: List[MemoryEntry], max_entries: int = 50) -> str:
        """生成已有记忆摘要，用于传入 LLM 做去重参考。仅取 Active 区 top-N。"""
        active = [e for e in entries if e.score >= 0.2]
        active.sort(key=lambda e: e.score, reverse=True)
        lines = [f"[{e.id}] {e.content}" for e in active[:max_entries]]
        return "\n".join(lines)
```

### 5.4 与 WorkspaceLoader 集成

修改 `WorkspaceLoader.build_system_prompt()` 中关于 MEMORY.md 的处理逻辑：

```python
# 现有逻辑（替换）:
# memory_lines = instructions.memory_md.split('\n')[:50]

# 新逻辑:
memory_manager = MemoryManager(memory_path, llm_context)
prompt_memories = memory_manager.get_prompt_memories(top_k=20)
if prompt_memories:
    parts.append(f"# 历史记忆\n\n关于用户的关键信息：\n{prompt_memories}")
```

### 5.5 与 HistoryManager 的关系

现有 `HistoryManager._archive_to_memory()` 将被 **替代**：

- 不再直接将原始消息追加到 MEMORY.md
- 改为通过 `MemoryManager.process_session_end()` 进行结构化提取

`HistoryManager.trim_if_needed()` 的裁剪逻辑保留，但归档动作改为触发 MemoryManager。

---

## 6. 分阶段实施

### Phase 1: 最小闭环（提取 → 存储 → 注入 → 衰减）

**目标**：用最少的代码跑通记忆的完整生命周期——能从对话中提取记忆、持久化存储、注入后续 prompt、自然衰减遗忘。

- [ ] 定义 `MemoryEntry` 数据模型
- [ ] 实现 `MemoryStore`：MEMORY.md 的容错解析、全量写入、写入前备份
- [ ] 实现 `MemoryExtractor`：LLM 提取记忆候选（content + category + importance）
- [ ] 实现 `MemoryMerger`：LLM 语义去重 + 初始给分 + 正向命中加分 + 时间衰减 + 排序
- [ ] 实现 `MemoryManager`：串联以上流程的统一接口
- [ ] 修改 `HistoryManager`：归档动作改为触发 MemoryManager
- [ ] 修改 `WorkspaceLoader`：从 MEMORY.md 提取高分记忆注入 prompt
- [ ] 基础测试

### Phase 1.5: 健壮性增强

**目标**：在跑通的基础上补全生产环境所需的健壮性和精细化管理。

- [ ] 并发控制：`MemoryManager` 内置 `asyncio.Lock`，保证多触发点串行写入
- [ ] 幂等性保证：通过 `source_session` 检查避免同一会话重复提取
- [ ] 矛盾检测：Extract prompt 增加 `contradicts` 字段，识别与已有记忆的矛盾
- [ ] 负向惩罚：被矛盾的旧记忆 `score *= 0.5`，加速淘汰
- [ ] `todo` 类过期处理：增加 `expires_at` 字段，过期 todo 直接降分归档
- [ ] 用户显式管理：支持对话中 "记住这个" / "忘掉那个"

### Phase 2: 按需召回与向量检索

**目标**：支持大容量记忆的高效匹配，让系统拥有通过工具主动在历史记忆池中检索的能力。

- [ ] 实现 `recall_memory` 技能，注册到 skillkit
- [ ] 接入 embedding API（text-embedding-v3 或本地模型）
- [ ] 搭建本地向量存储（chromadb / sqlite-vss）
- [ ] `MemoryMerger`：匹配环节增加基于向量的初步筛选
- [ ] `MemoryRecaller`：语义向量相似度检索，在 Archived 区查询
- [ ] 增量更新优化，避免全量重算

### Phase 3: 高级特性

- [ ] 记忆分组与层级化（如 "工作" / "生活" 子分类）
- [ ] 记忆冲突检测与自动解决策略
- [ ] 记忆可视化（Web UI 中展示和管理记忆列表）

---

## 7. 测试策略（Phase 1）

### 7.1 MemoryStore（解析与持久化）

| 场景                     | 验证点                                                                             |
| ------------------------ | ---------------------------------------------------------------------------------- |
| 解析正常格式的 MEMORY.md | 所有字段正确提取（id, content, category, score, last_activated, activation_count） |
| 解析空文件 / 文件不存在  | 返回空列表，不报错                                                                 |
| 容错：某条记忆格式损坏   | 跳过该条，其余正常加载，输出 warning 日志                                          |
| 全量写入后重新解析       | save → load 结果一致（round-trip 验证）                                           |
| 写入前备份               | 写入后 `.bak` 文件存在且内容等于旧版本                                           |
| Active / Archived 分区   | score >= 0.2 在 Active 区，< 0.2 在 Archived 区                                    |
| 排序                     | Active 区按 score 降序排列                                                         |

### 7.2 MemoryExtractor（LLM 提取）

| 场景              | 验证点                                                       |
| ----------------- | ------------------------------------------------------------ |
| 正常对话提取      | 返回合法的候选列表，content / category / importance 字段齐全 |
| 空对话 / 极短对话 | 返回空列表                                                   |
| LLM 返回非法 JSON | 容错处理，返回空列表，不崩溃                                 |
| 已有记忆去重      | 对话中再次提到已有记忆的内容时，不产生重复候选               |
| Token 控制        | existing_summary 超过 50 条时被截断为 top-50                 |

### 7.3 MemoryMerger（合并与衰减）

| 场景                   | 验证点                                                                            |
| ---------------------- | --------------------------------------------------------------------------------- |
| 新记忆初始给分         | high → 0.8, medium → 0.6, low → 0.4                                            |
| 正向命中加分           | score 按 `score + (1-score)*0.2` 更新，activation_count +1，last_activated 更新 |
| 边际递减收敛           | 多次连续命中后 score 趋近 1.0 但永远不超过                                        |
| 时间衰减：7 天保护期内 | score 不变                                                                        |
| 时间衰减：超过 7 天    | score 按 `0.99^(days-7)` 衰减，数值验证                                         |
| 归档阈值               | 衰减后 score < 0.2 的条目移入 Archived 区                                         |
| 删除阈值               | score < 0.05 的条目被清除                                                         |
| 混合场景               | 同时存在新增、命中、衰减、删除的条目，合并结果均正确                              |

### 7.4 MemoryManager（端到端）

| 场景                   | 验证点                                                |
| ---------------------- | ----------------------------------------------------- |
| 首次提取（无已有记忆） | 正常创建记忆，写入 MEMORY.md，文件内容可解析          |
| 多次会话累积           | 记忆条数增长，被反复提及的记忆 score 上升             |
| get_prompt_memories    | 只返回 score >= 0.5 的条目，最多 20 条，按 score 降序 |
| 返回值 stats           | new 和 updated 计数与实际变更一致                     |

### 7.5 集成测试

| 场景                 | 验证点                                                                               |
| -------------------- | ------------------------------------------------------------------------------------ |
| WorkspaceLoader 注入 | 生成的 prompt 中包含高分记忆文本，不包含 Archived 区内容                             |
| HistoryManager 触发  | 归档时调用 MemoryManager.process_session_end()，而非直接写 MEMORY.md                 |
| 完整生命周期         | 对话 → 会话结束 → 提取记忆 → 新会话 prompt 中可见记忆 → 长期不活跃后记忆衰减消失 |

---

## 8. 关键设计决策

### 8.1 为什么用 MEMORY.md 而不是数据库？

- **一致性**：与 AGENTS.md / HEARTBEAT.md / USER.md 保持统一的 Markdown 驱动范式
- **可审计**：用户可以直接用编辑器查看、修改记忆内容
- **简单**：不引入额外的数据库依赖
- **可迁移**：纯文本，易于备份和迁移
- 当记忆条目超过 ~200 条时，可考虑拆分为 MEMORY.md（Active）+ 向量库（Archived）

### 8.2 MEMORY.md 的健壮性保障

虽然选择了 Markdown 作为存储格式，但通过以下策略保证可靠性：

- **格式简洁**：每条记忆就是一个 `###` 块，标题行按 `|` 分割，解析逻辑简单
- **容错解析**：解析失败的条目跳过并 warn，不影响其他条目的加载
- **全量写入**：每次 save 重新生成完整文件，自动修复被用户编辑破坏的格式
- **写入前备份**：写入前将当前文件备份为 `MEMORY.md.bak`，防止写入异常丢数据

### 8.3 为什么不在每轮对话后都提取记忆？

- **成本**：每次提取需要一次 LLM 调用
- **噪声**：单轮对话信息密度通常不高，容易产生低质量记忆
- 会话结束时一次性处理，信息更完整，上下文更丰富

### 8.4 Score 机制的设计考量

- **初始分数由 LLM 判断的 importance 决定**：避免所有记忆起点相同
- **命中加分 + 时间衰减**：自然实现"越常用越重要，越久不用越淡忘"
- **衰减起点 7 天**：给新记忆一个观察窗口，避免刚创建就开始衰减：
- **阈值分档**：>= 0.2 Active（可注入 prompt），0.05 ~ 0.2 Archived（可召回），< 0.05 删除

### 8.5 Token 成本控制

每次会话结束的记忆处理需要 **1 次 LLM 调用**（Extract + Match 合并完成）。控制策略：

- `existing_memories_summary` 最多传入 50 条 Active 记忆的单行摘要（约 2000~3000 tokens）
- 对话历史过长时（超过 8000 tokens），先由 SessionCompressor 摘要后再传入 Extract

### 8.6 隐私与安全

- 所有记忆存储在本地（`~/.alfred/agents/{name}/MEMORY.md`）
- 不上传到任何外部服务（LLM 调用除外）
- 用户可随时删除或编辑 MEMORY.md
