# Invest 技能设计文档

## 1. 定位

**invest** 是面向用户的统一投资决策辅助技能。它整合数据采集、信号评分、因果推理三个层次，输出带有概率和因果链条的投资决策建议。

核心理念：不只告诉用户"宏观流动性偏紧"，而是展示**为什么偏紧、会传导到哪里、概率多大**。

### 与现有技能的关系

invest 由以下现有技能合并演化而来：

| 原技能 | 在 invest 中的角色 | 处理方式 |
|-------|-------------------|---------|
| investment-signal | 信号层 — 宏观/价值/市场/技术信号采集与评分 | 代码整合进 invest |
| gray-rhino | 信号层 — 风险事件监测与趋势分析 | 代码整合进 invest |
| tushare | 数据层 — 中国市场数据接口参考文档 | references 合并进 invest |

合并后，原有三个技能目录废弃，统一收口为 `skills/invest/`。

## 2. 分层架构

四层架构：**公约层 → 工作区层 → 工具层 → 数据层**。

与 coding-master 的关键区别：invest 的工具层需要访问外部数据源（FRED / yfinance / Tushare / RSS），这些外部依赖**封装在工具层内部，对 agent 透明**。agent 不需要关心 API 超时、重试、缓存等问题，只通过工具返回值中的元信息（freshness、confidence）感知数据质量。

```
┌─────────────────────────────────────────────────────┐
│  公约层 (SKILL.md) — agent 只读                       │
│                                                      │
│  定义：因果推理方法论、节点/边分类法、                    │
│       工具调用规则、输出模板                             │
│  谁写：人类设计时          谁读：agent                   │
│  **不可变**：agent 不得修改                             │
└─────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────┐
│  工作区层 (MD) — agent 读写                            │
│                                                      │
│  SESSION.md    — 当次分析会话记录（agent 通过           │
│                  inv journal 追加推理过程）              │
│  GRAPH.md      — 因果图的人类可读描述（agent 维护，      │
│                  记录当前活跃的因果链条和判断依据）         │
│  whatif/XX.md  — 假设推演工作区（agent 写）              │
└─────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────┐
│  工具层 (Python < 600 行) — agent 调用                 │
│                                                      │
│  信号采集: inv scan                                    │
│  图操作:   inv node / inv edge / inv chain             │
│  推理:     inv infer / inv whatif                      │
│  查询:     inv show / inv status                       │
│  输出:     inv report / inv journal                    │
│                                                      │
│  每个工具做一件机械的事：                                │
│  向上：被 agent 调用                                    │
│  向下：读写数据层                                       │
│                                                      │
│  ┌───────────────────────────────────────────────┐   │
│  │  信号脚本（工具层内部，agent 不直接调用）          │   │
│  │                                               │   │
│  │  signals/macro.py / signals/china.py /        │   │
│  │  signals/rhino.py / signals/value.py /        │   │
│  │  signals/breakout.py                          │   │
│  │                                               │   │
│  │  封装外部数据源访问：                            │   │
│  │  - FRED API / yfinance / Tushare / RSS        │   │
│  │  - 缓存策略（本地缓存 + TTL）                   │   │
│  │  - 失败降级（API 超时 → 返回缓存 + 降低置信度）   │   │
│  │  - 格式统一（各源输出标准化为节点状态）            │   │
│  └───────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────┐
│  数据层 (JSON) — agent 不可见                          │
│                                                      │
│  graph.json    — 因果图（节点 + 边 + 条件概率）          │
│  signals.json  — 最新一次信号快照                       │
│  inference.json— 最近一次推理结果快照                    │
│  session.json  — 会话状态（时间戳、已执行步骤）           │
│  cache/        — 外部数据源本地缓存                     │
│  history/      — 历史快照（趋势基线 + 概率校准）          │
│                                                      │
│  工具的持久化存储，flock 原子操作                        │
└─────────────────────────────────────────────────────┘
```

### 关键设计原则

