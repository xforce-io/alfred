# Event Memory Design

> **版本**: v3.0
> **创建时间**: 2026-05-01
> **状态**: Draft
> **关系**: 取代 `memory_system_design.md` v2 的全量检索层方案。v1 画像记忆层保留并扩展。

---

## 1. 背景

v1（画像记忆层）解决了"用户是谁"的问题。线上跑下来观察到一个稳定缺口：

- 跨会话**事件**没法回忆。例如"上周决定改用 FastAPI"、"用户说明天要交付 X"、"昨天聊过 Redis 缓存方案" 在下一次会话里不可用。
- v2 草案给出的方案是**全量对话向量化（BGE-M3 + LanceDB + RRF）**——能力上确实覆盖，但相对当前用量是过度工程：单 agent（demo_agent）累计只处理过 16 条消息、最高频条目被强化 20 次后产物只有 5 条画像，跑全量向量库的运维与依赖成本与收益严重失衡。
- 同时画像 extractor 的 prompt 显式排除事件类信息（"对话中讨论的新闻、事件、赛果等时效性内容"），所以即使 LLM 看到事件也不会留下来。

参考 Hermes（Hindsight provider 的 `events / cases / patterns` 分类）和 OpenClaw（`MEMORY.md` evergreen + `memory/YYYY-MM-DD.md` 事件日志 + 30 天半衰期指数衰减），二者在轻量化方向上做出的共同选择是：**画像保持 evergreen，事件按时间落盘 + 衰减**。本设计沿用这条路。

---

## 2. 设计目标与非目标

### 2.1 目标

| 编号 | 目标 | 优先级 |
|------|------|--------|
| G1 | 在画像记忆基础上引入**事件记忆**，记录决定、待办、关键互动 | P0 |
| G2 | 事件记忆按时间衰减，自然过期，无需手工清理 | P0 |
| G3 | 注入下一轮 prompt 时区分"画像"与"近期事件"两段 | P0 |
| G4 | 复用现有 `MemoryEntry` / `MemoryStore` / `MemoryMerger` 框架，最少新代码 | P0 |
| G5 | 事件可被 `recall_memory` 技能按关键字召回 | P1 |
| G6 | 不引入向量库、embedding 模型、Docker 等额外组件 | P0 |

### 2.2 非目标

| 编号 | 非目标 | 说明 |
|------|--------|------|
| N1 | 全量对话归档与语义检索 | v2 草案的方向，本期不做。等 BM25 grep 真正不够再考虑 |
| N2 | 跨 agent 事件共享 | 事件归属单 agent workspace |
| N3 | 实时事件推送/提醒 | 提醒由 HEARTBEAT 体系负责，事件记忆只是"记下来"以便回忆 |
| N4 | 事件的合并/聚类 | 事件天然是时间序列；想要趋势可以另起 reflection 任务 |

---

## 3. 数据模型

### 3.1 MemoryEntry 扩展

在现有字段基础上新增两个字段：

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `kind` | `str` | `"profile"` | `"profile"` 或 `"event"` |
| `event_at` | `Optional[str]` | `None` | 事件发生时间（ISO8601）。`kind="event"` 必填，`kind="profile"` 留空 |

向后兼容：旧 `MEMORY.md` 解析时若无 `kind` 字段，默认填 `"profile"`，无需迁移脚本。

### 3.2 Category 在两种 kind 下的语义

| kind | category 取值 |
|------|---------------|
| `profile` | `preference` / `fact` / `experience` / `workflow` / `decision`（沿用现状） |
| `event` | `decision` / `todo` / `incident` / `interaction` / `milestone` |

`decision` 在两种 kind 下都存在但语义不同：
- profile.decision = "用户长期决定使用 FastAPI 重构后端"（重复确认才进画像）
- event.decision = "2026-05-01 用户决定把 demo_agent 切到 deepseek-chat"（一次性时点决定）

两者**不去重、不合并**，extractor prompt 会显式区分。

---

## 4. 存储

### 4.1 文件布局

```
~/.alfred/agents/{agent_name}/
├── MEMORY.md              # kind="profile"，沿用现状
└── events/
    ├── 2026-04.md         # kind="event"，按月分文件
    ├── 2026-05.md
    └── ...
```

**为什么按月而不是按日**：低活跃 agent 一天可能 0 事件，按日会产生大量空文件；按月是检索粒度（top-N 时只读最近 1~2 个月）和文件数量的折衷。

