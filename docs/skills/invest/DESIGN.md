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
│  session.json  — 会话状态（时间戳、已执行步骤）           │
│  cache/        — 外部数据源本地缓存                     │
│  history/      — 历史快照（趋势基线 + 概率校准）          │
│                                                      │
│  工具的持久化存储，flock 原子操作                        │
└─────────────────────────────────────────────────────┘
```

### 关键设计原则

1. **agent 做推理，工具做机械操作** — agent 负责因果判断（LLM 擅长的定性推理），工具负责信号采集、概率计算、图持久化
2. **工具不编排** — 每个工具做一件事，调用顺序由 agent 根据 SKILL.md 的规则自行决定
3. **图是 agent 构建的** — agent 通过多轮推理，逐步调用 `inv node` / `inv edge` / `inv chain` 构建因果图，工具只负责存储和计算
4. **外部数据源对 agent 透明** — 信号脚本封装所有外部 API 访问（缓存、重试、降级），agent 只看到节点状态 + 数据质量元信息（freshness / confidence / errors）

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

**返回**：

```json
{
  "ok": true,
  "data": {
    "timestamp": "2026-03-11T10:00:00Z",
    "nodes_updated": [
      {"id": "fed_liquidity", "state": "tight", "confidence": 0.90, "freshness": "live"},
      {"id": "northbound_flow", "state": "outflow", "confidence": 0.70, "freshness": "cached_2h"},
      {"id": "geo_risk", "state": "high", "confidence": 0.75, "freshness": "live"}
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

# 添加新节点（图结构演化）
$INV node --id tariff_escalation --type event --states "low,medium,high" --label "关税升级风险"
```

**做什么**：读写 `graph.json` 中的节点。agent 通过此工具将自己的定性判断注入图中。

**返回**：

```json
{
  "ok": true,
  "data": {
    "id": "geo_risk",
    "state": "high",
    "confidence": 0.75,
    "reason": "中东局势升级，多源印证",
    "updated_at": "2026-03-11T10:05:00Z"
  }
}
```

#### `inv edge` — 添加/更新因果边

```bash
# 查看某条边
$INV edge --from fed_liquidity --to northbound_flow

# 设定/更新条件概率（agent 基于推理或数据给出）
$INV edge --from fed_liquidity --to northbound_flow \
  --prob '{"tight->outflow": 0.70, "crisis->outflow": 0.90}' \
  --reason "历史数据：美联储缩表期间北向资金 70% 概率净流出"

# 添加新边
$INV edge --from tariff_escalation --to a_share \
  --prob '{"high->bearish": 0.80}' \
  --reason "2018-2019 贸易战期间 A 股大幅下跌"

# 从历史数据自动估算概率（工具调用 probability_estimator）
$INV edge --from fed_liquidity --to northbound_flow --estimate --lookback 5y
```

**做什么**：读写 `graph.json` 中的边。条件概率可以由 agent 直接给出（先验），也可以让工具从历史数据估算。

**返回**：

```json
{
  "ok": true,
  "data": {
    "from": "fed_liquidity",
    "to": "northbound_flow",
    "probabilities": {
      "tight->outflow": 0.70,
      "crisis->outflow": 0.90,
      "normal->neutral": 0.60
    },
    "method": "agent_prior",
    "reason": "历史数据：美联储缩表期间北向资金 70% 概率净流出",
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

# 查看已注册的链条
$INV chain --list

# 删除链条
$INV chain --remove --id 3
```

**做什么**：将 agent 推理出的因果链条作为一个整体注册到 `graph.json`。链条是边的有序组合，但额外携带 agent 的推理说明。链条概率由工具根据路径上各边的条件概率自动计算（联合概率）。

**返回**：

```json
{
  "ok": true,
  "data": {
    "id": 1,
    "path": ["fed_liquidity", "sofr_level", "northbound_flow", "a_share"],
    "label": "美联储紧缩传导至A股",
    "joint_probability": 0.41,
    "edge_probabilities": [0.85, 0.70, 0.65],
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

**做什么**：读取 `graph.json` 中所有已观测节点的状态，沿已注册的链条和边进行正向推理，计算目标节点的概率分布。按链条概率 × 影响幅度排序。

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
          {"node": "sofr_level", "state": "high", "p": 0.85},
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

**做什么**：汇总当前图状态 + 推理结果，生成结构化报告。不做推理（推理由 `inv infer` 完成），只做格式化输出。

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
| `inv infer` | 推理 | 正向概率推理，输出 Top N 链条 | 读 graph.json |
| `inv whatif` | 推理 | 假设推演 | 读 graph.json，写 whatif/ |
| `inv show` | 查询 | 查看图状态、节点、边、链条 | 读 graph.json |
| `inv status` | 查询 | 会话进度，告诉 agent 缺什么 | 读 session.json + graph.json |
| `inv report` | 输出 | 格式化输出报告 | 读 graph.json + 推理结果 |
| `inv journal` | 输出 | 追加推理日志到 SESSION.md | 写 SESSION.md |

## 4. Agent 工作流

SKILL.md 中定义 agent 的标准工作流（公约，agent 必须遵循）：

### 4.1 完整因果分析流程

```
1. inv scan                          # 采集所有信号，填充节点状态
2. inv status                        # 检查当前图的完整度
3. （agent 推理）                     # LLM 分析信号，识别因果关系
4. inv node --id X --state Y ...     # 注入 LLM 的定性判断（如 geo_risk）
5. inv edge --from A --to B ...      # 设定/更新因果边的条件概率
6. inv chain --path "A->B->C" ...    # 注册推理出的因果链条
7. 重复 3-6 多轮                      # 多轮推理，逐步完善图
8. inv infer --top 5                 # 运行推理，获取排序后的链条
9. inv journal --content "..."       # 记录推理过程
10. inv report --format text         # 生成最终报告呈现给用户
```

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
      "label": "美联储净流动性",
      "states": ["abundant", "normal", "tight", "crisis"],
      "observed_state": "tight",
      "confidence": 0.90,
      "freshness": "live",
      "source": "macro",
      "observed_at": "2026-03-11T10:00:00Z"
    }
  },
  "edges": {
    "fed_liquidity->sofr_level": {
      "from": "fed_liquidity",
      "to": "sofr_level",
      "probabilities": {
        "tight->high": 0.85,
        "crisis->high": 0.95,
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

### 5.3 session.json — 会话状态

```json
{
  "started_at": "2026-03-11T09:55:00Z",
  "last_scan": "2026-03-11T10:00:00Z",
  "steps_completed": ["scan", "node:geo_risk", "edge:fed_liquidity->sofr_level", "chain:1", "infer"],
  "inference_results_at": "2026-03-11T10:15:00Z"
}
```

## 6. 因果图模型

### 6.1 节点定义

节点分为三类：

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
- 编写 SKILL.md 公约
- 初始化 `graph.json`：预定义节点 + 空边

### Phase 2：因果图核心

- 实现 `inv edge --estimate`：调用 `probability_estimator.py` 从历史数据估算条件概率
- 实现 `inv chain`：链条注册 + 联合概率计算
- 实现 `inv infer`：正向推理 + 链条排序
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
5. **免责** — 输出中明确声明：分析工具，非投资建议