1. **agent 做推理，工具做机械操作** — agent 负责因果判断（LLM 擅长的定性推理），工具负责信号采集、概率计算、图持久化
2. **语义判断归 LLM，机械计算归工具** — 节点归一、主题识别、因果链条设计是 LLM 的工作；概率连乘、noisy-or 合并、归一化、排序、枚举校验、环检测是工具的工作。工具不做语义匹配，只提供信息辅助 LLM 决策
3. **工具不编排** — 每个工具做一件事，调用顺序由 agent 根据 SKILL.md 的规则自行决定
4. **图是 agent 自顶向下构建的** — agent 先识别主题（theme），从主题展开因果链条，再按链条补齐节点和边。不是从底层信号自由拼凑
5. **外部数据源对 agent 透明** — 信号脚本封装所有外部 API 访问（缓存、重试、降级），agent 只看到节点状态 + 数据质量元信息（freshness / confidence / errors）
6. **派生结果不回写图结构** — 链条概率、资产分布、排序结果属于派生值，不写回 `graph.json`，避免状态过期

## 3. 工具层设计

`$INV = python $SKILL_DIR/scripts/tools.py`

所有工具返回 JSON `{"ok": true, "data": {...}}` 或 `{"ok": false, "error": "..."}`。

### 3.1 信号采集

#### `inv scan` — 采集信号，填充节点当前状态

```bash
# 采集所有信号源
$INV scan

# 仅采集指定模块
$INV scan --modules macro,china,rhino

# 仅采集宏观
$INV scan --modules macro
```

**做什么**：调用信号脚本（signals/macro.py / signals/china.py 等），将结果写入 `signals.json`，并自动更新 `graph.json` 中对应节点的 `observed_state`。信号脚本内部处理缓存、重试、降级，对 agent 透明。

**覆盖规则**：

- `scan` 只更新扫描观测字段，不覆盖 agent 通过 `inv node` 写入的分析判断
- 若节点同时存在 `observed_state` 和 `analyst_state`，推理时优先使用 `analyst_state`
- 工具返回 `effective_state`，明确当前参与推理的是扫描值还是人工判断

**返回**：

```json
{
  "ok": true,
  "data": {
    "timestamp": "2026-03-11T10:00:00Z",
    "nodes_updated": [
      {"id": "fed_liquidity", "state": "tight", "confidence": 0.90, "freshness": "live", "effective_state": "tight"},
      {"id": "northbound_flow", "state": "outflow", "confidence": 0.70, "freshness": "cached_2h", "effective_state": "outflow"},
      {"id": "geo_risk", "state": "medium", "confidence": 0.60, "freshness": "live", "effective_state": "high"}
    ],
    "modules_run": ["macro", "china", "rhino"],
    "errors": ["margin_trading: tushare API timeout, using 2h cache"]
  }
}
```

agent 通过 `freshness` 和 `confidence` 判断数据质量，无需关心底层 API 细节。降级时工具自动使用本地缓存并降低置信度。

### 3.2 图操作

#### `inv node` — 添加/更新节点

```bash
# 查看节点
$INV node --id fed_liquidity

# agent 手动设定节点状态（用于 LLM 定性判断的节点，如 geo_risk）
$INV node --id geo_risk --state high --confidence 0.75 --reason "中东局势升级，多源印证"

# 新建节点前，先查看已有节点列表（归一检查）
$INV node --check
# 返回所有节点的 id / label / type，agent 自行判断是否有语义重复

# 确认无重复后，添加新节点
$INV node --id tariff_escalation --type event --states "low,medium,high" --label "关税升级风险"
```

**做什么**：读写 `graph.json` 中的节点。agent 通过此工具将自己的定性判断注入图中。

**节点归一规则**：

新建节点前，agent **必须**先调用 `inv node --check` 获取已有节点清单，自行判断新节点是否与已有节点语义重复。工具不做语义匹配（LLM 本身就是最好的语义匹配器），只负责返回节点列表供 agent 决策：

- 语义覆盖 → 复用已有节点（如想建 `trade_war_risk`，发现 `trade_risk` 已存在 → 复用）
- 粒度细化 → 复用上级节点或明确建立父子边（如想建 `qt_pace`，发现 `fed_liquidity` 已覆盖 → 复用或建边）
- 确认无重复 → 允许新建

预定义节点（6.1 中列出的）为锁定节点，agent 不可删除或重命名。agent 新建的节点标记为 `dynamic`，`inv scan` 不会自动填充其 `observed_state`。

**节点字段约定**：

