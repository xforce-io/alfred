---
name: gray-rhino
description: 灰犀牛风险预警——基于热门新闻识别高概率风险事件，映射资产影响矩阵
version: "1.0.0"
tags: [investment, risk, geopolitics, macro, news, gray-rhino]
---

# 灰犀牛风险预警 (Gray Rhino Risk Alert)

通过抓取全球热门财经新闻，自动聚类识别"灰犀牛"事件（高概率、高影响、但被市场忽视的风险），并映射到主要投资品的影响方向。

## When to Use

- 用户询问"最近有什么灰犀牛风险"、"地缘政治对投资有什么影响"
- 用户想了解当前全球热点新闻对资产配置的影响
- 定时执行（heartbeat）生成每日风险预警
- 用户问到战争、制裁、央行政策等对投资品的影响

## 覆盖范围

| 类别 | 示例 |
|------|------|
| 地缘政治 | 战争/军事冲突、制裁、领土争端 |
| 宏观经济 | 央行政策、债务危机、通胀、银行风险 |
| 贸易与产业 | 贸易战、关税、供应链断裂、出口管制 |
| 科技监管 | AI监管、反垄断、加密货币政策 |
| 能源与气候 | OPEC、能源危机、极端天气 |
| 公共卫生 | 疫情爆发、食品安全 |

## Scripts

所有脚本位于 `$SKILL_DIR/scripts/`。

### 综合报告（主入口）

```bash
$D = python $SKILL_DIR/scripts/rhino_report.py

# 生成完整灰犀牛报告（文本格式）
$D --format text

# 生成 JSON 格式报告
$D --format json

# 仅看最近 24 小时，取 Top 3
$D --max-age 24 --top 3

# 按风险类别筛选
$D --category geopolitics

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

## 报告输出说明

### 风险等级
- 🔴 高度关注（5+ 条相关报道聚类）
- 🟠 密切跟踪（3-4 条相关报道）
- 🟡 初步关注（2 条相关报道）

### 资产影响方向
- ↑↑ 强烈利好 / ↑ 利好
- ↓↓ 强烈利空 / ↓ 利空
- ± 影响不确定或中性

## Agent 使用建议

作为 Agent，你在拿到聚类结果后应该：

1. **调用 `rhino_report.py --format json`** 获取结构化数据
2. **对每个聚类做灰犀牛判断**：基于你的知识评估该事件的概率、影响程度、时间窗口
3. **补充资产影响**：对于未匹配到内置场景的聚类，用你的分析能力推断资产影响
4. **生成最终报告**：结合脚本输出和你的分析，给出综合风险评估

## Dependencies

```bash
pip install feedparser beautifulsoup4 requests scikit-learn
```

`scikit-learn` 为可选依赖（用于 TF-IDF 聚类），不安装时会自动降级为关键词聚类。

## 新闻源

内置以下 RSS/API 源（无需 API Key）：
- Reuters, BBC, Al Jazeera（国际新闻）
- CNBC, MarketWatch（财经）
- OilPrice（能源）
- 华尔街见闻（中文财经快讯）
- 新浪财经（中文）

## References

- `$SKILL_DIR/references/risk_categories.md` — 风险类别定义与资产映射参考表
