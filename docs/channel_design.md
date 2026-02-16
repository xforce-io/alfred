# Channel 抽象层设计文档

## 1. 背景与目标

EverBot 当前的前端通信逻辑直接耦合在 `ChatService` 中，仅支持 Web/WebSocket 渠道。`ChatService` 同时承担了两类职责：

1. **传输层** — WebSocket 连接管理、消息序列化、流式推送
2. **业务层** — Session 加锁/加载/持久化、turn 执行、mailbox compose、事件路由

为支持 Telegram、Discord 等多前端渠道，需要将这两类职责解耦：

- 将业务层提取为 transport-agnostic 的 **ChannelCoreService**
- 将 WebSocket 部分重构为 **WebChannel**（Channel 的一种实现）
- 新增 **ChannelManager** 统一管理所有 Channel 的注册、生命周期和事件路由

### 参考项目

[OpenClaw](~/dev/github/openclaw) — 成熟的多渠道 AI 助手平台（Telegram、WhatsApp、Discord、Slack 等十余个渠道）。本设计借鉴了其中两个实用模式：

- **Capabilities 声明** — 每个 Channel 声明自身能力（是否支持流式、消息长度限制等），ChannelManager 据此决定投递策略，无需 isinstance 判断
- **回调式事件投递** — 与 OpenClaw 的 `onPartialReply` / `onToolResult` 回调一致，`ChannelCoreService` 通过 `on_event` 回调向 Channel 逐事件投递

未采纳的模式（当前阶段不需要）：组合式多 Adapter 拆分、多账户支持、外部插件发现机制。

### 设计约束

- **核心层零修改** — `TurnOrchestrator`、`SessionManager`、`events.py`、`HeartbeatRunner` 不做任何改动
- **遵循现有 Protocol 模式** — 同 `ContextStrategy`，Channel 使用 `typing.Protocol` 定义接口
- **流式 vs 批量由 Channel 自决** — WebSocket 流式推送 delta；Telegram 等渠道攒完后一次性发送
- **HeartbeatRunner 不改动** — 跨 Channel 通知由 ChannelManager 通过 events 广播实现

---

## 2. Channel Protocol

### 2.1 消息模型

```python
# src/everbot/core/channel/models.py

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

@dataclass
class InboundMessage:
    """从前端渠道接收到的用户消息。"""
    channel_type: str          # "web", "telegram", "discord"
    channel_session_id: str    # Channel 内部的会话标识（如 Telegram chat_id）
    agent_name: str            # 目标 agent
    text: str                  # 用户输入文本
    user_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    # metadata 可存放渠道特有信息，如 telegram message_id、reply_to 等

@dataclass
class OutboundMessage:
    """向前端渠道发送的消息。"""
    channel_session_id: str    # 目标 Channel session
    content: str               # 文本内容
    msg_type: str = "text"     # "text" | "delta" | "status" | "error" | "end"
    metadata: Dict[str, Any] = field(default_factory=dict)
    # metadata 可携带 tool_call 信息、skill 信息等，由各 Channel 自行解读
```

### 2.2 Channel Capabilities

```python
# src/everbot/core/channel/models.py（续）

@dataclass
class ChannelCapabilities:
    """声明 Channel 的能力，供 ChannelManager 决定投递策略。"""
    streaming: bool = False        # 是否支持流式 delta 推送
    text_chunk_limit: int = 0      # 单条消息最大字符数，0 表示无限制
```

各 Channel 声明自身能力，`ChannelManager` 据此做投递决策（如：跳过向非流式渠道发 delta），无需 isinstance 判断具体类型。

| Channel | streaming | text_chunk_limit |
|---|---|---|
| web | True | 0 (无限制) |
| telegram | False | 4096 |
| discord | False | 2000 |

### 2.3 Channel 接口

```python
# src/everbot/core/channel/protocol.py

from __future__ import annotations
from typing import Protocol, Optional, Callable, Awaitable

from .models import ChannelCapabilities, OutboundMessage

class Channel(Protocol):
    """前端渠道协议。每种渠道（Web、Telegram、Discord）实现此接口。"""

    @property
    def channel_type(self) -> str:
        """渠道类型标识，如 "web"、"telegram"、"discord"。"""
        ...

    @property
    def capabilities(self) -> ChannelCapabilities:
        """声明此渠道的能力。"""
        ...

    async def start(self) -> None:
        """启动渠道（建立连接、开始轮询等）。"""
        ...

    async def stop(self) -> None:
        """停止渠道并清理资源。"""
        ...

    async def send(self, message: OutboundMessage) -> None:
        """向指定 channel_session 发送一条消息。"""
        ...

    async def broadcast_to_agent(self, agent_name: str, message: OutboundMessage) -> None:
        """向某 agent 的所有活跃 channel session 广播消息。"""
        ...

    def is_connected(self, channel_session_id: str) -> bool:
        """检查指定 channel session 是否仍然活跃。"""
        ...
```

### 2.4 设计说明