- `observed_state` / `observed_confidence`：来自 `inv scan`
- `analyst_state` / `analyst_confidence` / `analyst_reason`：来自 `inv node`
- `effective_state` / `effective_confidence`：读取时计算，不持久化；规则为 `analyst_*` 优先，否则回退到 `observed_*`

**返回**：

```json
{
  "ok": true,
  "data": {
    "id": "geo_risk",
    "analyst_state": "high",
    "analyst_confidence": 0.75,
    "analyst_reason": "中东局势升级，多源印证",
    "effective_state": "high",
    "updated_at": "2026-03-11T10:05:00Z"
  }
}
```

#### `inv edge` — 添加/更新因果边

```bash
# 查看某条边
$INV edge --from fed_liquidity --to sofr_level

# 设定/更新条件概率（agent 基于推理或数据给出）
$INV edge --from fed_liquidity --to sofr_level \
  --prob '{"tight->elevated": 0.70, "crisis->dangerous": 0.90}' \
  --reason "历史数据：流动性收紧阶段短端融资成本明显上行"

# 添加新边
$INV edge --from tariff_escalation --to a_share \
  --prob '{"high->bearish": 0.80}' \
  --reason "2018-2019 贸易战期间 A 股大幅下跌"

# 从历史数据自动估算概率（工具调用 probability_estimator）
$INV edge --from fed_liquidity --to sofr_level --estimate --lookback 5y
```

**做什么**：读写 `graph.json` 中的边。条件概率可以由 agent 直接给出（先验），也可以让工具从历史数据估算。

**返回**：

```json
{
  "ok": true,
  "data": {
    "from": "fed_liquidity",
    "to": "sofr_level",
    "probabilities": {
      "tight->elevated": 0.70,
      "crisis->dangerous": 0.90,
      "normal->normal": 0.60
    },
    "method": "agent_prior",
    "reason": "历史数据：流动性收紧阶段短端融资成本明显上行",
    "updated_at": "2026-03-11T10:06:00Z"
  }
}
```

#### `inv chain` — 注册因果链条

```bash
# agent 推理出一条因果链条后，注册到图中
$INV chain --path "fed_liquidity -> sofr_level -> northbound_flow -> a_share" \
  --label "美联储紧缩传导至A股" \
  --reasoning "美联储缩表 → 短端利率上升 → 美元回流 → 北向资金撤离 → A股承压"

# 预览链条当前联合概率（不持久化）
$INV chain --preview --path "fed_liquidity -> sofr_level -> northbound_flow -> a_share"

# 查看已注册的链条
$INV chain --list

# 删除链条
$INV chain --remove --id 3
```

**做什么**：将 agent 推理出的因果链条作为一个整体注册到 `graph.json`。链条是边的有序组合，但额外携带 agent 的推理说明。

**重要约束**：

- `chain` 只存结构和推理说明，不持久化 `joint_probability`
- `joint_probability` 只在 `inv chain --preview` 或 `inv infer` 时按当前有效状态实时计算
- 若链条上的边概率或节点有效状态变更，后续推理自动反映，无需回写链条

**返回**：

```json
{
  "ok": true,
  "data": {
    "id": 1,
    "path": ["fed_liquidity", "sofr_level", "northbound_flow", "a_share"],
    "label": "美联储紧缩传导至A股",
    "reasoning": "美联储缩表 → 短端利率上升 → 美元回流 → 北向资金撤离 → A股承压",
    "registered_at": "2026-03-11T10:10:00Z"
  }
}
```

### 3.3 推理

#### `inv infer` — 正向概率推理

```bash
# 基于当前所有已观测节点，推理所有资产节点的概率分布
$INV infer

# 仅推理指定目标
$INV infer --target a_share,gold

# 输出 Top N 活跃链条
$INV infer --top 5
```

**做什么**：读取 `graph.json` 中所有有效节点状态，沿已注册的链条和边进行正向推理，计算目标节点的概率分布。按链条概率 × 影响幅度排序，并将结果写入 `inference.json` 供 `inv report` 复用。

**证据合并规则**：