**为什么不单文件**：单个 `events.md` 长期会涨到 MB 级别，每次 load 全文解析浪费；按月分文件天然支持冷数据不加载。

### 4.2 events/YYYY-MM.md 格式

复用 `MEMORY.md` 的解析格式，仅 header 行多一段 `event_at`：

```markdown
# Event Memory — 2026-05

<!-- Last updated: 2026-05-01T14:00:00Z -->
<!-- Total entries: 3 -->

## Events

### [a1b2c3] event/decision | 0.80 | 2026-05-01T10:30:00Z | 2026-05-01 | 1
用户决定把 demo_agent 的默认模型切到 deepseek-chat，理由是成本敏感

### [d4e5f6] event/todo | 0.60 | 2026-05-01T11:15:00Z | 2026-05-03 | 1
用户提到本周五（5/3）要交付 KWeaver demo，需要提前两天准备
```

**Header 格式**：`### [id] kind/category | score | event_at | last_activated_date | activation_count`

注意 header 多了 `kind/category` 段（不是单 category）和 `event_at` 段（事件时间，比 last_activated 更早）。

`MEMORY.md` 不变，header 仍是 `### [id] category | score | last_activated_date | activation_count`——靠**文件路径**区分 kind，不靠 header。这样老的 MEMORY.md 不需要重写。

### 4.3 文件级保留策略

- 当前月与上月：常驻
- 3~12 月前：按需 load（只在 recall 时读）
- 12 月以上：移到 `events/archive/`，默认不再扫描
- 不主动删除文件，用户可手动清理

---

## 5. 写入流程

### 5.1 触发点

复用现有 `MemoryManager.process_session_end()`，画像与事件**并行**抽取：

```
session_end(messages, session_id)
    ├── extract_profile()  ← 现有逻辑，写 MEMORY.md
    └── extract_events()   ← 新增逻辑，写 events/YYYY-MM.md
```

任一失败不影响另一条。

### 5.2 Event Extractor

新增 `EventExtractor`，复用 `MemoryExtractor` 的 LLM 调用骨架，仅替换 prompt：

```
你是一个事件记忆抽取器。从下面的对话中提取**这次会话发生的事件**。

## 应该提取
- decision: 用户做出的具体一次性决定（带时间锚定）
- todo: 用户提到的待办、deadline、跟进事项
- incident: 出现的问题、报错、异常
- interaction: 重要的交互节点（达成共识、用户表达情绪等）
- milestone: 项目/任务的进度推进

## 不应该提取
- 用户的长期偏好、习惯、画像（那是 profile extractor 的职责）
- 助手的回答内容、搜索结果
- 工具调用细节

## 输出
JSON 格式，每条带：
- content: 一句话描述事件
- category: decision|todo|incident|interaction|milestone
- event_at: ISO8601 时间戳，没有明确时间就用对话发生时间
- importance: high|medium|low
- due_at: 仅 todo 用，可选

宁缺毋滥。大多数对话只产生 0~2 条事件。
```

### 5.3 写入逻辑

```python
class EventStore:
    def __init__(self, events_dir: Path): ...

    def append(self, entries: List[MemoryEntry]) -> None:
        """按 event_at 的月份分组，append 到对应月文件。"""

    def load_recent(self, days: int = 30) -> List[MemoryEntry]:
        """只读最近 N 天的月文件，用于 prompt 注入和衰减。"""

    def load_all(self) -> List[MemoryEntry]:
        """全量加载（仅 recall 用）。"""
```

事件**不去重、不合并**——每次会话抽出来的事件直接 append。这是与画像记忆最大的区别：
- 画像：相同语义的多次确认 → reinforce 同一条
- 事件：每次发生都是独立时点 → 多条独立记录

---

## 6. 衰减与生命周期

### 6.1 画像（不变）

`score *= 0.99^(days_since_activated - 7)`，7 天保护期。

### 6.2 事件（新）

更快衰减，30 天半衰期：

```python
days_old = (now - event_at).days
event.score = initial_score * (0.5 ** (days_old / 30))
```

- `initial_score`：importance high=0.8 / medium=0.6 / low=0.4
- 30 天后衰减到一半，60 天到四分之一
- todo 类有 `due_at`：到期前不衰减；过期后按 `(due_at - now)` 计算衰减天数

### 6.3 注入阈值

- `score >= 0.3` 才注入 prompt
- 30 天 medium 事件衰到 0.3，正好是默认遗忘窗口
- todo 默认更慢（受 due_at 保护）

