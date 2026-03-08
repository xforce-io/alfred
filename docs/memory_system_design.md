# Memory System Design

> **版本**: v2.0
> **创建时间**: 2026-02-01
> **更新时间**: 2026-03-08
> **状态**: v1（画像记忆层）已实现，v2（全量检索层）Draft

---

## 目录

1. [背景](#1-背景)
2. [名词解释](#2-名词解释)
3. [设计目标与非目标](#3-设计目标与非目标)
4. [能力与功能设计](#4-能力与功能设计)
5. [总体架构](#5-总体架构)
6. [画像记忆层](#6-画像记忆层)
7. [全量检索层](#7-全量检索层)
8. [两层协同](#8-两层协同)
9. [设计思路与折衷](#9-设计思路与折衷)
10. [整体代码结构](#10-整体代码结构)
11. [模块设计](#11-模块设计)
12. [API 设计](#12-api-设计)
13. [技术亮点与创新](#13-技术亮点与创新)
14. [边界考虑](#14-边界考虑)
15. [测试设计](#15-测试设计)
16. [实施路线](#16-实施路线)

---

## 1. 背景

### 1.1 现状

EverBot 的"记忆"能力经历了两个阶段：

**原始阶段**（已替代）：

- **HistoryManager**: 对话超过 10 轮后，将旧消息原文截断追加到 MEMORY.md
- **SessionCompressor**: 基于 LLM 的滑动窗口摘要，压缩 session 内历史
- **WorkspaceLoader**: 将 MEMORY.md 前 50 行注入 system prompt

这些机制本质上是**对话历史的被动归档**，缺乏结构化的知识提取、基于重要性的排序淘汰、跨会话的知识积累与召回、语义检索能力。

**当前阶段**（v1 已实现）：

| 层级 | 机制 | 容量 | 检索方式 |
|------|------|------|----------|
| **Session 持久化** | JSONL 文件存储完整对话历史 | 单 session 无上限 | 无检索，仅按时间顺序恢复 |
| **MEMORY.md 画像记忆** | LLM 提取结构化记忆条目 | 硬上限 30 条 | Top-K by score 注入 prompt |

v1 画像记忆层解决了"了解用户是谁"的问题（偏好、事实、工作流等），但仍存在核心缺失：

- **Session 层**：对话历史以 JSONL 文件按 session 隔离存储。跨 session 的历史不可检索——用户在三个月前的对话中提到过一个技术方案，当前系统无法召回。SessionCompressor 压缩后的原始对话永久丢失。
- **MEMORY.md 层**：上限 30 条，是摘要式的、有损的、小容量的。无法回答"用户曾经说过什么"。

**核心缺失**：没有一个可以承载**全量对话历史**并支持**语义检索**的存储层。随着使用时间增长，大量有价值的对话内容被遗忘或丢失在不可检索的 JSONL 文件中。

### 1.2 目标

构建一个**两层记忆系统**：

1. **画像记忆层**（v1 已实现）：从对话中提取用户画像，评分、衰减、合并，高分常驻 prompt
2. **全量检索层**（v2 本次设计）：全量对话持久化到向量库，支持 dense + sparse 混合检索，按需召回

两层互补——画像记忆回答"用户是谁"，全量检索回答"用户说过什么"。

---

## 2. 名词解释

### 2.1 画像记忆层术语

| 术语 | 定义 |
|------|------|
| **MemoryEntry** | 画像记忆条目，包含 content、category、score 等字段 |
| **Active / Archived** | MEMORY.md 的两个分区。Active（score >= 0.2）注入 prompt，Archived 供按需召回 |
| **Score** | 记忆重要性分数 [0.0, 1.0]，受初始重要性、正向强化、时间衰减三个因素影响 |
| **Extract** | LLM 从对话中提取结构化记忆候选的过程 |
| **Reinforce** | 已有记忆被当前对话再次验证时的加分操作 |

### 2.2 全量检索层术语

| 术语 | 定义 |
|------|------|
| **Chunk** | 对话历史的最小检索单元。一个 chunk 包含一段连续的对话文本及其元数据 |
| **Dense Vector** | 由 embedding 模型生成的稠密浮点向量（如 1024 维），捕捉文本的语义信息 |
| **Sparse Vector** | 由 SPLADE 等模型生成的稀疏向量，维度等于词表大小，非零项对应被激活的词。兼具语义理解和关键词匹配能力 |
| **BM25** | Best Matching 25，经典的基于词频和逆文档频率的稀疏检索算法 |
| **Hybrid Search** | 混合检索，同时执行向量检索和关键词检索，融合两者结果 |
| **RRF** | Reciprocal Rank Fusion，基于排名的融合算法：`score(d) = Σ 1/(k + rank_i(d))`，无需归一化原始分数 |
| **BGE-M3** | BAAI 发布的多语言 embedding 模型（0.6B），可同时输出 dense 和 sparse 向量 |
| **LanceDB** | 嵌入式向量数据库（类似 SQLite），基于 Apache Arrow 列式存储，原生支持混合检索 |
| **ConversationStore** | 全量对话存储层，负责 chunk 的持久化和检索 |

---

## 3. 设计目标与非目标

### 3.1 设计目标

| 编号 | 目标 | 层级 | 优先级 |
|------|------|------|--------|
| G1 | **画像提取**：会话结束时自动提取用户画像记忆（偏好、事实、工作流等） | 画像层 | P0 (已实现) |
| G2 | **评分衰减**：记忆评分支持正向累积与时间衰减，自然遗忘 | 画像层 | P0 (已实现) |
| G3 | **Prompt 常驻**：高分画像记忆常驻 system prompt | 画像层 | P0 (已实现) |
| G4 | **全量持久化**：所有对话历史写入向量库，不因 session 结束或压缩而丢失 | 检索层 | P0 |
| G5 | **混合检索**：支持 dense vector + sparse vector 双通道检索，RRF 融合排序 | 检索层 | P0 |
| G6 | **实时可用**：归档索引延迟 < 5 秒，检索延迟 < 500ms | 检索层 | P0 |
| G7 | **多粒度**：支持 turn / window / session 三级分块粒度 | 检索层 | P1 |
| G8 | **元数据过滤**：支持按时间、session_id、speaker、topic 过滤检索结果 | 检索层 | P1 |
| G9 | **本地优先**：全部数据存储在本地，不依赖外部服务 | 全局 | P0 |
| G10 | **中英文支持**：embedding 模型需良好支持中文和英文 | 检索层 | P0 |

### 3.2 非目标

| 编号 | 非目标 | 说明 |
|------|--------|------|
| N1 | 图数据库 / 知识图谱 | 实体关系网络是更远期的目标 |
| N2 | 替代 Session 持久化 | JSONL session 文件继续作为 Dolphin Agent 状态恢复的主存储 |
| N3 | 多用户隔离 | Alfred 是单用户个人助手 |
| N4 | 云端同步 | 不做跨设备记忆同步 |
| N5 | 自动删除/遗忘全量对话 | 全量保留，用户可手动清理 |

---

## 4. 能力与功能设计

### 4.1 核心能力矩阵

```
┌──────────────────────────────────────────────────────────────────────┐
│                        Memory System                                 │
│                                                                      │
│  ┌──────────────────────────┐   ┌──────────────────────────────────┐ │
│  │     画像记忆层 (v1)       │   │        全量检索层 (v2)           │ │
│  │                          │   │                                  │ │
│  │  · LLM 画像提取          │   │  · 对话归档                      │ │
│  │  · 评分 + 衰减 + 强化    │   │  · 多粒度分块                    │ │
│  │  · MEMORY.md 持久化      │   │  · Dense + Sparse 向量编码       │ │
│  │  · Prompt 常驻注入       │   │  · 混合检索 + RRF 融合           │ │
│  │  · 记忆审阅与合并        │   │  · 元数据过滤                    │ │
│  │                          │   │  · recall_memory 技能            │ │
│  │  回答: "用户是谁"        │   │  回答: "用户说过什么"            │ │
│  └──────────────────────────┘   └──────────────────────────────────┘ │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────────┐ │
│  │                     共享编排层                                    │ │
│  │  · 统一触发（process_session_end）                                │ │
│  │  · 统一注入（build_system_prompt）                                │ │
│  │  · 幂等性保障（watermark 机制）                                   │ │
│  └──────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────┘
```

### 4.2 全量检索层功能详述

#### F1: 对话归档

**触发时机**：与画像记忆层共享同一组触发点（见 [8.1 统一触发](#81-统一触发)）。

**写入内容**：将对话消息按分块策略切分为 chunks，每个 chunk 生成 dense + sparse 向量，连同元数据写入 LanceDB。

#### F2: 混合检索

当 Agent 处理用户消息时，系统自动执行：

1. 将用户当前消息编码为 query 向量
2. 并行执行 dense vector 检索和 sparse vector 检索
3. RRF 融合两路结果
4. 按元数据过滤（排除当前 session，避免重复）
5. 返回 Top-K 结果，格式化后注入 prompt

#### F3: 多粒度分块

| 粒度 | 策略 | 适用查询 | 示例 |
|------|------|----------|------|
| **Turn** | 单轮 user+assistant 对 | 精确事实查询 | "我上次说的数据库密码是什么" |
| **Window** | 3~5 轮滑动窗口，30% overlap | 上下文连贯查询 | "之前讨论的部署方案是怎样的" |
| **Session** | LLM 生成的 session 摘要 | 宏观回忆查询 | "上周我们讨论了哪些话题" |

#### F4: 主动召回技能

注册为 Dolphin Skillkit 中的一个 tool，Agent 可在对话中主动调用：

```
用户: 我之前跟你讨论过一个关于 Redis 缓存穿透的方案，还记得吗？
Agent: [调用 recall_memory("Redis 缓存穿透方案")]
Agent: 是的，在 2026-02-15 的对话中，我们讨论了三种方案...
```

---

## 5. 总体架构

### 5.1 逻辑分层架构

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          应用层 (Application Layer)                      │
│                                                                         │
│  ┌─────────────────┐  ┌──────────────────┐  ┌───────────────────────┐  │
│  │  ChatService    │  │  HeartbeatRunner  │  │  recall_memory Skill  │  │
│  │  (Web/Telegram) │  │  (心跳执行)       │  │  (Agent 主动调用)      │  │
│  └────────┬────────┘  └────────┬─────────┘  └───────────┬───────────┘  │
│           │                    │                        │               │
├───────────┼────────────────────┼────────────────────────┼───────────────┤
│           │           编排层 (Orchestration Layer)       │               │
│           │                    │                        │               │
│  ┌────────▼────────────────────▼────────────────────────▼───────────┐  │
│  │                       MemoryManager                              │  │
│  │  ┌────────────────────────┐  ┌───────────────────────────────┐  │  │
│  │  │  画像记忆编排           │  │  全量检索编排                   │  │  │
│  │  │  process_session_end() │  │  archive_session()             │  │  │
│  │  │  get_prompt_memories() │  │  recall() / get_context_for_  │  │  │
│  │  │                        │  │  prompt()                      │  │  │
│  │  └───────────┬────────────┘  └──────────────┬────────────────┘  │  │
│  └──────────────┼──────────────────────────────┼───────────────────┘  │
│                 │                              │                      │
├─────────────────┼──────────────────────────────┼──────────────────────┤
│                 │       核心层 (Core Layer)      │                      │
│                 │                              │                      │
│  ┌──────────────▼──────────┐   ┌───────────────▼─────────────────┐   │
│  │    画像记忆核心          │   │       全量检索核心               │   │
│  │                         │   │                                 │   │
│  │  · MemoryExtractor      │   │  · SlidingWindowChunker         │   │
│  │    (LLM 提取)           │   │    (分块策略)                    │   │
│  │  · MemoryMerger         │   │  · HybridRetriever              │   │
│  │    (匹配/合并/衰减)      │   │    (Dense + Sparse + RRF 融合)  │   │
│  └──────────────┬──────────┘   └───────────────┬─────────────────┘   │
│                 │                              │                      │
├─────────────────┼──────────────────────────────┼──────────────────────┤
│                 │    基础设施层 (Infra Layer)    │                      │
│                 │                              │                      │
│  ┌──────────────▼──────────┐   ┌───────────────▼─────────────────┐   │
│  │    MemoryStore          │   │       EmbeddingService           │   │
│  │    (MEMORY.md 读写)      │   │  · BGE-M3 模型加载与推理         │   │
│  │                         │   │  · Dense + Sparse 向量生成       │   │
│  │                         │   │                                 │   │
│  │                         │   │       ConversationStore          │   │
│  │                         │   │  · LanceDB 读写                  │   │
│  │                         │   │  · 混合检索                      │   │
│  └─────────────────────────┘   └─────────────────────────────────┘   │
│                                                                       │
│  数据:                                                                 │
│  ~/.alfred/agents/{name}/MEMORY.md        (画像记忆)                    │
│  ~/.alfred/conversation_db/               (全量对话向量库)               │
│  ~/.alfred/models/bge-m3/                 (embedding 模型缓存)           │
└─────────────────────────────────────────────────────────────────────────┘
```

### 5.2 核心业务流程

#### 5.2.1 写入流程（会话结束）

```
会话结束 / 压缩触发 / 心跳完成
        │
        ▼
┌─────────────────────────────────────────────────────┐
│              MemoryManager.process_session_end()     │
│                                                     │
│  ┌──────────────────┐    ┌────────────────────────┐ │
│  │  画像记忆写入链路  │    │  全量检索写入链路       │ │
│  │                  │    │                        │ │
│  │  1. Load 已有记忆 │    │  1. 检查 watermark     │ │
│  │  2. LLM Extract  │    │  2. Chunker 分块       │ │
│  │  3. Apply Decay  │    │  3. BGE-M3 编码        │ │
│  │  4. Merge        │    │  4. LanceDB 写入       │ │
│  │  5. Save to      │    │  5. 更新 watermark     │ │
│  │     MEMORY.md    │    │                        │ │
│  └──────────────────┘    └────────────────────────┘ │
│                                                     │
│  两条链路并行执行，互不阻塞                            │
│  任一链路失败不影响另一条                              │
└─────────────────────────────────────────────────────┘
```

#### 5.2.2 读取流程（Prompt 构建）

```
WorkspaceLoader.build_system_prompt()
        │
        ├── 画像记忆注入 ──────────────────────────────────────────┐
        │   MemoryManager.get_prompt_memories(top_k=20)           │
        │   → MEMORY.md 中 score >= 0.5 的条目                    │
        │   → 常驻注入，无需 query                                 │
        │                                                         │
        │   # 历史记忆                                             │
        │   关于用户的关键信息：                                     │
        │   - [preference] 用户喜欢简洁的代码风格                   │
        │   - [fact] 用户的主要开发语言是 Python                    │
        │                                                         │
        └── 全量检索注入 ──────────────────────────────────────────┐
            ConversationMemory.get_context_for_prompt(user_msg)   │
            → 以 user_message 为 query 检索                        │
            → 仅在有高相关结果时注入                                │
                                                                  │
            # 相关历史对话                                         │
            以下是与当前问题可能相关的历史对话片段：                  │
            ---                                                    │
            [2026-02-15 session_abc] (相关度: 0.82)                │
            用户: Redis缓存穿透有什么好的解决方案？                  │
            助手: 常见的方案有三种：1. 布隆过滤器...                 │
            ---                                                    │
```

#### 5.2.3 检索流程（混合召回）

```
用户消息 / recall_memory 调用
        │
        ▼
┌───────────────────┐
│  EmbeddingService │
│  .encode(query)   │──▶ query_dense, query_sparse
└────────┬──────────┘
         │
         ├──────────────────────────┐
         ▼                          ▼
┌─────────────────┐      ┌──────────────────┐
│ Dense Retrieval │      │ Sparse Retrieval │
│                 │      │                  │
│ LanceDB ANN    │      │ LanceDB FTS /    │
│ query_dense     │      │ sparse vector    │
│ → top_k×2 结果  │      │ → top_k×2 结果   │
└────────┬────────┘      └────────┬─────────┘
         │                        │
         └───────────┬────────────┘
                     ▼
         ┌───────────────────┐
         │   HybridFusion    │
         │                   │
         │  RRF(k=60):       │
         │  score(d) = Σ     │
         │   1/(60+rank_i)   │
         │                   │
         │  + 时间加权        │
         │  + 元数据过滤      │
         │  + 去重            │
         │                   │
         │  → top_k 结果      │
         └───────────────────┘
```

---

## 6. 画像记忆层

### 6.1 核心数据结构

#### Memory Entry（记忆条目）

每条记忆由内容、分数、激活状态及元数据组成：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | `str` | 唯一标识符（uuid4 短格式） |
| `content` | `str` | 记忆的自然语言描述 |
| `category` | `str` | 分类标签（见下表） |
| `score` | `float` | 重要性分数，范围 `[0.0, 1.0]` |
| `created_at` | `datetime` | 创建时间 |
| `last_activated` | `datetime` | 上次被命中/激活的时间 |
| `activation_count` | `int` | 累计被激活次数 |
| `source_session` | `str` | 来源会话 ID |

#### Memory Categories（记忆分类）

| 分类 | 说明 | 示例 |
|------|------|------|
| `preference` | 用户偏好 | "用户喜欢用中文交流" |
| `fact` | 事实信息 | "用户的公司叫 xxx" |
| `experience` | 经验/处理流程 | "遇到 Nginx 502 时，常采用的排查与处理流程" |
| `workflow` | 工作习惯 | "用户每天早上查看股票行情" |
| `decision` | 重要决策 | "项目决定使用 FastAPI 而非 Flask" |
| `skill_usage` | 技能使用模式 | "用户经常让我帮忙写 Python 脚本" |
| `todo` | 待办/跟进 | "用户提到下周要准备演示材料" |

#### MEMORY.md 存储格式

```markdown
# Agent Memory

<!-- Last updated: 2026-02-20T10:30:00 -->
<!-- Total entries: 15 -->

## Active Memories

### [a1b2c3] preference | 0.92 | 2026-02-20 | 12
用户喜欢简洁的代码风格，不喜欢过多注释

### [d4e5f6] fact | 0.88 | 2026-02-19 | 8
用户的主要开发语言是 Python，常用 FastAPI 框架

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
- **写入前备份**：写入前将当前文件备份为 `MEMORY.md.bak`
- Active / Archived 两区划分，控制注入 prompt 的范围
- 按 score 降序排列

### 6.2 记忆生命周期

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

#### Step 1: Extract（提取）

用 LLM 扫描会话历史，输出结构化的记忆候选。提取方式聚焦**用户画像**——关于用户本人的偏好、事实、习惯，而非对话内容的摘要。

**Token 控制**：`existing_memories_summary` 仅传入 Active 区的记忆（score >= 0.2），截断为最多 50 条单行摘要。对话历史超过 8000 tokens 时，先由 SessionCompressor 摘要后再传入。

#### Step 2: Match（匹配）

将提取出的候选记忆与已有记忆进行匹配：
1. **新记忆**：在已有记忆中找不到语义相似的条目
2. **强化命中**：已有记忆被当前对话再次提及或验证

匹配策略：在 Extract 的 LLM 调用中一并完成——prompt 中提供已有记忆摘要，LLM 在提取时自然去重。代码层面使用 token-based Jaccard similarity（阈值 0.35）做二次去重。

#### Step 3: Merge（合并与排序）

**新记忆（初始给分）**：

- `importance: high` → score = 0.8
- `importance: medium` → score = 0.6
- `importance: low` → score = 0.4

**强化命中（正向累积，边际递减）**：

- `score = score + (1.0 - score) * 0.2`
  越接近 1.0 加得越少，天然收敛。*(例如：0.6 → 0.68 → 0.744)*

**时间衰减机制（指数衰减）**：

```python
days_since_activated = (now - last_activated).days
# 7 天保护期，每日衰减 1%
effective_days = max(0, days_since_activated - 7)
score = score * (0.99 ** effective_days)
```

- score < 0.2 → Archived（不进 System Prompt，供按需召回）
- score < 0.05 → 删除

### 6.3 记忆的使用

**高分记忆：Prompt 常驻**

WorkspaceLoader 提取 Active 区中 score >= 0.5 的记忆条目（最多 20 条），注入 system prompt：

```
# 历史记忆

关于用户的关键信息：
- [preference] 用户喜欢简洁的代码风格，不喜欢过多注释
- [fact] 用户的主要开发语言是 Python，常用 FastAPI 框架
- [workflow] 用户每天早上 9 点查看 A 股行情，关注新能源板块
```

**低分记忆：按需召回**

score 在 0.2 ~ 0.5 之间的记忆保留在 Archived 区，通过 `recall_memory` 工具按需召回。

---

## 7. 全量检索层

### 7.1 核心数据结构

#### Chunk（对话检索单元）

```python
@dataclass
class Chunk:
    chunk_id: str              # UUID
    text: str                  # 对话文本（含 speaker 标注）
    session_id: str            # 来源 session
    agent_name: str            # 来源 agent
    timestamp: str             # 窗口内最早消息的时间戳 (ISO 8601)
    timestamp_end: str         # 窗口内最晚消息的时间戳
    speaker: str               # "mixed" | "user" | "assistant"
    granularity: str           # "turn" | "window" | "session"
    turn_start: int            # 窗口起始 turn 索引
    turn_end: int              # 窗口结束 turn 索引
    topic_tags: List[str]      # 可选: LLM 提取的话题标签
    message_count: int         # 窗口内消息数
```

#### RetrievedChunk（检索结果）

```python
@dataclass
class RetrievedChunk:
    chunk: Chunk
    score: float               # 融合后的最终分数
    dense_rank: Optional[int]  # 在 dense 检索中的排名
    sparse_rank: Optional[int] # 在 sparse 检索中的排名
```

### 7.2 分块策略

**Phase 1 仅实现 Window 级别**（精度与成本的最佳平衡）：

- 每窗口 3~5 轮对话（1 turn = 1 user + 1 assistant）
- 相邻窗口 30% overlap，确保边界处信息不丢失
- 单 chunk 文本上限 2000 字符

**文本格式化示例**：

```
用户: 我想了解 Redis 缓存穿透的解决方案
助手: 缓存穿透是指查询一个不存在的数据，常见方案有：
1. 布隆过滤器：在缓存前加一层过滤
2. 缓存空值：将不存在的 key 也缓存起来

用户: 布隆过滤器的误判率怎么控制？
助手: 布隆过滤器的误判率由哈希函数数量和位数组大小决定...
```

### 7.3 Embedding 编码

使用 **BGE-M3**（0.6B）一次推理同时产出 dense vector（1024 维）和 sparse vector（类 SPLADE，含 term expansion）。

- 单例模式，模型全局加载一次
- 缓存在 `~/.alfred/models/bge-m3/`
- 支持 Apple Silicon MPS 加速
- Lazy loading：首次 encode 时加载，不阻塞 daemon 启动

### 7.4 存储与检索

使用 **LanceDB** 嵌入式向量库：

- 数据目录：`~/.alfred/conversation_db/`
- 表 schema：text, dense_vector[1024], sparse_vector, session_id, agent_name, timestamp, speaker, granularity, turn_start, turn_end, message_count
- 原生混合检索 + RRF 融合
- MVCC 保证读写并发安全

### 7.5 混合检索与融合

**RRF 融合**（k=60）：

```python
rrf_score(d) = Σ 1/(60 + rank_i(d))   # 对每个检索通道 i
```

**时间加权**：

```python
time_weight = 1.0 / (1.0 + days_ago * 0.01)  # 100 天前权重 ≈ 0.5
final_score = rrf_score * time_weight
```

**去重**：同一 session 中 turn_range 有重叠的 chunks，只保留分数最高的。

### 7.6 时间维度组织：聚合策略

随着使用时间增长，全量数据持续膨胀。向量检索在百万级 chunk 下仍然高效（HNSW 的 ANN 复杂度与数据量近似无关），但可通过分层聚合优化质量和存储：

**分层聚合**（Phase 2）：

```
0 ~ 30 天:   Turn + Window + Session 三级索引
30 ~ 180 天: Window + Session 两级索引
180 天以上:  仅 Session 摘要索引
```

**索引分片**：LanceDB 支持多表，可按月/季度分片，冷数据不加载到内存。

---

## 8. 两层协同

### 8.1 统一触发

两层共享同一组触发点，在 `MemoryManager.process_session_end()` 中并行执行：

| 触发场景 | 调用方 | 画像记忆 | 全量检索 |
|----------|--------|----------|----------|
| Web UI 点击"新会话" | ChatService | LLM 提取画像 | 归档对话到向量库 |
| Telegram `/new` | TelegramChannel | 同上 | 同上 |
| SessionCompressor 压缩触发 | SessionCompressor | 提取画像 | 归档被压缩的原始消息 |
| 心跳执行完成 | HeartbeatRunner | 提取画像 | **不归档**（低信息密度） |
| 会话空闲超时 | SessionManager | 被动触发 | 归档对话 |

心跳会话默认不归档到全量检索层：心跳执行结果中有价值的部分已通过 mailbox 投递到主 session，随主 session 一起归档。

### 8.2 统一注入

在 `WorkspaceLoader.build_system_prompt()` 中，两层的输出按先后顺序注入：

```
System Prompt
├── SOUL.md
├── AGENTS.md
├── USER.md
├── ...
├── # 历史记忆（画像层，常驻，无需 query）
│   关于用户的关键信息：
│   - [preference] 用户喜欢简洁的代码风格
│   - [fact] 用户的主要开发语言是 Python
│
└── # 相关历史对话（检索层，按需，需要 query）
    以下是与当前问题可能相关的历史对话片段：
    ---
    [2026-02-15 session_abc] (相关度: 0.82)
    用户: Redis缓存穿透有什么好的解决方案？
    助手: 常见的方案有三种...
    ---
```

**关键区别**：
- 画像记忆**始终注入**（不依赖当前 query）
- 全量检索**按需注入**（以 user_message 为 query，有高相关结果才注入）

### 8.3 数据流关系

```
用户对话
    │
    ▼
┌──────────────────────────────────────────────────────┐
│  process_session_end(messages, session_id)            │
│                                                      │
│  messages ──┬── LLM Extract ──▶ MEMORY.md (30 条)    │
│             │   抽象为一句话事实                       │
│             │                                        │
│             └── Chunker ──▶ EmbeddingService ──▶     │
│                 保留原始对话    Dense + Sparse         │
│                                    │                 │
│                                    ▼                 │
│                              LanceDB (无上限)         │
└──────────────────────────────────────────────────────┘

查询时:
    用户提问 ──┬── "我喜欢什么编程风格？"
              │    → 画像记忆已在 prompt 中（无需检索）
              │
              └── "之前讨论过的 Redis 方案"
                   → 全量检索层 recall → 注入原始对话片段
```

### 8.4 幂等性保障

两层各自维护独立的 watermark：

| 层级 | Watermark 存储 | 语义 |
|------|----------------|------|
| 画像记忆 | MEMORY.md 中 `<!-- last_processed_count: N -->` | 已处理 N 条消息 |
| 全量检索 | LanceDB watermark 表 | 已归档 N 条消息 |

同一 session 被多次触发时，两层各自只处理增量消息。

---

## 9. 设计思路与折衷

### 9.1 Embedding 模型选型

**选定：BGE-M3（0.6B）**

| 候选 | 优势 | 劣势 | 结论 |
|------|------|------|------|
| **BGE-M3** | 一个模型同时输出 dense + sparse；100+ 语言，中文强；0.6B 本地可运行；8192 tokens | 精度略低于 Qwen3-Embedding-8B | **选定** |
| Qwen3-Embedding-0.6B | MTEB 多语言榜首；指令感知 | 不原生输出 sparse 向量 | 备选 |
| Qwen3-Embedding-8B | 精度最高 | 8B 本地推理资源消耗大 | 不适合 |
| OpenAI text-embedding-3-large | API 调用省事 | 依赖网络；不支持 sparse；隐私顾虑 | 不符合本地优先 |

**选择理由**：BGE-M3 单次推理同时产出 dense 和 sparse 两种向量，不需要维护独立的 BM25 索引。sparse 向量自带 term expansion（类似 SPLADE），效果优于传统 BM25。0.6B 在 Apple Silicon 上高效运行。

### 9.2 向量数据库选型

**选定：LanceDB（嵌入式）**

| 候选 | 优势 | 劣势 | 结论 |
|------|------|------|------|
| **LanceDB** | 嵌入式无需服务器；原生混合检索 + RRF；磁盘友好 | 社区生态不如 Qdrant 成熟 | **选定** |
| Qdrant | 功能最全；DBSF 融合 | 需要 Docker / 独立进程 | 备选 |
| SQLite + sqlite-vec + FTS5 | 极致轻量 | 混合检索需手工实现 | 过于底层 |
| PostgreSQL + pgvector | 全能 | 重量级 | 杀鸡用牛刀 |

**选择理由**：嵌入式特性与 Alfred 的"本地优先、文件驱动"理念一致。不需要独立进程，数据就是 `~/.alfred/conversation_db/` 下的文件，运维成本为零。

### 9.3 融合策略选型

**选定：RRF（Reciprocal Rank Fusion）**

RRF 无需调参（k=60），不受 dense 和 sparse 两路 score 分布差异影响，即插即用。Linear Combination 需要标注数据调 α 权重，属于过早优化。

### 9.4 分块策略的折衷

- 粒度太粗（整个 session）→ 检索精度低
- 粒度太细（单条消息）→ 丢失上下文
- 多粒度全部索引 → 存储翻倍

**决策**：Phase 1 只做 Window 级别（3-5 轮滑动窗口）。Phase 2 扩展为三级。

### 9.5 MEMORY.md 的设计决策

#### 为什么用 MEMORY.md 而不是数据库？

- **一致性**：与 AGENTS.md / HEARTBEAT.md / USER.md 保持统一的 Markdown 驱动范式
- **可审计**：用户可以直接用编辑器查看、修改记忆内容
- **简单**：不引入额外依赖
- **可迁移**：纯文本，易于备份

#### 为什么不在每轮对话后都提取记忆？

- **成本**：每次提取需要一次 LLM 调用
- **噪声**：单轮对话信息密度通常不高，容易产生低质量记忆
- 会话结束时一次性处理，信息更完整，上下文更丰富

#### Score 机制的设计考量

- **初始分数由 LLM 判断的 importance 决定**：避免所有记忆起点相同
- **命中加分 + 时间衰减**：自然实现"越常用越重要，越久不用越淡忘"
- **7 天保护期**：给新记忆一个观察窗口，避免刚创建就开始衰减
- **阈值分档**：>= 0.2 Active，0.05 ~ 0.2 Archived，< 0.05 删除

#### Token 成本控制

每次会话结束的画像提取需要 1 次 LLM 调用（Extract + Match 合并完成）：
- `existing_memories_summary` 最多传入 50 条 Active 记忆的单行摘要（约 2000~3000 tokens）
- 对话历史过长时，先由 SessionCompressor 摘要后再传入

---

## 10. 整体代码结构

```
src/everbot/core/memory/                # 画像记忆层（已实现）
├── __init__.py
├── models.py                           # MemoryEntry 数据模型
├── store.py                            # MemoryStore: MEMORY.md 读写、解析、备份
├── extractor.py                        # MemoryExtractor: LLM 提取记忆
├── merger.py                           # MemoryMerger: 匹配、合并、衰减、排序
└── manager.py                          # MemoryManager: 统一入口（扩展：集成全量检索）

src/everbot/core/conversation/          # 全量检索层（新增）
├── __init__.py
├── models.py                           # Chunk, RetrievedChunk, ArchiveResult
├── chunker.py                          # SlidingWindowChunker
├── embedding.py                        # EmbeddingService (BGE-M3)
├── store.py                            # ConversationStore (LanceDB)
├── retriever.py                        # HybridRetriever (Dense + Sparse + RRF)
├── memory.py                           # ConversationMemory (编排层)
└── skill.py                            # recall_memory 技能注册

~/.alfred/
├── agents/{name}/MEMORY.md             # 画像记忆持久化
├── conversation_db/                    # 全量对话向量库 (LanceDB)
│   └── conversations.lance/
├── models/bge-m3/                      # Embedding 模型缓存
└── sessions/                           # 现有 JSONL session 文件
```

---

## 11. 模块设计

### 11.1 画像记忆层模块

#### MemoryStore

MEMORY.md 的读写、解析、备份。容错解析（坏条目跳过不崩溃），全量写入（每次重新生成），写入前备份（`.bak`），硬上限 30 条。

#### MemoryExtractor

LLM 提取记忆候选。聚焦用户画像（偏好、事实、习惯），而非对话内容摘要。输出 JSON 格式的 new_memories + reinforced_ids。容错 JSON 解析（支持 markdown code block 包裹）。

#### MemoryMerger

纯逻辑层，无 I/O。负责初始给分、正向强化（边际递减）、时间衰减、token-based Jaccard 去重（阈值 0.35，同 category 内匹配）、内容过滤（屏蔽系统内部文件引用）。

#### MemoryManager

统一入口。`process_session_end()` 串联 Load → Extract → Decay → Merge → Save 全流程。`get_prompt_memories()` 返回注入 prompt 的文本。`apply_review()` 支持 memory-review 技能的合并/废弃/精炼操作。

### 11.2 全量检索层模块

#### SlidingWindowChunker

```python
class SlidingWindowChunker:
    def __init__(
        self,
        window_size: int = 4,       # 每窗口 turn 数
        overlap_turns: int = 1,     # 重叠 turn 数
        max_chunk_chars: int = 2000
    ): ...

    def chunk(
        self,
        messages: List[Dict[str, Any]],
        session_id: str,
        agent_name: str,
    ) -> List[Chunk]:
        """消息按 turn 分组 → 滑动窗口 → 拼接为 text → 生成 Chunk"""
```

#### EmbeddingService

```python
class EmbeddingService:
    MODEL_NAME = "BAAI/bge-m3"
    DENSE_DIM = 1024

    @classmethod
    def get_instance(cls) -> "EmbeddingService":
        """单例模式，模型全局复用。"""

    def encode(self, texts: List[str], batch_size: int = 32
    ) -> Tuple[List[np.ndarray], List[Dict]]:
        """批量编码 → (dense_vectors, sparse_vectors)"""

    def encode_query(self, query: str) -> Tuple[np.ndarray, Dict]:
        """编码查询（BGE-M3 对 query 和 document 使用不同策略）。"""
```

#### ConversationStore

```python
class ConversationStore:
    TABLE_NAME = "conversations"
    DB_DIR = Path("~/.alfred/conversation_db").expanduser()

    def upsert_chunks(self, chunks, dense_vectors, sparse_vectors) -> int:
        """批量写入，chunk_id 去重。"""

    def search_hybrid(self, query_dense, query_sparse, top_k, filters) -> List:
        """原生混合检索（LanceDB hybrid search + RRF）。"""

    def get_archive_watermark(self, session_id: str) -> int: ...
    def set_archive_watermark(self, session_id: str, count: int) -> None: ...
    def delete_by_session(self, session_id: str) -> int: ...
    def count(self, filters=None) -> int: ...
```

#### HybridRetriever

```python
class HybridRetriever:
    def __init__(self, store, embedding, rrf_k=60, time_decay_rate=0.01): ...

    def retrieve(
        self,
        query: str,
        top_k: int = 10,
        filters: Optional[Dict] = None,
        exclude_session: Optional[str] = None,
    ) -> List[RetrievedChunk]:
        """编码 query → 双通道检索 → RRF 融合 → 时间加权 → 去重 → top_k"""

    @staticmethod
    def _rrf_fuse(dense_results, sparse_results, k=60) -> Dict[str, float]: ...
```

#### ConversationMemory

```python
class ConversationMemory:
    async def archive_session(self, messages, session_id, agent_name) -> ArchiveResult:
        """幂等归档：watermark 检查 → Chunker → Embedding → Store → 更新 watermark"""

    def recall(self, query, top_k=10, filters=None, exclude_session=None
    ) -> List[RetrievedChunk]:
        """混合检索。"""

    def get_context_for_prompt(self, user_message, current_session_id,
                                top_k=5, min_score=0.3) -> str:
        """为 prompt 注入格式化的检索结果。无高相关结果时返回空字符串。"""

    def get_stats(self) -> Dict[str, Any]: ...
```

#### recall_memory 技能

```python
def register_recall_skill(skillkit, conversation_memory: ConversationMemory):
    """注册到 Dolphin Skillkit。Agent 可主动调用检索历史对话。

    Tool schema:
    - query: str — 检索关键词或问题描述
    - time_range: str — 可选时间范围
    - top_k: int — 返回结果数量，默认 5
    """
```

---

## 12. API 设计

### 12.1 MemoryManager（画像记忆主接口）

| 方法 | 参数 | 返回值 | 说明 |
|------|------|--------|------|
| `process_session_end` | messages, session_id | Dict (stats) | 异步，提取画像 + 触发全量归档 |
| `get_prompt_memories` | top_k=20 | str | 画像记忆注入文本 |
| `apply_review` | review: dict | Dict (stats) | 应用 memory-review 结果 |
| `load_entries` | — | List[MemoryEntry] | 加载所有画像记忆条目 |

### 12.2 ConversationMemory（全量检索主接口）

| 方法 | 参数 | 返回值 | 说明 |
|------|------|--------|------|
| `archive_session` | messages, session_id, agent_name | ArchiveResult | 异步，归档到向量库 |
| `recall` | query, top_k, filters, exclude_session | List[RetrievedChunk] | 混合检索 |
| `get_context_for_prompt` | user_message, current_session_id, top_k, min_score | str | Prompt 注入文本 |
| `get_stats` | — | Dict | 存储统计 |

### 12.3 集成点

#### 集成点 1: MemoryManager.process_session_end()

```python
# src/everbot/core/memory/manager.py

async def process_session_end(self, messages, session_id):
    # 画像记忆链路（现有逻辑）
    existing = self.store.load()
    extract_result = await extractor.extract(new_messages, existing)
    existing = self.merger.apply_decay(existing)
    merge_result = self.merger.merge(existing, ...)
    self.store.save(merge_result.entries)

    # 全量检索链路（新增）
    try:
        from ..conversation.memory import ConversationMemory
        cm = ConversationMemory()
        agent_name = SessionManager.resolve_agent_name(session_id) or "unknown"
        await cm.archive_session(messages, session_id, agent_name)
    except Exception:
        logger.warning("Conversation archive failed", exc_info=True)
```

#### 集成点 2: WorkspaceLoader.build_system_prompt()

```python
# src/everbot/infra/workspace.py

def build_system_prompt(self, ..., user_message="", session_id=""):
    parts = [...]  # SOUL.md, AGENTS.md, USER.md, ...

    # 画像记忆（常驻注入）
    prompt_memories = memory_manager.get_prompt_memories(top_k=20)
    if prompt_memories:
        parts.append(prompt_memories)

    # 全量检索（按需注入）
    if user_message:
        context = conversation_memory.get_context_for_prompt(
            user_message, session_id, top_k=5, min_score=0.3)
        if context:
            parts.append(context)
```

### 12.4 CLI 接口（Phase 2）

```bash
alfred memory stats          # 统计信息
alfred memory recall "query" # 手动检索
alfred memory delete-session <id>  # 清理
alfred memory rebuild        # 从 JSONL 重建索引
```

---

## 13. 技术亮点与创新

### 13.1 Dense + Sparse 单模型同出

BGE-M3 单次推理同时产出 dense 和 sparse 两种向量，sparse 向量自带 term expansion（类 SPLADE）。一次编码、两路检索，推理成本减半，不需要独立的 BM25 索引。

### 13.2 零运维的嵌入式架构

整个系统不需要任何外部服务。LanceDB 嵌入式运行，BGE-M3 本地加载，没有 Docker、没有数据库进程、没有网络依赖。备份 = 复制目录。与 Alfred "文件驱动"范式一致。

### 13.3 Watermark 幂等归档

借鉴 heartbeat 系统中 ReflectionState 的 watermark 模式，两层各自维护水位线，重复触发只处理增量。

### 13.4 检索结果的上下文去重

滑动窗口分块产生的重叠区域，在 RRF 融合后通过同 session turn_range 重叠检测去重，避免向用户展示高度相似的片段。

### 13.5 两层互补的 Prompt 注入

画像记忆常驻（无需 query，低成本），全量检索按需（需要 query，有相关结果才注入）。既保证了基本的个性化（"我知道你是谁"），又支持深度回忆（"我记得你说过什么"）。

---

## 14. 边界考虑

### 14.1 存储增长

- 画像记忆：硬上限 30 条，不增长
- 全量对话：平均每天 ~3 chunks × ~7.5KB ≈ 22.5KB/天，每年 ~16MB（含索引开销）
- 即使使用数年，存储也在 100MB 级别

### 14.2 Embedding 模型加载延迟

BGE-M3 首次加载 ~3-5 秒（Apple Silicon）。Lazy loading + 单例复用，仅首次归档/检索有额外延迟。Alfred daemon 常驻进程，加载是一次性成本。

### 14.3 心跳会话处理

心跳会话默认不归档到全量检索层（低信息密度）。可通过配置开关控制。有价值的结果已通过 mailbox 投递到主 session。

### 14.4 并发安全

- **画像层**：`asyncio.Lock` + `fcntl.flock`（apply_review）
- **检索层**：`asyncio.Lock` 保证单写，LanceDB MVCC 保证读写并发
- Alfred 单进程模型，跨进程并发不是当前问题

### 14.5 模型降级与容错

| 故障场景 | 应对策略 |
|----------|----------|
| BGE-M3 模型不存在 | 首次使用自动下载；失败则跳过归档/检索 |
| 模型加载 OOM | 降级为仅 BM25 检索（LanceDB FTS） |
| LanceDB 数据损坏 | 从 JSONL session 文件重建（`alfred memory rebuild`） |
| LLM 画像提取失败 | 返回空结果，不阻塞主流程 |
| 编码单 chunk 异常 | 跳过该 chunk，不影响其他 |

### 14.6 隐私与安全

- 所有数据本地存储（`~/.alfred/`）
- Embedding 模型本地运行，文本不经过网络
- 用户可通过文件系统权限保护、手动删除/编辑

---

## 15. 测试设计

### 15.1 画像记忆层测试

#### MemoryStore 测试

| 场景 | 验证点 |
|------|--------|
| 解析正常 MEMORY.md | 所有字段正确提取 |
| 解析空文件 / 不存在 | 返回空列表，不报错 |
| 容错：格式损坏 | 跳过坏条目，其余正常，warning 日志 |
| Round-trip | save → load 结果一致 |
| 写入前备份 | `.bak` 存在且等于旧版本 |
| Active / Archived 分区 | score >= 0.2 Active，< 0.2 Archived |
| 排序 | Active 区按 score 降序 |

#### MemoryExtractor 测试

| 场景 | 验证点 |
|------|--------|
| 正常提取 | 合法候选列表，字段齐全 |
| 空对话 / 极短对话 | 返回空列表 |
| LLM 返回非法 JSON | 容错，返回空列表 |
| 已有记忆去重 | 不产生重复候选 |
| Token 控制 | existing_summary 截断为 top-50 |

#### MemoryMerger 测试

| 场景 | 验证点 |
|------|--------|
| 新记忆给分 | high → 0.8, medium → 0.6, low → 0.4 |
| 正向命中加分 | `score + (1-score)*0.2`，activation_count+1 |
| 边际递减 | 多次命中趋近 1.0 但不超过 |
| 7 天保护期 | 保护期内 score 不变 |
| 超过 7 天衰减 | `0.99^(days-7)` |
| 归档/删除阈值 | < 0.2 Archived，< 0.05 删除 |
| 混合场景 | 新增、命中、衰减、删除同时存在均正确 |

#### MemoryManager 端到端

| 场景 | 验证点 |
|------|--------|
| 首次提取 | 正常创建，MEMORY.md 可解析 |
| 多次累积 | 反复提及的记忆 score 上升 |
| get_prompt_memories | score >= 0.5，最多 20 条，降序 |
| stats | new/updated 计数正确 |

### 15.2 全量检索层测试

#### Chunker 测试 (`tests/unit/test_conversation_chunker.py`)

| 场景 | 验证点 |
|------|--------|
| 基本分块 | 8 条消息（4 轮），window_size=2 → 2~3 chunks |
| Overlap 正确 | turn 重叠区域覆盖正确 |
| 不足一窗口 | 1 轮 → 1 个 chunk |
| 空消息 | 返回空列表 |
| 超长截断 | chunk.text <= max_chunk_chars |
| 元数据正确 | timestamp 为窗口最早时间 |
| turn 索引 | turn_start/turn_end 正确递增 |
| 文本格式 | "用户: ...\n助手: ..." |

#### EmbeddingService 测试 (`tests/unit/test_conversation_embedding.py`)

| 场景 | 验证点 |
|------|--------|
| 单例模式 | 多次 get_instance 返回同一实例 |
| Dense 维度 | shape = (1024,) |
| Sparse 格式 | 含 indices 和 values |
| 批量编码 | 10 条 → 10 dense + 10 sparse |
| 中文支持 | 非全零向量 |

> EmbeddingService 测试需加载模型，标记 `@pytest.mark.slow`。提供 mock fixture 供其他模块快速测试。

#### ConversationStore 测试 (`tests/unit/test_conversation_store.py`)

| 场景 | 验证点 |
|------|--------|
| 自动建表 | 首次写入创建表，schema 正确 |
| 写入与读取 | upsert 后 count 增加 |
| Upsert 去重 | 同 chunk_id 不增加行 |
| 混合检索 | 相似文本排名靠前 |
| 元数据过滤 | session_id / 时间过滤有效 |
| 删除 | delete_by_session 后不可检索 |
| Watermark | get/set round-trip 正确 |

#### HybridRetriever 测试 (`tests/unit/test_conversation_retriever.py`)

| 场景 | 验证点 |
|------|--------|
| RRF 融合 | 两路重叠文档排名最前 |
| RRF 分数 | 手算一致 |
| 时间加权 | 30 天前分数低于今天 |
| Exclude session | 过滤有效 |
| 窗口去重 | 同 session 重叠窗口只保留一个 |
| Top-K | 返回数 <= top_k |

#### ConversationMemory 测试 (`tests/unit/test_conversation_memory.py`)

| 场景 | 验证点 |
|------|--------|
| 正常归档 | ArchiveResult 统计正确 |
| 幂等归档 | 二次归档 chunks_skipped > 0 |
| 增量归档 | 只处理新增消息 |
| Recall | 归档后可召回相关内容 |
| get_context_for_prompt | 有结果返回格式化文本，无结果返回空 |
| min_score 过滤 | 低分结果被过滤 |

### 15.3 集成测试

#### 端到端归档与检索 (`tests/integration/test_conversation_archive_recall.py`)

```python
async def test_archive_then_recall():
    """归档 → 语义检索 → 关键词检索 全链路验证。"""

async def test_cross_session_recall():
    """跨 session 检索验证。"""
```

#### MemoryManager 集成 (`tests/integration/test_memory_system_integration.py`)

```python
async def test_session_end_triggers_both_layers():
    """process_session_end 同时触发画像提取和全量归档。"""

def test_prompt_includes_both_layers():
    """build_system_prompt 包含画像记忆和检索到的历史对话。"""
```

#### 性能测试 (`tests/integration/test_conversation_performance.py`)

```python
@pytest.mark.slow
async def test_retrieval_latency_at_scale():
    """10,000 chunks 下检索延迟 < 500ms。"""
```

### 15.4 Mock 策略

```python
@pytest.fixture
def mock_embedding_service():
    """返回随机向量的 Mock，不加载模型。"""
    service = Mock(spec=EmbeddingService)
    service.encode.side_effect = lambda texts, **kw: (
        [np.random.randn(1024).astype(np.float32) for _ in texts],
        [{"indices": [1, 5, 10], "values": [0.8, 0.5, 0.3]} for _ in texts],
    )
    return service
```

---

## 16. 实施路线

### Phase 1: 画像记忆闭环 (已完成)

- [x] MemoryEntry 数据模型
- [x] MemoryStore：MEMORY.md 容错解析、全量写入、备份
- [x] MemoryExtractor：LLM 提取（content + category + importance）
- [x] MemoryMerger：去重 + 给分 + 强化 + 衰减 + 排序
- [x] MemoryManager：统一入口
- [x] WorkspaceLoader 集成：高分记忆注入 prompt
- [x] 基础测试

### Phase 1.5: 画像记忆健壮性增强 (已完成)

- [x] 并发控制（asyncio.Lock）
- [x] 幂等性（last_processed_count watermark）
- [x] 内容过滤（屏蔽系统内部文件引用）
- [x] memory-review 技能（合并/废弃/精炼）

### Phase 2: 全量检索闭环

- [ ] Chunk / RetrievedChunk / ArchiveResult 数据模型
- [ ] SlidingWindowChunker（Window 级别）
- [ ] EmbeddingService（BGE-M3 加载、批量编码）
- [ ] ConversationStore（LanceDB 读写、混合检索）
- [ ] HybridRetriever（RRF 融合、时间加权、去重）
- [ ] ConversationMemory（编排层、幂等归档）
- [ ] 集成 MemoryManager.process_session_end()
- [ ] 集成 WorkspaceLoader.build_system_prompt()
- [ ] 单元测试 + 集成测试
- [ ] 依赖新增：`lancedb`, `FlagEmbedding`, `numpy`

### Phase 3: 多粒度 + 技能

- [ ] Turn-level + Session 摘要分块
- [ ] 分层聚合（历史数据降级为粗粒度索引）
- [ ] recall_memory 技能注册
- [ ] CLI 命令：`alfred memory stats / recall / rebuild`
- [ ] 从现有 JSONL session 文件批量导入历史数据

### Phase 4: 高级特性

- [ ] Topic 标签自动提取
- [ ] Cross-encoder reranker 精排
- [ ] Web UI 记忆管理面板
- [ ] 索引按月分片 + 冷数据卸载

---

## 附录: 依赖变更

```toml
# pyproject.toml 新增

[project]
dependencies = [
    # ... 现有依赖 ...
    "lancedb>=0.4",
    "numpy>=1.24",
]

[project.optional-dependencies]
memory = [
    "FlagEmbedding>=1.2",     # BGE-M3 模型
    "torch>=2.0",             # PyTorch (FlagEmbedding 依赖)
]
```

`FlagEmbedding` 和 `torch` 放入 optional dependency `[memory]`，避免对不需要全量记忆功能的用户引入重型依赖：

```bash
pip install -e .           # 基础安装
pip install -e ".[memory]" # 含 embedding 模型支持
```