- `Channel` 不感知 `SessionManager`、`TurnOrchestrator` 等核心概念，它只负责消息的收发
- 用户消息到达后，Channel 将其包装为 `InboundMessage` 交给 `ChannelCoreService` 处理
- `ChannelCoreService` 处理完毕后，通过回调将 `OutboundMessage` 序列投递回 Channel
- Channel 决定如何呈现这些消息（WebSocket 逐个 delta 推送 vs Telegram 攒完后发一条）
- `ChannelCapabilities` 让 ChannelManager 可以按能力过滤事件（如不向非流式渠道发 delta），避免各 Channel 各自重复实现过滤逻辑

---

## 3. ChannelCoreService

### 3.1 职责

从 `ChatService` 中提取 transport-agnostic 的消息处理核心：

| 现 ChatService 职责 | 归属 |
|---|---|
| WebSocket accept/close/receive/send_json | → WebChannel |
| 连接注册/注销 (_register/_unregister) | → WebChannel |
| Session 加锁（双层锁） | → ChannelCoreService |
| Session 加载/恢复/持久化 | → ChannelCoreService |
| 消息 compose（mailbox prefix） | → ChannelCoreService |
| TurnOrchestrator.run_turn() | → ChannelCoreService |
| Timeline 记录 | → ChannelCoreService |
| 事件流 → WebSocket JSON | → WebChannel (via callback) |
| 事件路由 (_on_background_event) | → ChannelManager |
| Agent 实例管理 (AgentService) | → ChannelCoreService |

### 3.2 接口设计

```python
# src/everbot/core/channel/core_service.py

from __future__ import annotations
import asyncio
from typing import AsyncIterator, Callable, Awaitable, Optional
from .models import InboundMessage, OutboundMessage

class ChannelCoreService:
    """Transport-agnostic 消息处理核心。

    所有 Channel 共享同一个 ChannelCoreService 实例。
    """

    def __init__(
        self,
        session_manager: SessionManager,
        agent_service: AgentService,
        user_data: UserDataManager,
    ):
        self.session_manager = session_manager
        self.agent_service = agent_service
        self.user_data = user_data
        self._orchestrator = TurnOrchestrator(CHAT_POLICY)
        self._primary_context_strategy = PrimaryContextStrategy()
        self._runtime_deps = RuntimeDeps(
            load_workspace_instructions=self._load_workspace_instructions,
        )

    async def process_message(
        self,
        agent: Any,
        agent_name: str,
        session_id: str,
        message: str,
        on_event: Callable[[OutboundMessage], Awaitable[None]],
    ) -> None:
        """处理一条用户消息。

        完整流程：session 加锁 → 加载 → compose → run_turn → 持久化 → 释放锁。
        turn 执行过程中产生的事件通过 on_event 回调逐个投递给调用方。

        调用方（Channel）负责 agent 实例的获取和 session_id 的解析，
        ChannelCoreService 只关注 turn 执行流水线。

        Args:
            agent: Agent 实例（由调用方通过 AgentService 创建/缓存）
            agent_name: Agent 名称
            session_id: 已解析的 EverBot session_id
            message: 用户输入文本
            on_event: 事件回调。Channel 实现在此回调中将 OutboundMessage
                      转换为渠道特有格式（如 WebSocket JSON、Telegram sendMessage）。
        """
        ...

    async def load_history(self, session_id: str) -> list[dict]:
        """加载 session 历史消息，用于连接初始化时发送。"""
        ...

    async def drain_mailbox(self, session_id: str) -> list[str]:
        """获取并清除 mailbox 中的待展示事件，返回可展示内容列表。"""
        ...

    async def set_session_variable(self, session_id: str, key: str, value: str) -> None:
        """在 session variables 中存储一个键值对（用于 Channel 持久化元数据）。"""
        ...

    async def list_sessions_by_prefix(self, prefix: str) -> list[tuple[str, dict]]:
        """列出指定前缀的所有 session，返回 (session_id, variables) 列表。

        用于 Channel 启动时恢复状态（如 TelegramChannel 恢复 chat_id 绑定）。
        """
        ...
```

### 3.3 process_message 内部流程

```
process_message(agent, agent_name, session_id, message, on_event)
    │
    │      注：session_id 解析和 agent 实例管理由调用方（Channel）负责
    │
    ├── 1. acquire_session(session_id)           ← 双层锁，与现有逻辑相同
    │
    ├── 2. load_session(session_id)
    │      restore_to_agent(agent, session_data)
    │
    ├── 3. compose_turn_message(text, session_data, agent_name)
    │      → via PrimaryContextStrategy.build_message()
    │
    ├── 4. orchestrator.run_turn(agent, message, ...)
    │      │
    │      ├── LLM_DELTA   → on_event(OutboundMessage(msg_type="delta", ...))
    │      ├── TOOL_CALL   → on_event(OutboundMessage(msg_type="text", metadata={tool_call: ...}))
    │      ├── TOOL_OUTPUT → on_event(OutboundMessage(msg_type="text", metadata={tool_output: ...}))
    │      ├── STATUS      → on_event(OutboundMessage(msg_type="status", ...))
    │      ├── TURN_ERROR  → on_event(OutboundMessage(msg_type="error", ...))
    │      └── TURN_COMPLETE → on_event(OutboundMessage(msg_type="end", ...))
    │
    ├── 5. save_session(session_id, agent)
    │
    ├── 6. ack_mailbox_events(session_id, ack_ids)
    │
    └── 7. release_session(session_id)           ← finally 中释放双层锁
```

