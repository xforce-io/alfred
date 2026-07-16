---
name: twitter-watch
description: Fetch a given X (Twitter) user's latest tweets and run a deep analysis with the configured OpenAI-compatible model route. Use when the user wants to watch/track an X account or analyze someone's recent tweets.
version: "1.0.0"
tags: [twitter, x, social, analysis, watch]
---

# Twitter Watch Skill

抓取指定 X(Twitter)用户的最新推文,并用当前 `config/models.yaml` 配置的 OpenAI-compatible 模型做**深度分析**,产出结构化中文报告。可手动调用,也可注册为每日定时任务。

## When to Use

- 用户想看某个 X 账号的最新动态 / 让你跟踪某人推特(如 "看下 Serenity 最近发了啥"、"分析下 @aleabitoreddit 的最新推文")
- 每日定时推送某账号的推文分析

默认目标账号:**@aleabitoreddit**(显示名 Serenity)。

## 前置依赖(已就绪,无需安装)

- 抓取复用同级 **`web`** 技能的 Playwright 浏览器(端口 9222)及其**已登录 X** 的持久化 profile。脚本会在 server 未起时自动 `web/server.sh &` 拉起。
- 分析用 `config/models.yaml` 的模型路由；不依赖本机特定分析 CLI。

## 用法

两步:抓取 → 分析。用 `run_command` 执行(脚本用绝对路径)。

```bash
TW="$SKILL_DIR/scripts"

# 1) 抓最新 3 条(默认 handle 见 config/.env;也可显式传 handle)
python "$TW/fetch_tweets.py" aleabitoreddit --count 3 > /tmp/tweets.json

# 2) 深度分析(配置模型),输出中文报告
python "$TW/analyze.py" --input /tmp/tweets.json

# 或一行管道
python "$TW/fetch_tweets.py" aleabitoreddit --count 3 | python "$TW/analyze.py"
```

把 `analyze.py` 的报告原样作为给用户的回复。

## Fail-fast（#153）

- `fetch_tweets.py` 失败（非零退出 / 空结果 / `SELECTOR_OR_STRUCTURE_CHANGED` / `LOGIN_REQUIRED`）时:**立即停止**,把错误原文报告给用户。
- **禁止**用 shell 现场修选择器、重写抓取脚本、反复 `curl`/tsx 调试来“抢救”同一次任务。
- 选择器/登录问题应修 skill 代码或重新登录 profile 后,再开下一次任务。

## 脚本

- `scripts/fetch_tweets.py <handle> [--count N]` — 抓最新 N 条非置顶推文,按时间倒序,输出 JSON(正文/时间/URL/互动数)。**未登录/cookie 失效时退出码 3 并提示去 `web` profile 重登,绝不伪造内容。** 空结果时带 `SELECTOR_OR_STRUCTURE_CHANGED` 以便 fail-fast。
- `scripts/analyze.py [--input f] [--model LLM_NAME] [--agent NAME] [--fast] [--timeout S]` — 读推文 JSON(默认 stdin),经 **#155 `resolve_model`** 调 OpenAI-compatible 路由产出中文报告。优先级:`--model` > agent 意图(`--agent` / 环境变量 `EVERBOT_AGENT`) > `models.yaml` 系统 fallback。milkie sidecar 会注入 `EVERBOT_AGENT`,isolated 任务默认跟随 agent 模型,不再静默打顶层 kimi default。

## 已知限制

- 时间线视图对长推文会截断(X 的 "Show more")。当前抓的是时间线可见文本,超长推文末尾可能 "原文中断";`analyze.py` 会标注而非编造。如需全文,后续可增强为逐条打开 status 链接抓取。
- 抓取依赖 X 页面 DOM 结构;X 改版时 `fetch_tweets.py` 的选择器需跟进(已兼容 `article[data-tweet-id]` 与旧 `data-testid="tweet"`)。
- X 会话 cookie 会过期;过期后在 `web` 技能的持久化 profile 里重新登录一次即可。

## 配置

见 `config/.env.template`:默认 handle、抓取条数等(复制为 `.env` 后生效,可选)。
