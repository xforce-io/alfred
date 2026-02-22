---
name: investment-signal
description: 投资信号检测 — 宏观流动性 + 美股价值投资 + A股市场信号 + 箱体突破
version: "2.0.0"
tags: [investing, macro, value-investing, china-market, breakout, signals, finance]
---

# Investment Signal Detector

投资信号检测技能，整合四个核心分析维度：

1. **宏观流动性监控** — 净流动性、SOFR、MOVE 指数、日元套利
2. **美股价值投资框架** — ROE、负债率、FCF、护城河、估值
3. **A股/港股市场信号** — 北向资金、两市成交额、融资融券、南向资金
4. **箱体突破检测** — 唐奇安通道突破 + 量能确认

## When to Use

- 用户询问当前宏观流动性状态、市场风险水平
- 用户要求评估某只美股的基本面质量
- 用户希望获取综合投资信号报告
- 用户提到净流动性、SOFR、MOVE、日元套利等宏观指标
- 用户提到 ROE、负债率、自由现金流、护城河、估值等价值投资指标
- 用户询问 A 股/港股市场情绪、北向资金、融资融券等
- 用户询问某只股票是否突破箱体、是否出现突破信号

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
python scripts/macro_liquidity.py --format text
python scripts/macro_liquidity.py --format json
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
python scripts/value_investing.py AAPL --format text
python scripts/value_investing.py --symbols AAPL,MSFT,GOOGL --format text
python scripts/value_investing.py AAPL --format json
```

**依赖**: `yfinance`, `pandas`, `numpy`

### 3. `scripts/china_market_signal.py` — A股/港股市场信号

四维度监控 A 股和港股通资金流向：

| 维度       | 数据源                    | 预警条件                       | 权重 |
|-----------|--------------------------|-------------------------------|------|
| 北向资金   | Tushare moneyflow_hsgt   | 单日净流出>50亿 或 连续3日净流出  | 30%  |
| 两市成交额 | Tushare index_daily      | 低于8000亿(冷) / 超2万亿(热)    | 25%  |
| 融资融券   | Tushare margin            | 余额周变化>5% 或 急降           | 25%  |
| 南向资金   | Tushare moneyflow_hsgt   | 单日净流出>30亿 或 连续3日净流出  | 20%  |

**状态输出**: 积极(Bullish) / 中性(Neutral) / 谨慎(Cautious) / 防御(Defensive)

**使用方式**:
```bash
python scripts/china_market_signal.py --format text
python scripts/china_market_signal.py --format json
python scripts/china_market_signal.py --lookback-days 30 --format text
```

**环境变量**: `TUSHARE_TOKEN` (必需，注册获取: https://tushare.pro/register)

**依赖**: `tushare`, `pandas`, `numpy`

### 4. `scripts/box_breakout.py` — 箱体突破检测

基于唐奇安通道的箱体突破检测与三因子评分模型：

| 因子       | 权重 | 说明                                     |
|-----------|------|----------------------------------------|
| 突破强度   | 40%  | 突破幅度归一化 (0-10% → 0-100)            |
| 量能放大   | 30%  | 量比归一化 (0.5x-5x → 0-100)             |
| 箱体紧度   | 30%  | 箱体宽度反转 (越窄越好，0-30% → 100-0)     |

**突破等级**: 强势突破(70+) / 有效突破(50-69) / 弱突破(30-49) / 勉强突破(<30) / 箱体内

未放量确认的突破评分×0.7折扣。

**使用方式**:
```bash
# 美股 (yfinance)
python scripts/box_breakout.py AAPL --format text
python scripts/box_breakout.py --symbols AAPL,MSFT,GOOGL --format text

# A股 (tushare)
python scripts/box_breakout.py 600519.SH --provider tushare --format text
python scripts/box_breakout.py --symbols 600519.SH,000001.SZ --provider tushare --format text

# JSON 输出
python scripts/box_breakout.py AAPL --format json
```

**环境变量**: `TUSHARE_TOKEN` (仅使用 `--provider tushare` 时必需)

**依赖**: `yfinance` (默认) 或 `tushare`, `pandas`, `numpy`

### 5. `scripts/signal_report.py` — 综合信号报告

整合所有分析模块的综合报告。

**使用方式**:
```bash
# 全量报告
python scripts/signal_report.py --all --symbols AAPL,MSFT

# 仅宏观流动性
python scripts/signal_report.py --macro

# 仅价值投资
python scripts/signal_report.py --value --symbols AAPL,MSFT,GOOGL

# 仅A股市场信号
python scripts/signal_report.py --china

# 仅箱体突破
python scripts/signal_report.py --breakout --symbols AAPL,MSFT

# 组合使用
python scripts/signal_report.py --macro --china --format text

# JSON 输出
python scripts/signal_report.py --all --symbols AAPL --format json
```

## References

- [阈值说明文档](references/thresholds.md) — 各框架的参数、阈值和评分逻辑详解

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

### A股市场信号
用户问: "A股市场情绪如何？北向资金有异动吗？"
→ 运行 `python scripts/china_market_signal.py --format text`

### 箱体突破检测
用户问: "AAPL 有没有突破箱体？"
→ 运行 `python scripts/box_breakout.py AAPL --format text`

### A股箱体突破
用户问: "帮我看看茅台有没有突破整理区间"
→ 运行 `python scripts/box_breakout.py 600519.SH --provider tushare --format text`

### 综合报告
用户问: "给我一份完整的投资信号报告"
→ 运行 `python scripts/signal_report.py --all --symbols AAPL,MSFT --format text`

## Dependencies

```bash
pip install fredapi yfinance tushare pandas numpy
```

## Environment Variables

| 变量名          | 必需                    | 说明                        |
|----------------|------------------------|-----------------------------|
| FRED_API_KEY   | 宏观分析必需             | FRED API Key，免费申请        |
| TUSHARE_TOKEN  | A股信号/tushare突破必需   | Tushare Pro Token，注册获取   |