### 3.4 流式 vs 批量

`ChannelCoreService` 始终逐事件回调 `on_event`，由各 Channel 决定处理策略：

- **WebChannel**：每个 `on_event` 立即 `websocket.send_json()`（流式）
- **TelegramChannel**：累积所有 delta，在收到 `msg_type="end"` 时一次性 `sendMessage()`（批量）

---

## 4. ChannelManager

### 4.1 职责

- 管理所有 Channel 实例的注册与生命周期
- 替代 `ChatService` 成为 `events.py` 的唯一订阅者
- 将 background event 路由到正确的 Channel

### 4.2 接口设计

```python
# src/everbot/core/channel/manager.py

from __future__ import annotations
from typing import Dict, Any

from ...core.runtime.events import subscribe, unsubscribe
from .protocol import Channel

class ChannelManager:
    """管理所有 Channel 的注册、生命周期与事件路由。"""

    def __init__(self, core_service: ChannelCoreService):
        self.core_service = core_service
        self._channels: Dict[str, Channel] = {}  # channel_type -> Channel

    def register(self, channel: Channel) -> None:
        """注册一个 Channel 实例。一个 channel_type 只允许注册一个。"""
        self._channels[channel.channel_type] = channel

    def unregister(self, channel_type: str) -> None:
        """注销一个 Channel。"""
        self._channels.pop(channel_type, None)

    async def start_all(self) -> None:
        """启动所有已注册的 Channel，并订阅全局事件。"""
        subscribe(self._on_background_event)
        for channel in self._channels.values():
            await channel.start()

    async def stop_all(self) -> None:
        """停止所有 Channel 并取消事件订阅。"""
        unsubscribe(self._on_background_event)
        for channel in self._channels.values():
            await channel.stop()

    async def _on_background_event(self, session_id: str, data: Dict[str, Any]) -> None:
        """全局事件处理器。

        替代原 ChatService._on_background_event()，将事件分发给所有 Channel。
        保留原有的 deliver 过滤、scope 路由、idle gate、heartbeat 过滤等逻辑。
        """
        if data.get("deliver") is False:
            return

        # Heartbeat body content 只通过 mailbox drain 展示，不直接推送
        source_type = data.get("source_type")
        if source_type == "heartbeat":
            event_type = str(data.get("type") or "")
            if event_type in {"message", "delta", "skill"} or "_progress" in data:
                return

        bypass_idle_gate = source_type in {"time_reminder", "heartbeat", "heartbeat_delivery"}

        scope = data.get("scope", "session")
        agent_name = data.get("agent_name")

        if scope == "agent" and agent_name:
            # Agent-scope idle gate: 按 agent 节流广播频率
            now = time.time()
            last_t = self._last_agent_broadcast.get(agent_name, 0)
            if (not bypass_idle_gate) and (now - last_t < 20):
                return

            msg_type = data.get("type", "")
            for channel in self._channels.values():
                if msg_type == "delta" and not channel.capabilities.streaming:
                    continue
                message = self._event_to_outbound(data)
                await channel.broadcast_to_agent(agent_name, message)
            self._last_agent_broadcast[agent_name] = now
        else:
            # Session 级事件 → 根据 session_id 前缀确定 channel_type，精准投递
            channel_type = ChannelSessionResolver.extract_channel_type(session_id)
            channel = self._channels.get(channel_type)
            if channel:
                message = self._event_to_outbound(data, channel_session_id=session_id)
                await channel.send(message)

    @staticmethod
    def _event_to_outbound(data: Dict[str, Any], channel_session_id: str = "") -> OutboundMessage:
        """将 events.py 的 envelope 转换为 OutboundMessage。"""
        ...
```

### 4.3 事件路由流程

```
HeartbeatRunner
    │
    └── events.emit(session_id, data, scope="agent", agent_name="my_agent")
              │
              └── ChannelManager._on_background_event()
                        │
                        ├── scope="agent" → 遍历所有 Channel.broadcast_to_agent()
                        │     ├── WebChannel: 推送到所有 WebSocket 连接
                        │     └── TelegramChannel: 推送到所有 Telegram chat
                        │
                        └── scope="session" → 根据 session_id 前缀定位 Channel
                              └── Channel.send() → 精准投递到特定 session
```

---

## 5. ChannelSessionResolver

### 5.1 Session 命名空间

每个 Channel 类型拥有独立的 session 命名空间，避免跨渠道冲突：

| Channel | Primary Session ID | Sub Session ID |
|---|---|---|
| web | `web_session_{agent}` | `web_session_{agent}__{ts}_{id}` |
| telegram | `tg_session_{agent}__{chat_id}` | — |
| discord | `discord_session_{agent}__{channel_id}` | — |

### 5.2 接口设计

非 web 渠道使用 `__`（双下划线）作为 agent_name 与 channel_session_id 的分隔符，确保含下划线的 agent 名称（如 `daily_insight`）可被无歧义解析。