1. 单条链内部：沿路径做条件概率连乘
2. 多条链汇聚到同一目标状态时：先按“首个分叉节点”分组
3. 同组链条视为相关证据，只保留 `chain_score` 最高的一条，避免共享前缀导致重复计数
4. 不同组链条视为近似独立证据，按 noisy-or 合并：`P = 1 - Π(1 - P_i)`
5. 最终对目标节点全部候选状态做归一化，得到 `asset_summary`

这是一个受控启发式，而不是严格的动态贝叶斯网络；目标是避免重复计数，同时保持实现可解释、可维护。

**返回**：

```json
{
  "ok": true,
  "data": {
    "timestamp": "2026-03-11T10:15:00Z",
    "observed_nodes": 6,
    "top_chains": [
      {
        "id": 1,
        "label": "美联储紧缩传导至A股",
        "probability": 0.41,
        "impact": "high",
        "path_detail": [
          {"node": "fed_liquidity", "state": "tight", "observed": true},
          {"node": "sofr_level", "state": "elevated", "p": 0.85},
          {"node": "northbound_flow", "state": "outflow", "p": 0.70},
          {"node": "a_share", "state": "bearish", "p": 0.65}
        ],
        "asset_impact": {"a_share": "↓", "hk_equity": "↓", "usd": "↑"}
      }
    ],
    "asset_summary": {
      "a_share": {"bearish": 0.58, "neutral": 0.30, "bullish": 0.12},
      "gold": {"bullish": 0.65, "neutral": 0.25, "bearish": 0.10}
    }
  }
}
```

#### `inv whatif` — 假设推演

```bash
# 假设地缘风险升至极高，推理影响
$INV whatif --assume "geo_risk=extreme" --top 5

# 多条件假设
$INV whatif --assume "geo_risk=extreme,fed_policy_shift=hawkish" --target gold,crude_oil
```

**做什么**：临时覆盖指定节点的状态（不写入 `graph.json`），重新运行推理，返回假设场景下的结果。同时将推演过程写入 `whatif/` 工作区。

**输出约定**：

- 返回 `baseline` 与 `scenario` 两份结果，便于用户直接比较
- 仅将假设推演写入 `whatif/`，不覆盖 `inference.json`

### 3.4 查询与输出

#### `inv show` — 查看图状态

```bash
# 查看完整图概览（节点数、边数、已观测节点、已注册链条）
$INV show

# 查看某个节点的所有入边和出边
$INV show --node fed_liquidity

# 查看所有已注册链条
$INV show --chains

# 导出 Mermaid 图
$INV show --format mermaid
```

#### `inv status` — 会话状态

```bash
$INV status
```

**返回**：当前会话的状态——上次 scan 时间、已观测节点数、已注册链条数、待补充的边（缺概率）等。类似 `cm progress`，告诉 agent 还缺什么。

```json
{
  "ok": true,
  "data": {
    "last_scan": "2026-03-11T10:00:00Z",
    "nodes_total": 22,
    "nodes_observed": 6,
    "nodes_unobserved": 16,
    "edges_total": 35,
    "edges_with_prob": 28,
    "edges_missing_prob": 7,
    "chains_registered": 4,
    "missing": [
      "7 edges missing probability estimates",
      "16 nodes not yet observed (run inv scan or inv node)"
    ]
  }
}
```

#### `inv report` — 生成报告

```bash
# 生成完整报告（text）
$INV report --format text

# JSON 报告
$INV report --format json

# Mermaid 可视化
$INV report --format mermaid
```

**做什么**：汇总当前图状态 + 最近一次 `inference.json`，生成结构化报告。不做推理（推理由 `inv infer` 完成），只做格式化输出。

**失败条件**：若不存在 `inference.json` 或其 `graph_version` 与当前 `graph.json` 不一致，返回错误并提示先运行 `inv infer`。

#### `inv journal` — 追加会话日志

```bash
$INV journal --content "## 第一轮推理\n\n扫描信号后发现宏观流动性偏紧..."
```

**做什么**：将内容追加到工作区层的 `SESSION.md`。agent 用此记录自己的推理过程和决策依据。

### 3.5 工具总览

