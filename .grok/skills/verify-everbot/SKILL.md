---
name: verify-everbot
description: Use when keel-verify must drive EverBot on a real user path (Telegram bot, Web API/WS on 127.0.0.1:8765, milkie sidecar, `bin/everbot doctor`).
---

# verify-everbot

驾驭 **EverBot daemon**（Telegram 频道 + Web + milkie sidecar）。Issue 验收对 `features/`；只 Drive 对上的功能文件。

本机只有一套线上环境（`~/.alfred`、`~/.env.secrets`、真实 bot）。Drive 即在线上进行——**先备份，后改动，Cleanup 不回滚已被 Issue 要求的配置**。

## Launch

在**本仓库当前分支**启动，`bin/everbot` 会 source `~/.env.secrets` 并激活 `.venv`。

```bash
cd $PROJECT_ROOT
cp ~/.alfred/config.yaml ~/.alfred/config.yaml.bak_verify_<issue-no>
# 按 Issue 需要编辑 ~/.alfred/config.yaml（见对应 features/ 文件的「前置配置」）
./bin/everbot stop && ./bin/everbot start
```

- daemon pid：`~/.alfred/everbot.pid`；web pid：`~/.alfred/everbot-web.pid`（uvicorn `src.everbot.web.app:app`，`127.0.0.1:8765`）。
- 日志：`~/.alfred/logs/everbot.out`、`everbot.err`、`everbot-web.out`。
- sidecar：`pgrep -fl "index.js serve"`，每个 agent 一个 node 进程；`ps eww <pid>` 可读其环境。

启动后等 `./bin/everbot status` 报 `运行中` 且 demo_agent 出现在 `Agents:` 里再 Doctor。

## Doctor

全部成立才 Drive：

1. `./bin/everbot status` 含 `EverBot: 运行中`。
2. `./bin/everbot doctor` 无 `ERROR` 级 item（本 Issue 若故意制造 ERROR 用于验证，另记）。
3. `curl -s -o /dev/null -w '%{http_code}' -H "X-API-Key: $EVERBOT_WEB_API_KEY" http://127.0.0.1:8765/api/agents` 为 `200`。
4. `pgrep -f "milkie/demo_agent"` 有 1 个 sidecar 进程。
5. `~/.alfred/telegram_bindings_default.json` 里有本人的 chat_id（授权用户），Telegram 客户端可用。

失败则 `BLOCKED`，不要发消息给 bot。

## Drive

只打开 `features/` 里对上本 Issue `S1…Sn` 的文件（外加本次会碰到的、先前已 pass 的功能）。文件列出的**每一条用户入口都要走**：Telegram 入口要在真实 Telegram 客户端里发消息并截图/复制 bot 回复，Web 入口用 `curl` 与 `websocat`/Python `websockets` 同时覆盖 HTTP 与 WS。

需要「未授权用户」时用**第二个 Telegram 账号**或明确不在 `allowed_chat_ids` 里的 chat；没有则该 Story 用「配置里临时移除本人 chat_id 再发消息」代替，并在 notes 写明。

## Evidence

写入仓库 `.grok/verify-runs/<issue-no>/`（已在 `.gitignore`）：

| 文件 | 内容 |
|---|---|
| `doctor.txt` | Doctor 五条命令与输出 |
| `config.diff` | `diff ~/.alfred/config.yaml.bak_verify_<issue-no> ~/.alfred/config.yaml` |
| `sN-*.txt` | 命令、HTTP 状态/响应体、WS close code、日志 `rg` 结果 |
| `sN-*.png` | Telegram 对话截图（若该 Story 走 Telegram 入口） |
| `notes.md` | 入口、chat_id（只留后 4 位）、run_id、观察 |

日志证据用 `rg -n <pattern> ~/.alfred/logs/everbot.err ~/.alfred/logs/everbot.out` 原文，不要转述。

## Cleanup

- 若 Issue 要求的配置（如 `allowed_chat_ids`、`web.api_key`、`env_passthrough`）就是最终形态，**保留**，只删 `config.yaml.bak_verify_*` 之外的临时改动（如为验证临时移除的 chat_id 要加回）。
- Drive 中启动的额外进程（`websocat`、临时 python）全部结束。
- 最后 `./bin/everbot status` 必须仍为 `运行中`、demo_agent 心跳继续。
- **不得删除** `.grok/verify-runs/`。
