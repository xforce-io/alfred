---
name: daily-attractor
description: 每日推送吸引子变化案例，追踪估值锚点迁移。监控全球市场资产，识别边际定价者切换、驱动维度变化的案例。
---

# Daily Attractor Monitor

每日推送吸引子变化案例，追踪估值锚点迁移。

## Description

监控全球市场资产，识别边际定价者切换、驱动维度变化的案例，每日15:00推送结构化分析报告。

## Usage

### 查看今日案例
```
用户: "今天有什么吸引子案例？"
助手: 立即生成并推送今日吸引子案例分析
```

### 手动触发推送
```
用户: "推一下吸引子"
助手: 发送今日案例到配置的 Telegram
```

### 配置监控列表
```
用户: "添加茅台到监控池"
助手: 将贵州茅台加入观察列表
```

## Features

- **自动定时推送**: 每日15:00推送案例
- **多市场覆盖**: A股、港股、美股、商品
- **结构化分析**: 边际定价者/驱动维度/估值锚点三维度
- **Telegram集成**: 直接推送到指定频道

## Configuration

在 `config/dolphin.yaml` 中配置:

```yaml
daily_attractor:
  telegram_token: "your_bot_token"
  chat_id: "your_chat_id"
  push_time: "15:00"
  markets: ["A股", "港股", "美股"]
  watchlist:
    A股:
      - {name: "贵州茅台", code: "600519", logic: "消费锚→奢侈品锚"}
      - {name: "宁德时代", code: "300750", logic: "制造业锚→能源基础设施锚"}
    美股:
      - {name: "NVDA", code: "NVDA", logic: "显卡锚→AI算力锚"}
```

## Requires

- `TELEGRAM_TOKEN` 环境变量或配置
- Python packages: python-telegram-bot, akshare, yfinance, pandas, schedule

## Schedule

- 自动任务: `0 15 * * *` (每日15:00)
