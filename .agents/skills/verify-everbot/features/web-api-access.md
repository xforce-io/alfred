# Web API / WebSocket 访问

`http://127.0.0.1:8765`，uvicorn `src.everbot.web.app:app`。前端 `static/js/app.js` 从 `localStorage.everbot_api_key` 读 key，发 `X-API-Key` / `?api_key=`。

## 用户入口（必须都走）

1. **HTTP**：`curl` 带 / 不带 `X-API-Key`，带 / 不带 `Origin`。
2. **WebSocket**：`websocat` 或 Python `websockets` 连 `/ws/chat/<agent>?api_key=…`，观察 close code。
3. **浏览器 UI**：打开 `http://127.0.0.1:8765/?api_key=<key>`，页面能列出 agent（同源 WS 正常建立）。

## 前置配置

```yaml
everbot:
  web:
    api_key: "${EVERBOT_WEB_API_KEY}"
```

`EVERBOT_WEB_API_KEY` 导出在 `~/.env.secrets`。

## #227 S2 api_key 必填 + Origin guard

1. **拒启**：临时把 `api_key` 置空（或 unset 变量）后 `./bin/everbot start`，`everbot-web.out` 出现 `everbot.web.api_key is required`（或未解析变量的 `ValueError`），8765 无监听。恢复配置再起。
2. 无 key：`GET /api/agents` → `401`。
3. 有 key：`GET /api/agents` → `200`。
4. 跨站 POST：`curl -X POST -H "X-API-Key: $K" -H "Origin: https://evil.example" /api/agents/demo_agent/sessions/reset` → `403 {"detail":"Origin not allowed"}`。
5. 同源 POST：`-H "Origin: http://127.0.0.1:8765"` → 非 403。
6. 跨站 WS：`Origin: https://evil.example` 握手 → 被拒（真实 uvicorn 为 HTTP 403 握手拒绝；ASGI 侧 close code 4003 只在 TestClient 可见），`everbot-web.out` 有 `Rejected websocket request from disallowed origin`。
7. 同源/无 Origin WS，带 key → 收到欢迎消息。
8. 浏览器 UI 入口能用。

## #227 S5 未知 agent

1. `GET /api/agents/ghost_agent/sessions`（带 key）→ `404 Unknown agent: ghost_agent`。
2. `POST /api/agents/..%2F..%2Fetc/sessions/reset`（带 key）→ `404`；`~/.alfred/agents/` 之外没有新目录。
3. WS `/ws/chat/ghost_agent?api_key=…` → 握手被拒 HTTP 403（ASGI close 4004）。
4. WS 无 key → 握手被拒 HTTP 403（ASGI close 4001）。

## 不做

不改前端；不开 CORS 通配。
