# Sidecar 环境变量白名单

每个 agent 一个 `node …/milkie/dist/cli/index.js serve` 进程；技能脚本（`skills/*/scripts`）由它派生，环境即它的环境。

## 用户入口（必须都走）

1. **进程环境**：`ps eww $(pgrep -f "milkie/demo_agent")`（或 `/proc`-less 的 macOS 用 `ps -Eww`），读出 sidecar 的实际 env 键。
2. **技能真跑**：通过 Telegram 让 demo_agent 用 `web` 技能搜索一次（需要 `TAVILY_API_KEY`），或触发一条依赖 `env_passthrough` 变量的 routine。

## 前置配置

```yaml
everbot:
  agents:
    demo_agent:
      env_passthrough: [TAVILY_API_KEY, TUSHARE_TOKEN, FRED_API_KEY, KAIRO_PROVIDER, XAI_API_KEY]
```

变量来源：`skills/web`（TAVILY）、`skills/invest`（TUSHARE/FRED）、`kairo` CLI（KAIRO_PROVIDER=grok → grok CLI，XAI_API_KEY 备用）。

## #227 S4

1. sidecar env 键集合 ⊆ `SIDECAR_BASE_ENV ∪ {EVERBOT_AGENT, ALFRED_AGENT, OPENAI_API_KEY, OPENAI_BASE_URL, EVERBOT_SKILL_MANIFEST…} ∪ env_passthrough`。
2. **不存在** `TELEGRAM_ALFRED_BOT_TOKEN`、`TELEGRAM_CM_ALPHA_BOT_TOKEN`、`DEEPSEEK_API_KEY`（若非当前模型 cloud）、`GITLAB_API_TOKEN`、`KWEAVER_PASSWORD` 等 daemon 私有变量。
3. `everbot.err` 启动段无 `env_passthrough … not set` WARNING（有则说明名字写错/变量未导出）。
4. Telegram 让 agent 做一次 web 搜索 → 有结果，不报 `TAVILY_API_KEY` 缺失。

判定：键集合原文存证（值一律脱敏，只留键名）。

## 不做

不给 `_reflector`/coding-master 加 passthrough，除非它们的技能明确要。