| 工具 | 类别 | 做什么 | 读/写 |
|------|------|--------|-------|
| `inv scan` | 信号采集 | 调用信号脚本，填充节点状态 | 写 signals.json + graph.json (nodes) |
| `inv node` | 图操作 | 添加/更新/查看节点 | 读写 graph.json |
| `inv edge` | 图操作 | 添加/更新/查看因果边 + 条件概率 | 读写 graph.json |
| `inv chain` | 图操作 | 注册/查看/删除因果链条 | 读写 graph.json |
| `inv infer` | 推理 | 正向概率推理，输出 Top N 链条 | 读 graph.json，写 inference.json |
| `inv whatif` | 推理 | 假设推演 | 读 graph.json，写 whatif/ |
| `inv show` | 查询 | 查看图状态、节点、边、链条 | 读 graph.json |
| `inv status` | 查询 | 会话进度，告诉 agent 缺什么 | 读 session.json + graph.json |
| `inv report` | 输出 | 格式化输出报告 | 读 graph.json + inference.json |
| `inv journal` | 输出 | 追加推理日志到 SESSION.md | 写 SESSION.md |

## 4. Agent 工作流

SKILL.md 中定义 agent 的标准工作流（公约，agent 必须遵循）：

### 4.1 完整因果分析流程（Theme-driven TTC）

图的构建是**自顶向下**的：先识别主题，从主题展开因果链条，再按链条补齐节点和边。不是从底层信号自由拼凑。

每个步骤标注了执行者——**LLM** 做语义判断，**工具** 做机械计算和持久化：

```
Phase 1: 信号采集
  1. inv scan                              # [工具] 采集所有信号，填充节点 observed_state
  2. inv status                            # [工具] 返回图完整度，告诉 agent 缺什么

Phase 2: 主题识别（TTC 起点）
  3. （agent 推理）                          # [LLM] 综合信号 + 外部背景，识别 2-3 个活跃主题
                                            #   每个主题 = 一句话假设
                                            #   例："美联储紧缩→流动性收缩→新兴市场承压"
                                            #   例："地缘溢价重估→避险资产上行"

Phase 3: 链条展开 + 节点归一
  4. （agent 推理）                          # [LLM] 将每个主题展开为 1-N 条因果链条草案
                                            #   确定链条路径上需要哪些节点
  5. inv node --check                       # [工具] 返回已有节点清单（id/label/type）
  6. （agent 判断）                          # [LLM] 逐个检查链条中的新节点：
                                            #   - 与已有节点语义重复 → 复用
                                            #   - 是已有节点的细化 → 复用或建父子边
                                            #   - 确认无重复 → 新建
  7. inv node --id X --type T --states ...  # [工具] 新建通过归一检查的节点
  8. inv node --id X --state Y --reason ... # [工具] 注入 LLM 的定性判断

Phase 4: 边与链条注册
  9. （agent 推理）                          # [LLM] 为链条上每条边给出条件概率先验 + 理由
  10. inv edge --from A --to B --prob ...   # [工具] 写入边和条件概率
  11. inv edge --from A --to B --estimate   # [工具] 或从历史数据自动估算概率
  12. inv chain --path "A->B->C" ...        # [工具] 注册链条（只存结构和推理说明）
  13. 重复 Phase 3-4                        # 逐个主题展开，直到所有主题覆盖完毕

Phase 5: 推理与输出
  14. inv infer --top 5                     # [工具] 概率计算：连乘、noisy-or、归一化、排序
  15. （agent 审查）                         # [LLM] 审查推理结果是否符合直觉，必要时回退修正
  16. inv journal --content "..."           # [工具] 记录推理过程
  17. inv report --format text              # [工具] 格式化输出
```

**关键约束**：Phase 2-3 是 LLM 的核心工作，工具在这些阶段只提供信息（节点清单、信号数据）不做决策。Phase 4-5 中概率计算完全由工具完成，LLM 只审查结果。

### 4.2 快速信号查询流程

```
1. inv scan --modules macro          # 仅采集宏观信号
2. 直接回复用户                       # 不需要因果推理
```

### 4.3 假设推演流程

```
1. inv scan                          # 先采集当前状态
2. inv whatif --assume "X=Y" --top 5 # 假设推演
3. inv journal --content "..."       # 记录推演
4. 回复用户                           # 对比当前 vs 假设场景
```

## 5. 数据层设计

### 5.1 graph.json — 因果图