```python
# src/everbot/core/channel/session_resolver.py

from __future__ import annotations

class ChannelSessionResolver:
    """Channel session ID ↔ EverBot session ID 映射。

    非 web 渠道格式: {prefix}{agent_name}__{channel_session_id}
    示例: tg_session_daily_insight__12345
    """

    _SEP = "__"

    _PREFIX_MAP = {
        "web": "web_session_",
        "telegram": "tg_session_",
        "discord": "discord_session_",
    }

    @classmethod
    def resolve(cls, channel_type: str, agent_name: str, channel_session_id: str) -> str:
        """将 Channel 侧的会话标识映射为 EverBot session_id。

        Returns:
            EverBot session_id，如 "tg_session_daily_insight__12345"
        """
        prefix = cls._PREFIX_MAP.get(channel_type, f"{channel_type}_session_")
        if channel_type == "web":
            if not channel_session_id:
                return f"{prefix}{agent_name}"
            return channel_session_id
        return f"{prefix}{agent_name}{cls._SEP}{channel_session_id}"

    @classmethod
    def extract_channel_type(cls, session_id: str) -> str:
        """从 session_id 前缀推断 channel_type。"""
        for channel_type, prefix in cls._PREFIX_MAP.items():
            if session_id.startswith(prefix):
                return channel_type
        return "web"

    @classmethod
    def extract_agent_name(cls, session_id: str) -> str:
        """从非 web session_id 中提取 agent_name。

        "tg_session_daily_insight__12345" → "daily_insight"
        """
        for ch_type, prefix in cls._PREFIX_MAP.items():
            if ch_type == "web":
                continue
            if session_id.startswith(prefix):
                remainder = session_id[len(prefix):]
                idx = remainder.find(cls._SEP)
                return remainder[:idx] if idx > 0 else remainder
        return ""

    @classmethod
    def extract_channel_session_id(cls, session_id: str) -> str:
        """从非 web session_id 中提取 channel_session_id。

        "tg_session_daily_insight__12345" → "12345"
        """
        for ch_type, prefix in cls._PREFIX_MAP.items():
            if ch_type == "web":
                continue
            if session_id.startswith(prefix):
                remainder = session_id[len(prefix):]
                idx = remainder.find(cls._SEP)
                return remainder[idx + len(cls._SEP):] if idx > 0 else ""
        return ""
```

### 5.3 与 SessionManager 的关系

`ChannelSessionResolver` **不修改** `SessionManager`。它只是一个纯函数式的 ID 映射层：

```
Channel.inbound_message(chat_id="12345", agent="my_agent")
    │
    └── ChannelSessionResolver.resolve("telegram", "my_agent", "12345")
          → "tg_session_my_agent__12345"
              │
              └── SessionManager.load_session("tg_session_my_agent__12345")
                  → 正常走现有 session 持久化/锁定逻辑
```

`SessionManager` 已有的 `session_type` 字段（"primary"、"heartbeat"、"job"、"sub"）不变。所有 Channel 的 chat session 均使用 `session_type="primary"`，通过 session_id 前缀区分来源渠道。

---

## 6. WebChannel 重构

### 6.1 拆分方案

```
现在：
    ChatService = 连接管理 + 消息处理 + 事件路由

重构后：
    WebChannel         = WebSocket 连接管理 + 消息格式转换
    ChannelCoreService = Session 管理 + Turn 执行 + Mailbox compose
    ChannelManager     = 事件路由
```

### 6.2 WebChannel 实现