### 6.4 删除

- 事件不主动删除条目，靠衰减自然失效
- 文件级别按 4.3 节策略归档

---

## 7. 注入流程

### 7.1 Prompt 结构

```
# 用户画像（来自 MEMORY.md，常驻）
关于用户的关键信息：
- [preference] 用户喜欢简洁代码风格
- [fact] 用户主要用 Python

# 近期事件（来自 events/，时间窗 + 衰减过滤）
最近 30 天的关键事件：
- [2026-05-01 decision] 用户决定把 demo_agent 切到 deepseek-chat
- [2026-04-30 todo, due 2026-05-03] 周五要交付 KWeaver demo
- [2026-04-28 incident] 心跳任务在 03:00 触发了 OOM
```

### 7.2 选择逻辑

```python
def get_prompt_events(self, top_k: int = 10, days: int = 30) -> str:
    entries = self.event_store.load_recent(days=days)
    entries = self.merger.apply_event_decay(entries)
    candidates = [e for e in entries if e.score >= 0.3]
    # 排序：todo 优先（受 due_at 保护），其余按 score*recency 综合
    candidates.sort(key=event_priority, reverse=True)
    return format_event_block(candidates[:top_k])
```

### 7.3 与画像的注入顺序

`build_system_prompt` 中：画像在前（描述用户长期画像），事件在后（提供近期上下文）。两段都有就两段都注入；任何一段空就跳过对应 header。

---

## 8. 召回（recall_memory 技能）

### 8.1 Phase 1：关键字 grep

```python
def recall_events(query: str, top_k: int = 5) -> List[MemoryEntry]:
    # 1. 加载所有月文件（含已归档）
    all_entries = event_store.load_all()
    # 2. 按 query token 在 content 中的命中数排序
    ranked = bm25_lite_score(query, all_entries)
    return ranked[:top_k]
```

`bm25_lite_score`：tf-idf 简化版，纯 Python 实现，不引入 `rank_bm25` 依赖。年级别数据量（千条以内）够用。

### 8.2 升级判据

什么时候考虑加 SQLite FTS5 / vector：
1. `events/` 累计超过 5000 条 → grep 慢于 200ms
2. 用户多次反馈"我说过 X 但你想不起来"——精确召回不够
3. 跨语种召回失败率 > 30%

不达到上述任何一条不上索引。

---

## 9. 模块设计

### 9.1 复用 vs 新增

| 模块 | 状态 | 改动 |
|------|------|------|
| `models.py` | 修改 | `MemoryEntry` 加 `kind`、`event_at` 字段 |
| `store.py` | 修改 | header parser 兼容 `kind/category` 形式（profile 文件无 kind 段时默认 profile） |
| `extractor.py` | 修改 | 拆出 `_PROFILE_PROMPT` 与 `_EVENT_PROMPT`，新增 `EventExtractor` 类 |
| `merger.py` | 修改 | 新增 `apply_event_decay()`，事件不走 `merge` 而是直接 append |
| `manager.py` | 修改 | `process_session_end` 同时跑画像与事件链路 |
| `event_store.py` | 新增 | `EventStore`，复用 `_HEADER_RE` 但识别新 header 格式 |

### 9.2 EventStore 接口

```python
class EventStore:
    def __init__(self, events_dir: Path): ...

    def append(self, entries: List[MemoryEntry]) -> None: ...
    def load_recent(self, days: int = 30) -> List[MemoryEntry]: ...
    def load_all(self) -> List[MemoryEntry]: ...
    def list_archived_months(self) -> List[str]: ...
    def archive_old_months(self, keep_recent_months: int = 12) -> int: ...
```

不需要 `save()` 全量重写——append-only 就够了。

### 9.3 MemoryManager 改动

```python
async def process_session_end(self, messages, session_id):
    # 画像链路（现有）
    profile_stats = await self._process_profile(messages, session_id)

    # 事件链路（新）
    try:
        event_stats = await self._process_events(messages, session_id)
    except Exception:
        logger.warning("Event extraction failed", exc_info=True)
        event_stats = {"new_count": 0}

    return {"profile": profile_stats, "event": event_stats}

def get_prompt_memories(self, top_k: int = 20) -> str:
    """返回画像 + 事件两段拼接。"""
    profile = self._format_profile_block(top_k)
    events = self._format_event_block(top_k=10, days=30)
    return "\n\n".join(p for p in [profile, events] if p)
```