```json
{
  "version": "1.0",
  "updated_at": "2026-03-11T10:15:00Z",
  "nodes": {
    "fed_liquidity": {
      "type": "macro",
      "origin": "predefined",
      "label": "美联储净流动性",
      "states": ["abundant", "normal", "tight", "crisis"],
      "observed_state": "tight",
      "observed_confidence": 0.90,
      "freshness": "live",
      "source": "macro",
      "observed_at": "2026-03-11T10:00:00Z"
    },
    "geo_risk": {
      "type": "event",
      "origin": "predefined",
      "label": "地缘政治风险",
      "states": ["low", "medium", "high", "extreme"],
      "observed_state": "medium",
      "observed_confidence": 0.60,
      "analyst_state": "high",
      "analyst_confidence": 0.75,
      "analyst_reason": "Middle East escalation confirmed by multiple sources",
      "freshness": "live",
      "source": "rhino",
      "observed_at": "2026-03-11T10:00:00Z",
      "updated_at": "2026-03-11T10:05:00Z"
    },
    "tariff_escalation": {
      "type": "event",
      "origin": "dynamic",
      "label": "关税升级风险",
      "states": ["low", "medium", "high"],
      "analyst_state": "medium",
      "analyst_confidence": 0.70,
      "analyst_reason": "2025 tariff announcements but no implementation yet",
      "created_by_theme": "贸易摩擦传导",
      "created_at": "2026-03-11T10:04:00Z"
    }
  },
  "edges": {
    "fed_liquidity->sofr_level": {
      "from": "fed_liquidity",
      "to": "sofr_level",
      "probabilities": {
        "tight->elevated": 0.85,
        "crisis->dangerous": 0.95,
        "normal->normal": 0.70
      },
      "method": "historical",
      "reason": "FRED 数据 2000-2025 回测",
      "updated_at": "2026-03-11T10:06:00Z"
    }
  },
  "chains": [
    {
      "id": 1,
      "path": ["fed_liquidity", "sofr_level", "northbound_flow", "a_share"],
      "label": "美联储紧缩传导至A股",
      "reasoning": "...",
      "registered_at": "2026-03-11T10:10:00Z"
    }
  ]
}
```

### 5.2 signals.json — 信号快照

```json
{
  "timestamp": "2026-03-11T10:00:00Z",
  "macro": {
    "net_liquidity": {"value": 5.8e12, "weekly_change_pct": -3.2, "risk_score": 60},
    "sofr": {"value": 5.35, "risk_score": 50},
    "move": {"value": 118, "risk_score": 50},
    "yen_carry": {"usdjpy": 148.5, "spread": 2.1, "risk_score": 20}
  },
  "china": {
    "northbound": {"net_flow_today": -45.2, "consecutive_outflow_days": 2},
    "turnover": {"total_billion": 9500},
    "margin": {"balance_change_pct": -1.2}
  },
  "rhino": {
    "top_signals": [
      {"topic": "中东冲突升级", "type": "accelerating", "trend_score": 8.5, "category": "geopolitics"}
    ]
  }
}
```

### 5.3 inference.json — 推理快照

```json
{
  "generated_at": "2026-03-11T10:15:00Z",
  "graph_version": "2026-03-11T10:10:00Z",
  "top_chains": [
    {
      "id": 1,
      "label": "美联储紧缩传导至A股",
      "probability": 0.41,
      "impact": "high",
      "chain_score": 0.82
    }
  ],
  "asset_summary": {
    "a_share": {"bearish": 0.58, "neutral": 0.30, "bullish": 0.12},
    "gold": {"bullish": 0.65, "neutral": 0.25, "bearish": 0.10}
  }
}
```

### 5.4 session.json — 会话状态

```json
{
  "started_at": "2026-03-11T09:55:00Z",
  "last_scan": "2026-03-11T10:00:00Z",
  "steps_completed": ["scan", "node:geo_risk", "edge:fed_liquidity->sofr_level", "chain:1", "infer"],
  "inference_results_at": "2026-03-11T10:15:00Z",
  "active_inference_graph_version": "2026-03-11T10:10:00Z"
}
```

## 6. 因果图模型

### 6.1 节点定义

节点分为**预定义节点**和**动态节点**两类：