```python
# src/everbot/web/channels/web_channel.py

from __future__ import annotations
import asyncio
from typing import Dict, Optional, Set, Tuple
from fastapi import WebSocket

from ...core.channel.protocol import Channel
from ...core.channel.models import InboundMessage, OutboundMessage
from ...core.channel.core_service import ChannelCoreService
from ...core.channel.session_resolver import ChannelSessionResolver

class WebChannel:
    """WebSocket 前端渠道实现。

    管理 WebSocket 连接，将 WebSocket 消息转换为 InboundMessage，
    将 OutboundMessage 转换为 WebSocket JSON 帧。
    """

    channel_type = "web"
    capabilities = ChannelCapabilities(streaming=True, text_chunk_limit=0)

    def __init__(self, core_service: ChannelCoreService):
        self._core = core_service
        # session_id -> WebSocket
        self._connections: Dict[str, WebSocket] = {}
        # agent_name -> set of (session_id, WebSocket)
        self._agent_connections: Dict[str, Set[Tuple[str, WebSocket]]] = {}

    async def start(self) -> None:
        """WebChannel 无需主动启动，连接由 FastAPI 路由驱动。"""
        pass

    async def stop(self) -> None:
        """关闭所有活跃连接。"""
        for ws in list(self._connections.values()):
            try:
                await ws.close()
            except Exception:
                pass
        self._connections.clear()
        self._agent_connections.clear()

    async def send(self, message: OutboundMessage) -> None:
        """向指定 session 的 WebSocket 发送消息。"""
        ws = self._connections.get(message.channel_session_id)
        if not ws:
            return
        await ws.send_json(self._to_ws_payload(message))

    async def broadcast_to_agent(self, agent_name: str, message: OutboundMessage) -> None:
        """向某 agent 的所有 WebSocket 连接广播。"""
        targets = list(self._agent_connections.get(agent_name, set()))
        for sid, ws in targets:
            try:
                await ws.send_json(self._to_ws_payload(message))
            except Exception:
                self._connections.pop(sid, None)
                self._agent_connections.get(agent_name, set()).discard((sid, ws))

    def is_connected(self, channel_session_id: str) -> bool:
        return channel_session_id in self._connections

    # ---- WebSocket 连接生命周期（由 FastAPI 路由调用）----

    async def handle_websocket(self, websocket: WebSocket, agent_name: str,
                                requested_session_id: Optional[str] = None) -> None:
        """处理一个完整的 WebSocket 会话。

        此方法保留了原 ChatService.handle_chat_session() 的完整逻辑，
        但 turn 执行部分委托给 ChannelCoreService.process_message()。
        """
        await websocket.accept()
        session_id = ChannelSessionResolver.resolve("web", agent_name, requested_session_id or "")
        self._register(session_id, agent_name, websocket)

        try:
            # 1. 初始化：加载 history，发送给客户端
            history = await self._core.load_history(session_id)
            if history:
                await websocket.send_json({
                    "type": "history",
                    "session_id": session_id,
                    "messages": history,
                })

            # 2. Drain mailbox
            drain_events = await self._core.drain_mailbox(session_id)
            if drain_events:
                await websocket.send_json({
                    "type": "mailbox_drain",
                    "events": drain_events,
                })

            # 3. 消息循环
            while True:
                data = await websocket.receive_json()
                action = data.get("action")
                if action == "stop":
                    # ... 中断逻辑保持不变
                    continue

                text = data.get("message", "").strip()
                if not text:
                    continue

                inbound = InboundMessage(
                    channel_type="web",
                    channel_session_id=session_id,
                    agent_name=agent_name,
                    text=text,
                )

                # 流式回调：每个事件立即推送
                async def on_event(out: OutboundMessage) -> None:
                    await websocket.send_json(self._to_ws_payload(out))

                await self._core.process_message(inbound, on_event)

        except Exception:
            pass
        finally:
            self._unregister(session_id, agent_name)
            try:
                await websocket.close()
            except Exception:
                pass

    # ---- 内部方法 ----

    def _register(self, session_id: str, agent_name: str, ws: WebSocket) -> None:
        self._connections[session_id] = ws
        if agent_name not in self._agent_connections:
            self._agent_connections[agent_name] = set()
        self._agent_connections[agent_name].add((session_id, ws))

    def _unregister(self, session_id: str, agent_name: str) -> None:
        ws = self._connections.pop(session_id, None)
        if agent_name in self._agent_connections:
            self._agent_connections[agent_name].discard((session_id, ws))
            if not self._agent_connections[agent_name]:
                del self._agent_connections[agent_name]

    @staticmethod
    def _to_ws_payload(msg: OutboundMessage) -> dict:
        """将 OutboundMessage 转换为现有 WebSocket 协议 JSON。

        保持与现有前端 app.js 的兼容性。
        """
        if msg.msg_type == "delta":
            return {"type": "delta", "content": msg.content}
        elif msg.msg_type == "status":
            return {"type": "status", "content": msg.content}
        elif msg.msg_type == "error":
            return {"type": "error", "content": msg.content}
        elif msg.msg_type == "end":
            return {"type": "end"}
        elif msg.msg_type == "text":
            payload = {"type": "message", "role": "assistant", "content": msg.content}
            # 透传 tool_call / skill 等 metadata
            if "ws_payload" in msg.metadata:
                payload = msg.metadata["ws_payload"]
            return payload
        return {"type": "message", "role": "assistant", "content": msg.content}
```

### 6.3 FastAPI 路由变更

```python
# 重构前
@app.websocket("/ws/chat/{agent_name}")
async def ws_chat(websocket, agent_name):
    await chat_service.handle_chat_session(websocket, agent_name)

# 重构后
@app.websocket("/ws/chat/{agent_name}")
async def ws_chat(websocket, agent_name):
    await web_channel.handle_websocket(websocket, agent_name)
```

---

## 7. TelegramChannel 示例实现

### 7.1 设计要点

- 使用 Telegram Bot API 长轮询（getUpdates）
- 非流式输出：攒完整轮回复后一次性 sendMessage
- 每个 Telegram chat_id 对应一个 EverBot session

### 7.2 实现草案

