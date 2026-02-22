---
name: investment-signal
description: 投资信号检测 — 宏观流动性监控 + 美股价值投资评估
version: "1.0.0"
tags: [investing, macro, value-investing, signals, finance]
---

# Investment Signal Detector

投资信号检测技能，整合两个核心分析维度：

1. **宏观流动性监控** — 净流动性、SOFR、MOVE 指数、日元套利
2. **美股价值投资框架** — ROE、负债率、FCF、护城河、估值

## When to Use

- 用户询问当前宏观流动性状态、市场风险水平
- 用户要求评估某只美股的基本面质量
- 用户希望获取综合投资信号报告
- 用户提到净流动性、SOFR、MOVE、日元套利等宏观指标
- 用户提到 ROE、负债率、自由现金流、护城河、估值等价值投资指标

## Scripts

### 1. `scripts/macro_liquidity.py` — 宏观流动性监控

四维度风险评分系统：

| 维度     | 数据源                              | 预警条件              | 权重 |
|---------|-------------------------------------|----------------------|------|
| 净流动性 | FRED: WALCL - WTREGEN - RRPONTSYD   | 单周下降 > 5%         | 40%  |
| SOFR    | FRED: SOFR                          | 突破 5.5%            | 25%  |
| MOVE指数 | Yahoo: ^MOVE                        | 超过 130             | 20%  |
| 日元套利 | Yahoo: JPY=X + FRED: DGS2           | USD/JPY急跌+利差收窄   | 15%  |

**状态输出**: 充裕(Abundant) / 正常(Normal) / 偏紧(Tight) / 危机(Crisis)

**使用方式**:
```bash
# 文本格式输出
python scripts/macro_liquidity.py --format text

# JSON 格式输出
python scripts/macro_liquidity.py --format json

# 指定回溯天数
python scripts/macro_liquidity.py --lookback-days 180 --format text
```

**环境变量**: `FRED_API_KEY` (必需，免费申请: https://fred.stlouisfed.org/docs/api/api_key.html)

**依赖**: `fredapi`, `yfinance`, `pandas`, `numpy`

### 2. `scripts/value_investing.py` — 美股价值投资分析

五因子评分模型 (总分 0-100)：

| 因子            | 权重  | 满分条件                                 |
|----------------|-------|----------------------------------------|
| ROE 持续性      | 25%   | ROE > 15% 且持续 3 年+                  |
| 负债率          | 20%   | Total Debt / Total Assets < 30%        |
| 自由现金流质量   | 20%   | FCF / Net Income > 80%                 |
| 护城河(定量代理)  | 15%   | 毛利率>40%, 营业利润率>20%, 市值>500亿    |
| 估值合理性       | 20%   | Forward PE < 25                        |

**评级**: A(80+优秀) / B(60-79良好) / C(40-59一般) / D(<40较差)

**使用方式**:
```bash
# 分析单只股票
python scripts/value_investing.py AAPL --format text

# 批量分析
python scripts/value_investing.py --symbols AAPL,MSFT,GOOGL --format text

# JSON 输出
python scripts/value_investing.py AAPL --format json
```

**依赖**: `yfinance`, `pandas`, `numpy`

### 3. `scripts/signal_report.py` — 综合信号报告

整合宏观流动性和价值投资分析的综合报告。

**使用方式**:
```bash
# 全量报告
python scripts/signal_report.py --all --symbols AAPL,MSFT

# 仅宏观流动性
python scripts/signal_report.py --macro

# 仅价值投资
python scripts/signal_report.py --value --symbols AAPL,MSFT,GOOGL

# JSON 输出
python scripts/signal_report.py --all --symbols AAPL --format json
```

## References

- [阈值说明文档](references/thresholds.md) — 两个框架的参数、阈值和评分逻辑详解

## Usage Examples

### 检测宏观流动性
用户问: "当前宏观流动性状态如何？"
→ 运行 `python scripts/macro_liquidity.py --format text`

### 分析个股
用户问: "帮我分析一下 AAPL 的基本面"
→ 运行 `python scripts/value_investing.py AAPL --format text`

### 批量筛选
用户问: "帮我筛选一下这几只股票的价值投资评分: AAPL, MSFT, GOOGL, AMZN"
→ 运行 `python scripts/value_investing.py --symbols AAPL,MSFT,GOOGL,AMZN --format text`

### 综合报告
用户问: "给我一份完整的投资信号报告"
→ 运行 `python scripts/signal_report.py --all --symbols AAPL,MSFT --format text`

## Dependencies

```bash
pip install fredapi yfinance pandas numpy
```

## Environment Variables

| 变量名         | 必需 | 说明                        |
|---------------|------|----------------------------|
| FRED_API_KEY  | 宏观分析必需 | FRED API Key，免费申请       |