- **预定义节点（predefined）**：下表中列出的所有节点，锁定在 `graph.json` 初始化时。agent 不可删除或重命名。`inv scan` 自动填充其 `observed_state`。
- **动态节点（dynamic）**：agent 在分析过程中通过 `inv node` 新建的节点。`inv scan` 不会自动填充，状态完全由 agent 通过 `analyst_state` 维护。新建前必须经过归一检查（见 3.2 `inv node` 的归一规则）。

预定义节点按角色分为三类：

#### 宏观状态节点（来自 scan:macro + scan:rhino）

| 节点 ID | 含义 | 取值 | 数据来源 |
|---------|------|------|---------|
| `fed_liquidity` | 美联储净流动性 | {abundant, normal, tight, crisis} | FRED |
| `sofr_level` | 短端融资成本 | {low, normal, elevated, dangerous} | FRED |
| `move_index` | 债市波动率 | {calm, elevated, volatile} | yfinance |
| `yen_carry` | 日元套利压力 | {stable, stressed, unwinding} | yfinance + FRED |
| `geo_risk` | 地缘政治风险 | {low, medium, high, extreme} | rhino + agent 判断 |
| `trade_risk` | 贸易摩擦风险 | {low, medium, high} | rhino + agent 判断 |
| `fed_policy_shift` | 美联储政策方向 | {dovish, neutral, hawkish} | rhino + FRED |
| `china_stimulus` | 中国政策刺激 | {tightening, neutral, easing, strong_stimulus} | rhino + Tushare |

#### 市场信号节点（来自 scan:china + scan:breakout）

| 节点 ID | 含义 | 取值 | 数据来源 |
|---------|------|------|---------|
| `northbound_flow` | 北向资金 | {heavy_outflow, outflow, neutral, inflow, heavy_inflow} | Tushare |
| `market_turnover` | 两市成交额 | {cold, normal, active, overheated} | Tushare |
| `margin_trading` | 融资融券 | {deleveraging, stable, leveraging} | Tushare |
| `southbound_flow` | 南向资金 | {outflow, neutral, inflow} | Tushare |

#### 资产价格节点（推理目标）

| 节点 ID | 含义 | 取值 |
|---------|------|------|
| `us_equity` | 美股 | {bearish, neutral, bullish} |
| `a_share` | A股 | {bearish, neutral, bullish} |
| `hk_equity` | 港股 | {bearish, neutral, bullish} |
| `gold` | 黄金 | {bearish, neutral, bullish} |
| `crude_oil` | 原油 | {bearish, neutral, bullish} |
| `us_bond` | 美债 | {bearish, neutral, bullish} |
| `usd` | 美元 | {bearish, neutral, bullish} |
| `btc` | BTC | {bearish, neutral, bullish} |
| `copper` | 铜 | {bearish, neutral, bullish} |

### 6.2 条件概率

每条边的条件概率 `P(effect_state | cause_state)` 来源：

| 优先级 | 方法 | 适用场景 | 工具支持 |
|--------|------|---------|---------|
| 1 | 历史数据回测 | 有量化数据的边（宏观→市场） | `inv edge --estimate --lookback 5y` |
| 2 | 学术文献引用 | 已有实证研究的关系 | `inv edge --prob '{...}' --reason "..."` |
| 3 | agent 先验判断 | 定性关系（地缘→资产） | `inv edge --prob '{...}' --reason "..."` |

### 6.3 推理算法

链条概率计算：沿路径各边条件概率连乘。

```
P(chain) = P(n1_state) × P(n2_state | n1_state) × P(n3_state | n2_state) × ...
```

其中 `P(n1_state)` 取观测置信度，后续各 `P` 取对应边的条件概率。

排序依据：`chain_score = joint_probability × impact_weight`

impact_weight 由资产影响方向的强度决定（↑↑/↓↓ = 2, ↑/↓ = 1, ± = 0）。

### 6.4 状态优先级与校验

为避免状态空间漂移，所有节点状态必须满足预定义枚举，工具层统一校验：

1. `inv scan` 输出的状态必须属于节点定义的 `states`
2. `inv node --state` 写入前必须校验枚举合法性
3. `inv edge --prob` 的键必须满足 `cause_state->effect_state`，且两端状态都在各自节点枚举内
4. 非法状态直接报错，不做自动纠错

