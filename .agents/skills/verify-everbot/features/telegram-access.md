# Telegram 访问控制

真实 Telegram 客户端 ↔ 线上 bot（`channels.telegram[].name: default` → demo_agent）。

## 用户入口（必须都走）

1. **授权用户发消息**：在 Telegram 里对 bot 发 `/ping`、普通文本。
2. **未授权 chat 发消息**：第二账号，或临时把本人 chat_id 从 `allowed_chat_ids` 移除后发消息（Cleanup 加回）。
3. **`/start <agent>`**：授权用户发 `/start demo_agent`、`/start ../..`、`/start ghost_agent`。

## 前置配置

`~/.alfred/config.yaml` 每个 telegram 项必须有：

```yaml
allowed_chat_ids: ["<本人 chat_id，取自 ~/.alfred/telegram_bindings_default.json>"]
```

不写 `allow_all`。

## #227 S1 deny-by-default

1. 授权 chat 发 `/ping` → bot 回复（截图）。
2. 未授权 chat 发文本 → **无任何回复**；`everbot.err` 出现一条 `Rejected Telegram update from unauthorized chat_id=<id>` WARNING；连发 3 条只产生 1 条 WARNING。
3. 未授权 chat 发一张图片 → 无回复，且 `~/.alfred/agents/demo_agent/tmp/` 没有新增下载文件（对比 `ls -t | head` 前后）。

判定：日志无 `Traceback`；授权 chat 不受影响。

## #227 S3 错误载荷

1. 授权 chat 让 agent 执行一个必然失败的动作（例如要求运行不存在的技能脚本），或在测试环境注入异常。
2. bot 的 error 消息**恰好**是 `执行失败（ref=<run_id>）`，没有 `Traceback` / `File "` / 命令行 / stderr。
3. `everbot.err` 里能用该 `run_id` 找到完整 traceback。

## #227 S5 `/start` 只绑定真实 agent

1. `/start ../..` → 回复 `Unknown agent: ../..`；`telegram_bindings_default.json` 未变。
2. `/start ghost_agent` → 同上。
3. `/start demo_agent` → 正常绑定回复。

## 不做

`allow_all: true` 线上不开；不改 coding-master bot 的 default_agent。