---

## 10. 折衷与说明

### 10.1 为什么不复用单一 MEMORY.md

考虑过用 `kind` 字段区分、塞进同一个文件。否决理由：
- 画像增长慢（30 条上限），事件持续增长，混在一起会让 MEMORY.md 越来越大
- 解析成本：每次 load 都要全文读，事件 100 条以上时影响 startup
- 用户审计：画像和事件的视图需求不同（画像看"我是谁"，事件看时间线），分开文件用户翻起来更方便

### 10.2 为什么按月分文件

按日（OpenClaw 方案）对低活跃 agent 太碎；单文件长期太大。按月是经验上的甜点：12 个月 12 个文件，每个文件几十到几百条，单文件 < 100KB。

### 10.3 为什么事件不合并

合并意味着丢失时间锚定。"用户提到要交付 demo" 出现两次，画像记忆会合并成"用户偏好按时交付"，事件记忆需要保留两次时点。如果未来需要趋势分析，由独立的 reflection 任务从 raw events 算，而不是在写入路径上做。

### 10.4 为什么不上 BM25 库

`rank_bm25` 是 70 行纯 Python 的事，引入依赖不划算。年级别 grep 在千条以内 < 50ms。等真有规模问题再迁移。

### 10.5 与 v2（向量层）的关系

v2 草案的 `ConversationStore` / `HybridRetriever` / `BGE-M3` 设计**暂时不实施**。本设计覆盖了 v2 想解决的核心场景（跨会话事件回忆）的 80%，剩下 20%（语义化 fuzzy 召回大段历史对话）等真出现回归再做。届时可在 EventStore 之上加一层 embedding 索引，事件粒度的 chunk 比对话粒度更天然。

---

## 11. 实施步骤

### Step 1：数据模型与存储（无行为改动）
- [ ] `MemoryEntry` 加 `kind`、`event_at` 字段，默认值保证向后兼容
- [ ] `MemoryStore` parser 兼容新 header（`kind/category` 形式）
- [ ] 单测：旧 `MEMORY.md` 文件能正确解析为 `kind="profile"`

### Step 2：EventStore
- [ ] 新增 `event_store.py`：append / load_recent / load_all / archive
- [ ] 单测：append 正确按月分文件、load_recent 时间过滤正确

### Step 3：EventExtractor
- [ ] 拆 prompt：`_PROFILE_PROMPT` 与 `_EVENT_PROMPT`
- [ ] 新增 `EventExtractor` 类（复用 `_call_llm`）
- [ ] 单测：事件 prompt 在典型对话上能产出合法 JSON

### Step 4：Merger 衰减
- [ ] `apply_event_decay`：30 天半衰期 + due_at 保护
- [ ] 单测：30 天 medium 事件衰到 0.3 附近、todo 在 due_at 前不衰减

### Step 5：MemoryManager 集成
- [ ] `process_session_end` 双链路并行
- [ ] `get_prompt_memories` 返回画像 + 事件拼接
- [ ] 集成测试：单次 session_end 后 MEMORY.md 与 events/2026-MM.md 都正确写入

### Step 6：Recall 技能
- [ ] `recall_memory` skill 加 `kind` 参数（profile / event / both）
- [ ] 简易 BM25 grep 实现
- [ ] 集成测试：注入 50 条 mock 事件后能召回到目标条目

### Step 7：观测
- [ ] 在 `process_session_end` 输出 stats 里加事件计数
- [ ] 跑一周后看 demo_agent 的 events/*.md 是否积累、衰减、注入正常

---

## 12. 边界与故障处理

| 场景 | 处理 |
|------|------|
| EventExtractor LLM 调用失败 | warning，事件这次不写，画像链路不受影响 |
| 月文件解析损坏 | 跳过该文件 + warning，其他月不受影响 |
| event_at 缺失 | fallback 到 session 结束时间 |
| due_at 解析失败 | 当作普通 todo（无保护期） |
| events_dir 不存在 | 首次 append 时自动创建 |
| 跨月事件（event_at 在上月） | 按 event_at 写入对应月文件，不强制写当月 |

---

## 附录：与 v1/v2 文档的关系

- `memory_system_design.md` v1 部分（画像层）：保留并扩展（加 kind 字段）
- `memory_system_design.md` v2 部分（全量检索层）：本设计取代，标记为 deprecated
- 后续若需向量层，作为 v4 在 EventStore 之上叠加，不影响本设计的接口