```python
# src/everbot/channels/telegram_channel.py

from __future__ import annotations
import asyncio
import logging
from typing import Dict, Set
import httpx

from ..core.channel.protocol import Channel
from ..core.channel.models import InboundMessage, OutboundMessage
from ..core.channel.core_service import ChannelCoreService
from ..core.channel.session_resolver import ChannelSessionResolver

logger = logging.getLogger(__name__)

class TelegramChannel:
    """Telegram Bot 渠道实现（长轮询模式）。"""

    channel_type = "telegram"
    capabilities = ChannelCapabilities(streaming=False, text_chunk_limit=4096)

    def __init__(self, core_service: ChannelCoreService, bot_token: str,
                 default_agent: str = ""):
        self._core = core_service
        self._bot_token = bot_token
        self._base_url = f"https://api.telegram.org/bot{bot_token}"
        self._default_agent = default_agent
        self._running = False
        self._poll_task: asyncio.Task | None = None
        # chat_id -> agent_name（运行时缓存，持久化在 session variables 中）
        self._chat_agent_map: Dict[str, str] = {}
        # 活跃 chat_id 集合
        self._active_chats: Set[str] = set()

    async def start(self) -> None:
        await self._restore_chat_agent_map()
        self._running = True
        self._poll_task = asyncio.create_task(self._polling_loop())
        logger.info("TelegramChannel started, restored %d chat bindings",
                     len(self._chat_agent_map))

    async def stop(self) -> None:
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        logger.info("TelegramChannel stopped")

    async def send(self, message: OutboundMessage) -> None:
        """发送消息到指定 Telegram chat。"""
        chat_id = self._extract_chat_id(message.channel_session_id)
        if not chat_id:
            return
        if message.msg_type in ("delta", "status"):
            return  # Telegram 不支持流式，忽略 delta/status
        if message.msg_type == "end":
            return  # end 信号不需要发送
        await self._send_message(chat_id, message.content)

    async def broadcast_to_agent(self, agent_name: str, message: OutboundMessage) -> None:
        """向所有关联该 agent 的 Telegram chat 广播。"""
        for chat_id, mapped_agent in self._chat_agent_map.items():
            if mapped_agent == agent_name:
                await self._send_message(chat_id, message.content)

    def is_connected(self, channel_session_id: str) -> bool:
        chat_id = self._extract_chat_id(channel_session_id)
        return chat_id in self._active_chats

    # ---- 长轮询 ----

    async def _polling_loop(self) -> None:
        offset = 0
        async with httpx.AsyncClient(timeout=60) as client:
            while self._running:
                try:
                    resp = await client.get(
                        f"{self._base_url}/getUpdates",
                        params={"offset": offset, "timeout": 30},
                    )
                    updates = resp.json().get("result", [])
                    for update in updates:
                        offset = update["update_id"] + 1
                        await self._handle_update(update)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error("Telegram polling error: %s", e)
                    await asyncio.sleep(5)

    async def _handle_update(self, update: dict) -> None:
        msg = update.get("message", {})
        text = msg.get("text", "").strip()
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if not text or not chat_id:
            return

        self._active_chats.add(chat_id)

        # /start 命令：绑定 agent
        if text.startswith("/start"):
            parts = text.split(maxsplit=1)
            agent_name = parts[1].strip() if len(parts) > 1 else ""
            if not agent_name:
                agent_name = self._default_agent
            if agent_name:
                self._chat_agent_map[chat_id] = agent_name
                # 持久化绑定到 session variables
                session_id = ChannelSessionResolver.resolve("telegram", agent_name, chat_id)
                await self._core.set_session_variable(
                    session_id, "tg_chat_id", chat_id,
                )
                await self._send_message(chat_id, f"已绑定到 Agent: {agent_name}")
            else:
                await self._send_message(chat_id, "请使用 /start <agent_name> 绑定 Agent")
            return

        agent_name = self._chat_agent_map.get(chat_id)
        if not agent_name:
            await self._send_message(chat_id, "请先使用 /start <agent_name> 绑定 Agent")
            return

        session_id = ChannelSessionResolver.resolve("telegram", agent_name, chat_id)

        inbound = InboundMessage(
            channel_type="telegram",
            channel_session_id=session_id,
            agent_name=agent_name,
            text=text,
            user_id=str(msg.get("from", {}).get("id", "")),
        )

        # 批量模式：收集所有 delta，最后一次性发送
        chunks: list[str] = []

        async def on_event(out: OutboundMessage) -> None:
            if out.msg_type == "delta":
                chunks.append(out.content)
            elif out.msg_type == "end":
                full_reply = "".join(chunks).strip()
                if full_reply:
                    await self._send_message(chat_id, full_reply)
            elif out.msg_type == "error":
                await self._send_message(chat_id, f"Error: {out.content}")

        await self._core.process_message(inbound, on_event)

    # ---- Telegram API ----

    async def _send_message(self, chat_id: str, text: str) -> None:
        """调用 Telegram sendMessage API。"""
        if not text:
            return
        limit = self.capabilities.text_chunk_limit
        if limit and len(text) > limit:
            text = text[:limit - 6] + "\n..."
        async with httpx.AsyncClient(timeout=30) as client:
            await client.post(
                f"{self._base_url}/sendMessage",
                json={"chat_id": chat_id, "text": text},
            )

    async def _restore_chat_agent_map(self) -> None:
        """启动时从已有的 tg_session_* 文件恢复 chat_id → agent 映射。

        扫描 SessionManager 中所有 tg_session_ 前缀的 session，
        从 session variables 中读取 tg_chat_id，重建内存映射。
        """
        sessions = await self._core.list_sessions_by_prefix("tg_session_")
        for session_id, variables in sessions:
            chat_id = variables.get("tg_chat_id", "")
            if not chat_id:
                # 从 session_id 解析 fallback
                chat_id = self._extract_chat_id(session_id)
            if chat_id:
                # 从 session_id 解析 agent_name
                agent_name = self._extract_agent_name(session_id)
                if agent_name:
                    self._chat_agent_map[chat_id] = agent_name
                    self._active_chats.add(chat_id)

    @staticmethod
    def _extract_agent_name(session_id: str) -> str:
        """从 tg_session_{agent}__{chat_id} 提取 agent_name。"""
        return ChannelSessionResolver.extract_agent_name(session_id)

    @staticmethod
    def _extract_chat_id(session_id: str) -> str:
        """从 tg_session_{agent}__{chat_id} 提取 chat_id。"""
        return ChannelSessionResolver.extract_channel_session_id(session_id)
```