节点有效状态优先级：

1. `analyst_state` 存在时，使用 `analyst_state`
2. 否则使用 `observed_state`
3. 两者都不存在时，视为未观测节点，不能作为链条起点

### 6.5 环与时间处理

本设计当前不实现完整动态贝叶斯网络。为控制复杂度，Phase 1-3 只支持单时点 DAG：

- `graph.json` 中禁止形成环
- 遇到明显反身性关系时，拆成跨期节点，例如 `usd_t0 -> northbound_flow_t1`
- 时间展开仅作为建模约定，不在第一版工具中自动生成

这样可以先保证推理语义稳定，再决定是否演进到动态模型。

## 7. 目录结构

```
skills/invest/
├── SKILL.md                          # 公约层：方法论、工具规则、流程模板
├── scripts/
│   ├── tools.py                      # 工具层入口（统一 CLI，< 600 行）
│   ├── signals/                      # 信号脚本（工具层内部，agent 不直接调用）
│   │   ├── macro.py                  #   宏观流动性（FRED + yfinance）
│   │   ├── value.py                  #   价值投资（yfinance）
│   │   ├── china.py                  #   A股/港股（Tushare）
│   │   ├── breakout.py               #   箱体突破（yfinance / Tushare）
│   │   └── rhino.py                  #   灰犀牛（RSS feeds）
│   └── probability_estimator.py      # 工具内部：历史数据 → 条件概率估算
│
├── references/
│   ├── thresholds.md                 # 信号层阈值说明
│   ├── risk_categories.md            # 风险类别与资产映射
│   └── tushare/                      # Tushare API 参考文档
│
└── .invest/                          # 数据层 + 工作区层（运行时生成）
    ├── graph.json                    # 因果图（节点 + 边 + 链条 + 概率）
    ├── signals.json                  # 最新信号快照
    ├── inference.json                # 最近一次推理结果快照
    ├── session.json                  # 会话状态
    ├── cache/                        # 外部数据源本地缓存（TTL 管理）
    ├── SESSION.md                    # 工作区：会话推理日志
    ├── GRAPH.md                      # 工作区：因果图人类可读描述
    ├── whatif/                       # 工作区：假设推演
    └── history/                      # 历史快照
```

## 8. 实现计划

### Phase 1：架构搭建

- 创建 `skills/invest/` 目录结构
- 实现 `tools.py` 骨架：argparse 入口 + scan / node / edge / chain / show / status
- 迁移信号脚本到 `scripts/signals/`（重命名，统一输出为 JSON）
- 编写 SKILL.md 公约（含 Theme-driven TTC 工作流、LLM/工具职责边界）
- 初始化 `graph.json`：预定义节点（`origin: predefined`）+ 空边
- 定义节点 schema 校验与 `analyst_state` / `observed_state` 合并规则
- 实现 `inv node --check`：返回节点清单供 agent 归一判断

### Phase 2：因果图核心

- 实现 `inv edge --estimate`：调用 `probability_estimator.py` 从历史数据估算条件概率
- 实现 `inv chain`：链条注册 + 联合概率预览
- 实现 `inv infer`：正向推理 + 链条排序
- 实现 `inference.json` 持久化与版本校验
- 实现 `inv report`：格式化输出
- 用人工 + LLM 填充初始边的条件概率

### Phase 3：完善

- 实现 `inv whatif`：假设推演
- 实现 `inv journal`：推理日志
- 概率校准：对比先验与历史数据，融合为后验
- Mermaid 可视化输出

## 9. 风险与约束

1. **条件概率的准确性** — 金融市场因果关系是动态的，历史概率不代表未来，需定期校准
2. **反身性** — 贝叶斯网络要求无环，反身性通过时间步展开解决
3. **数据可得性** — 部分节点（如地缘风险）缺乏量化数据，依赖 agent 定性判断
4. **链条爆炸** — 需要剪枝策略（最小概率阈值、最大链条长度）避免噪声
5. **证据相关性近似** — 当前用“首个分叉节点分组 + noisy-or”近似处理多链汇聚，不是严格概率图模型
6. **免责** — 输出中明确声明：分析工具，非投资建议
