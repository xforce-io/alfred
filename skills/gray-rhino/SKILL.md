---
name: gray-rhino
description: 灰犀牛趋势预警——识别新兴和加速中的风险信号，聚焦未被市场充分定价的苗头
version: "2.0.0"
tags: [investment, risk, geopolitics, macro, news, gray-rhino, trend]
---

# 灰犀牛趋势预警 (Gray Rhino Trend Alert)

通过抓取全球热门财经新闻，自动聚类并与历史基线对比，识别**新兴和加速中的风险信号**——真正的灰犀牛是那些有苗头、在趋势中、但尚未被市场充分定价的风险。

## 核心理念

传统做法按报道数量排序，但报道量大的事件往往已被市场 price in。本技能改为关注：

| 信号类型 | 含义 | 价值 |
|---------|------|------|
| 🆕 新兴信号 | 首次出现的话题，尚无历史基线 | 最高——可能是下一个灰犀牛的萌芽 |
| 📈 加速趋势 | 报道频率正在加速增长 | 高——市场可能还未充分反应 |
| 📊 平稳关注 | 持续存在但无明显加速 | 中——已有一定定价 |
| 📉 趋势减弱 | 报道频率在下降 | 低——风险可能在消退 |

## When to Use

- 用户询问"最近有什么灰犀牛风险"、"有没有新兴的风险苗头"
- 用户想了解当前全球热点新闻对资产配置的影响
- 定时执行（heartbeat）生成每日风险预警
- 用户问到战争、制裁、央行政策等对投资品的影响

## Scripts

所有脚本位于 `$SKILL_DIR/scripts/`。

### 综合报告（主入口）

```bash
$D = python $SKILL_DIR/scripts/rhino_report.py

# 生成趋势预警报告（文本格式）
$D --format text

# 生成 JSON 格式报告
$D --format json

# 仅看最近 24 小时，取 Top 5
$D --max-age 24 --top 5

# 扩大历史回看窗口到 14 天
$D --lookback 14

# 按风险类别筛选
$D --category geopolitics

# 不保存快照（调试用，不影响历史基线）
$D --no-save --format text

# 仅抓取新闻（调试用）
$D --fetch-only --format json

# 详细日志
$D --format text -v
```

### 单独模块

```bash
# 仅抓取新闻
python $SKILL_DIR/scripts/news_fetcher.py --max-age 48 --format json

# 仅聚类分析（从 stdin 读取 news_fetcher 输出）
python $SKILL_DIR/scripts/news_fetcher.py --format json | \
  python $SKILL_DIR/scripts/rhino_analyzer.py --format json

# 仅资产映射（从 stdin 读取 analyzer 输出）
python $SKILL_DIR/scripts/rhino_analyzer.py --format json < news.json | \
  python $SKILL_DIR/scripts/asset_mapper.py --format text

# 查询单个事件的资产影响
python $SKILL_DIR/scripts/asset_mapper.py --event "美伊冲突升级" --format text

# 列出所有内置风险场景
python $SKILL_DIR/scripts/asset_mapper.py --list-scenarios
```

## 趋势分析原理

### 历史基线
每次运行会自动保存当日聚类快照到 `~/.alfred/gray-rhino/history/`。后续运行时与历史数据对比，计算：

- **新颖度 (novelty)**: 该话题在历史中出现过几天？首次出现得分最高
- **加速度 (acceleration)**: 近期报道频率 vs 历史平均，>1.5 为加速
- **多源印证 (source diversity)**: 多个独立信源同时关注同一话题，信号更强
- **主流惩罚**: 报道数过多的话题会被降权（已充分定价）

### 综合趋势分 = 新颖度×3 + 加速度×2 + 多源印证×1.5 - 主流惩罚

## 报告输出说明

### 信号类型
- 🆕 新兴信号——首次出现的话题
- 📈 加速趋势——报道频率加速增长
- 📊 平稳关注——持续存在
- 📉 趋势减弱——频率下降

### 资产影响方向
- ↑↑ 强烈利好 / ↑ 利好
- ↓↓ 强烈利空 / ↓ 利空
- ± 影响不确定或中性

## Agent 使用建议

作为 Agent，你在拿到趋势分析结果后应该：

1. **优先关注 🆕 和 📈 信号**：这些是市场可能尚未充分定价的风险
2. **对新兴信号做深度评估**：基于你的知识判断该苗头的发展概率和潜在影响
3. **忽略 📉 信号**：除非用户明确要求，趋势减弱的事件通常已不构成灰犀牛
4. **补充资产影响**：对未匹配到内置场景的信号，用你的分析能力推断资产影响
5. **持续积累**：提醒用户定期运行以积累历史基线，趋势分析需要时间维度

## Dependencies

```bash
pip install feedparser beautifulsoup4 requests scikit-learn
```

`scikit-learn` 为可选依赖（用于 TF-IDF 聚类），不安装时会自动降级为关键词聚类。

## 新闻源

内置以下 RSS/API 源（无需 API Key）：
- NPR, BBC, Al Jazeera（国际新闻）
- CNBC, MarketWatch（财经）
- OilPrice（能源）
- 华尔街见闻（中文财经快讯）
- 新浪财经（中文）

## References

- `$SKILL_DIR/references/risk_categories.md` — 风险类别定义与资产映射参考表