---

## 7.5 TelegramChannel B+ 实现

### 产品定位

Telegram 是"移动端异步助手"——支持完整的多轮对话、心跳通知推送和命令。Session history 通过 `ChannelCoreService` 持久化，跨会话保留上下文。

功能：
- **多轮对话**：发送任意文本，通过 `ChannelCoreService.process_message()` 处理，对话历史持久化保存
- **心跳通知推送**：HeartbeatRunner 产生的 `heartbeat_delivery` 事件自动推送到绑定的 Telegram chat
- **命令**：`/start <agent>`, `/status`, `/heartbeat`, `/tasks`, `/help`

### 架构决策

1. **直接订阅 `events.py`**：与 ChatService 相同模式，不依赖 ChannelManager（Phase 3 尚未实现）。ChannelManager 到来后统一迁移。
2. **共享 daemon 的 SessionManager**：TelegramChannel 运行在 daemon 进程内，复用 `daemon.session_manager`，避免重复实例。
3. **进程内 pub/sub**：HeartbeatRunner 的 `events.emit()` 能直接到达 TelegramChannel。Web 进程的 ChatService 独立订阅，互不影响。

### 绑定持久化

chat_id → agent 绑定存储在 `~/.alfred/telegram_bindings.json`。选择独立 JSON 文件而非 `ChannelCoreService.set_session_variable()` 是因为后者尚未完整实现，独立文件更简单，后续可迁移。

### 配置结构

```yaml
everbot:
  channels:
    telegram:
      enabled: false
      bot_token: "${TELEGRAM_BOT_TOKEN}"  # 支持环境变量引用
      default_agent: "daily_insight"
      # allowed_chat_ids: ["123456789"]   # 可选白名单
```

### 实现文件

- `src/everbot/channels/__init__.py` — 包初始化
- `src/everbot/channels/telegram_channel.py` — 核心实现
- `src/everbot/cli/daemon.py` — 在 daemon 启动/停止时管理 TelegramChannel 生命周期

---

## 8. 事件投递模型

### 8.1 现有模型

```
HeartbeatRunner → events.emit() → ChatService._on_background_event()
                                         ↓
                                  WebSocket.send_json()
```

### 8.2 重构后模型

```
HeartbeatRunner → events.emit()    （不变）
                      ↓
              ChannelManager._on_background_event()
                      ↓
              ┌───────┴────────┐
              ↓                ↓
        WebChannel       TelegramChannel
        .send() /        .send() /
        .broadcast()     .broadcast()
```

### 8.3 关键点

- `events.py` **零修改**：仍然是简单的 pub/sub
- `ChannelManager` 替代 `ChatService` 成为唯一订阅者
- idle gate 逻辑从 `ChatService` 移至 `ChannelManager`（或各 Channel 自行实现）
- `HeartbeatRunner` **零修改**：仍然调用 `events.emit()`

---

## 9. 配置模型

### 9.1 config.yaml 新增 channels 段

```yaml
everbot:
  enabled: true

  channels:
    web:
      enabled: true                   # 默认启用

    telegram:
      enabled: false                  # 默认禁用
      bot_token: "${TELEGRAM_BOT_TOKEN}"  # 支持环境变量引用
      default_agent: "daily_insight"  # 可选：未绑定时的默认 agent

    discord:
      enabled: false
      bot_token: "${DISCORD_BOT_TOKEN}"
      default_agent: ""

  runtime:
    job_retention_days: 7
    # ... 现有配置不变

  agents:
    # ... 现有配置不变
```

### 9.2 Channel 初始化流程

```python
# 应用启动时
config = load_config()
core_service = ChannelCoreService(session_manager, agent_service, user_data)
channel_manager = ChannelManager(core_service)

channels_config = config.get("everbot", {}).get("channels", {})

# Web Channel（始终注册）
if channels_config.get("web", {}).get("enabled", True):
    web_channel = WebChannel(core_service)
    channel_manager.register(web_channel)

# Telegram Channel（按配置注册）
tg_config = channels_config.get("telegram", {})
if tg_config.get("enabled", False):
    bot_token = resolve_env(tg_config["bot_token"])
    tg_channel = TelegramChannel(core_service, bot_token)
    channel_manager.register(tg_channel)

# 启动所有 Channel
await channel_manager.start_all()
```

---

## 10. 文件组织

### 10.1 新增目录结构

```
src/everbot/
├── core/
│   ├── channel/                     # 新增：Channel 抽象层
│   │   ├── __init__.py
│   │   ├── protocol.py              # Channel Protocol 定义
│   │   ├── models.py                # InboundMessage, OutboundMessage
│   │   ├── core_service.py          # ChannelCoreService
│   │   ├── manager.py               # ChannelManager
│   │   └── session_resolver.py      # ChannelSessionResolver
│   ├── runtime/                     # 不变
│   │   ├── context_strategy.py
│   │   ├── events.py
│   │   ├── heartbeat.py
│   │   ├── mailbox.py
│   │   └── turn_orchestrator.py
│   └── session/                     # 不变
│       └── session.py
├── channels/                        # 新增：各渠道具体实现
│   ├── __init__.py
│   ├── telegram_channel.py          # TelegramChannel
│   └── discord_channel.py           # DiscordChannel（未来）
├── web/                             # 现有，重构
│   ├── channels/
│   │   └── web_channel.py           # WebChannel（从 ChatService 拆出）
│   ├── services/
│   │   ├── chat_service.py          # 保留但标记 deprecated，逐步迁移
│   │   └── agent_service.py         # 不变
│   └── app.py                       # 路由层，对接 WebChannel
└── ...
```

### 10.2 模块依赖关系

```
                ┌─────────────────────┐
                │   core/channel/     │
                │   protocol.py       │  ← 纯接口，无依赖
                │   models.py         │
                └─────────┬───────────┘
                          │
          ┌───────────────┼───────────────┐
          ↓               ↓               ↓
   core_service.py   manager.py    session_resolver.py
          │               │
          │    依赖 core_service
          │               │
          ↓               ↓
   ┌──────────────────────────────────┐
   │ 现有核心层（不修改）              │
   │ SessionManager, TurnOrchestrator │
   │ events.py, mailbox.py            │
   └──────────────────────────────────┘
          ↑               ↑
          │               │
   WebChannel      TelegramChannel
   (web/)          (channels/)
```

---

## 11. 分阶段实施计划

### Phase 1：定义抽象层（无功能变更）

**目标**：建立 Channel 抽象层的代码骨架，不改变现有行为。

1. 创建 `core/channel/` 目录
2. 实现 `models.py`（InboundMessage、OutboundMessage）
3. 实现 `protocol.py`（Channel Protocol）
4. 实现 `session_resolver.py`（ChannelSessionResolver）
5. 编写单元测试

**验证**：现有功能完全不受影响，新代码仅是新增文件。

### Phase 2：提取 ChannelCoreService

**目标**：从 `ChatService._process_message()` 中提取业务逻辑到 `ChannelCoreService`。

1. 实现 `core_service.py`，将 session 管理、turn 执行、mailbox compose 逻辑迁移过来
2. `ChatService` 内部调用 `ChannelCoreService.process_message()` 替代自身逻辑
3. 保持所有外部接口（WebSocket 协议、FastAPI 路由）不变

**验证**：WebSocket 聊天行为完全不变，是内部重构。

### Phase 3：实现 WebChannel + ChannelManager

**目标**：将 WebSocket 连接管理拆分为 WebChannel，引入 ChannelManager。

1. 实现 `WebChannel`（连接管理 + 消息格式转换）
2. 实现 `ChannelManager`（Channel 注册 + 事件路由）
3. 将 `ChatService._on_background_event()` 移至 `ChannelManager`
4. 更新 `app.py` 路由，指向 WebChannel
5. `ChatService` 标记为 deprecated

**验证**：WebSocket 聊天 + 心跳事件推送行为完全不变。

### Phase 4：新增 TelegramChannel

**目标**：实现第一个非 Web 渠道，验证 Channel 抽象层的扩展性。

1. 实现 `TelegramChannel`（长轮询 + 批量发送）
2. 新增 config.yaml `channels.telegram` 配置段
3. 在 `app.py` 或 daemon 启动时按配置注册 TelegramChannel
4. 端到端测试

**验证**：通过 Telegram Bot 与 EverBot agent 对话，心跳结果推送到 Telegram。

---

## 12. 风险与注意事项

### 12.1 向后兼容

- WebSocket 协议（JSON 帧格式）不变，前端 `app.js` 无需修改
- Session ID 命名规范不变（`web_session_` 前缀），现有 session 数据可直接迁移
- `HeartbeatRunner` 不修改，仍通过 `events.emit()` 投递事件

### 12.2 并发安全

- `ChannelCoreService` 复用现有双层锁机制（in-process asyncio.Lock + cross-process flock）
- 多个 Channel 可能同时向同一 agent 的 session 发消息，但锁机制保证串行执行
- Telegram 长轮询消息按序处理，不存在并发问题

### 12.3 Agent 实例管理

- 当前 `AgentService` 和 agent 缓存绑定在 `ChatService` 中
- Phase 2 需要将 agent 实例管理迁移到 `ChannelCoreService`，确保多 Channel 共享同一 agent 缓存

### 12.4 Telegram 绑定持久化

- chat_id → agent 的绑定关系存储在 session 的 `variables.tg_chat_id` 中
- TelegramChannel 启动时通过 `list_sessions_by_prefix("tg_session_")` 扫描恢复
- 这依赖 `ChannelCoreService` 新增的两个辅助方法（`set_session_variable`、`list_sessions_by_prefix`），底层委托给现有 `SessionManager`，无需修改 SessionManager 本身

### 12.5 测试策略

- Phase 1-2 通过现有 WebSocket 测试验证无回归
- Phase 3 新增 WebChannel 单元测试 + 集成测试
- Phase 4 TelegramChannel 使用 mock HTTP server 测试
