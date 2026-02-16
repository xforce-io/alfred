"""TelegramChannel — mobile async assistant with multi-turn conversation.

Supports persistent multi-turn chat, heartbeat notification push, and commands.
This implementation subscribes directly to ``events.py`` (same pattern as ChatService)
rather than going through a ChannelManager (Phase 3, not yet implemented).

Binding persistence uses a JSON file at ``~/.alfred/telegram_bindings.json``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import httpx

try:
    import telegramify_markdown
    HAS_TELEGRAMIFY = True
except ImportError:
    HAS_TELEGRAMIFY = False

from ..core.channel.core_service import ChannelCoreService
from ..core.channel.models import OutboundMessage
from ..core.channel.session_resolver import ChannelSessionResolver
from ..core.runtime import events
from ..core.runtime.control import get_local_status
from ..core.session.session import SessionManager
from ..infra.user_data import UserDataManager
from ..web.services.agent_service import AgentService

logger = logging.getLogger(__name__)

TELEGRAM_MSG_LIMIT = 4096


class TelegramChannel:
    """Telegram Bot channel (long-polling, batch reply).

    Full multi-turn conversation support with persistent session history,
    heartbeat notification push, and commands.
    """

    def __init__(
        self,
        bot_token: str,
        session_manager: SessionManager,
        default_agent: str = "",
        allowed_chat_ids: Optional[List[str]] = None,
    ) -> None:
        self._bot_token = bot_token
        self._base_url = f"https://api.telegram.org/bot{bot_token}"
        self._session_manager = session_manager
        self._default_agent = default_agent
        self._allowed_chat_ids: Optional[Set[str]] = (
            set(allowed_chat_ids) if allowed_chat_ids else None
        )

        self._user_data = UserDataManager()
        self._agent_service = AgentService()
        self._core = ChannelCoreService(
            session_manager=self._session_manager,
            agent_service=self._agent_service,
            user_data=self._user_data,
        )

        # chat_id -> agent_name
        self._bindings: Dict[str, str] = {}
        self._bindings_path = self._user_data.alfred_home / "telegram_bindings.json"

        self._client: Optional[httpx.AsyncClient] = None
        self._running = False
        self._poll_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Subscribe to events, create httpx client, start polling."""
        self._load_bindings()
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
        self._running = True
        events.subscribe(self._on_background_event)
        self._poll_task = asyncio.create_task(self._polling_loop())
        logger.info(
            "TelegramChannel started, restored %d binding(s)", len(self._bindings)
        )

    async def stop(self) -> None:
        """Unsubscribe, cancel poll, close client."""
        self._running = False
        events.unsubscribe(self._on_background_event)
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        logger.info("TelegramChannel stopped")

    # ------------------------------------------------------------------
    # Event subscription (heartbeat delivery push)
    # ------------------------------------------------------------------

    async def _on_background_event(
        self, session_id: str, data: Dict[str, Any]
    ) -> None:
        """Filter heartbeat_delivery events and push to Telegram."""
        if data.get("deliver") is False:
            return
        source_type = data.get("source_type")
        if source_type != "heartbeat_delivery":
            return

        agent_name = data.get("agent_name")
        if not agent_name:
            return

        # Build notification text
        detail = str(data.get("detail") or data.get("summary") or "").strip()
        if not detail:
            return
        text = self._convert_markdown(
            f"[Heartbeat] {agent_name}\n\n{detail}"
        )

        # Push to all chats bound to this agent
        for chat_id, bound_agent in list(self._bindings.items()):
            if bound_agent == agent_name:
                await self._send_message(chat_id, text)

    # ------------------------------------------------------------------
    # Long-polling loop
    # ------------------------------------------------------------------

    async def _polling_loop(self) -> None:
        offset = 0
        while self._running:
            try:
                resp = await self._client.get(  # type: ignore[union-attr]
                    f"{self._base_url}/getUpdates",
                    params={"offset": offset, "timeout": 30},
                )
                result = resp.json()
                updates = result.get("result", [])
                for update in updates:
                    offset = update["update_id"] + 1
                    await self._handle_update(update)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Telegram polling error: %s", exc)
                await asyncio.sleep(5)

    # ------------------------------------------------------------------
    # Update routing
    # ------------------------------------------------------------------

    async def _handle_update(self, update: dict) -> None:
        msg = update.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        if not text or not chat_id:
            return

        # Access control
        if self._allowed_chat_ids is not None and chat_id not in self._allowed_chat_ids:
            logger.debug("Ignoring message from unauthorized chat_id=%s", chat_id)
            return

        if text.startswith("/"):
            await self._handle_command(chat_id, text, msg)
        else:
            await self._handle_message(chat_id, text, msg)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def _handle_command(
        self, chat_id: str, text: str, raw_msg: dict
    ) -> None:
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower().split("@")[0]  # strip @botname suffix
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/start":
            await self._cmd_start(chat_id, arg)
        elif cmd == "/status":
            await self._cmd_status(chat_id)
        elif cmd == "/heartbeat":
            await self._cmd_heartbeat(chat_id)
        elif cmd == "/tasks":
            await self._cmd_tasks(chat_id)
        elif cmd == "/help":
            await self._cmd_help(chat_id)
        else:
            await self._send_message(chat_id, f"Unknown command: {cmd}\nType /help for available commands.")

    async def _cmd_start(self, chat_id: str, agent_name: str) -> None:
        if not agent_name:
            agent_name = self._default_agent
        if not agent_name:
            await self._send_message(
                chat_id, "Usage: /start <agent_name>\nExample: /start daily_insight"
            )
            return
        self._bindings[chat_id] = agent_name
        self._save_bindings()
        await self._send_message(chat_id, f"Bound to agent: {agent_name}")

    async def _cmd_status(self, chat_id: str) -> None:
        status = get_local_status(self._user_data)
        running = status.get("running", False)
        pid = status.get("pid")
        snapshot = status.get("snapshot") or {}
        agents = snapshot.get("agents", [])
        started = snapshot.get("started_at", "N/A")

        lines = [
            f"Status: {'running' if running else 'stopped'}",
            f"PID: {pid or 'N/A'}",
            f"Started: {started}",
            f"Agents: {', '.join(agents) if agents else 'none'}",
        ]
        await self._send_message(chat_id, "\n".join(lines))

    async def _cmd_heartbeat(self, chat_id: str) -> None:
        status = get_local_status(self._user_data)
        snapshot = status.get("snapshot") or {}
        heartbeats = snapshot.get("heartbeats", {})

        if not heartbeats:
            await self._send_message(chat_id, "No heartbeat results available.")
            return

        lines = []
        for agent_name, hb in heartbeats.items():
            ts = hb.get("timestamp", "N/A")
            preview = hb.get("result_preview", "")
            lines.append(f"[{agent_name}] {ts}\n{preview}")

        await self._send_message(chat_id, "\n\n".join(lines))

    async def _cmd_tasks(self, chat_id: str) -> None:
        status = get_local_status(self._user_data)
        snapshot = status.get("snapshot") or {}
        task_states = snapshot.get("task_states", {})

        if not task_states:
            await self._send_message(chat_id, "No task data available.")
            return

        lines = []
        for agent_name, ts_data in task_states.items():
            tasks = ts_data.get("tasks", []) if isinstance(ts_data, dict) else []
            lines.append(f"[{agent_name}] {len(tasks)} task(s)")
            for t in tasks[:10]:  # limit display
                title = t.get("title") or t.get("id", "?")
                state = t.get("state", "?")
                lines.append(f"  - {title} ({state})")

        await self._send_message(chat_id, "\n".join(lines))

    async def _cmd_help(self, chat_id: str) -> None:
        text = (
            "EverBot Telegram Assistant\n\n"
            "/start <agent> — Bind to an agent\n"
            "/status — Show daemon status\n"
            "/heartbeat — Show recent heartbeat results\n"
            "/tasks — Show task list\n"
            "/help — Show this help\n\n"
            "Send any text to chat with the bound agent (conversation history is preserved)."
        )
        await self._send_message(chat_id, text)

    # ------------------------------------------------------------------
    # Chat message handling
    # ------------------------------------------------------------------

    async def _handle_message(
        self, chat_id: str, text: str, raw_msg: dict
    ) -> None:
        agent_name = self._bindings.get(chat_id)
        if not agent_name:
            await self._send_message(
                chat_id,
                "No agent bound. Use /start <agent_name> first.",
            )
            return

        session_id = ChannelSessionResolver.resolve("telegram", agent_name, chat_id)

        # Get or create agent instance
        agent = self._session_manager.get_cached_agent(session_id)
        if not agent:
            try:
                agent = await self._agent_service.create_agent_instance(agent_name)
                self._session_manager.cache_agent(session_id, agent, agent_name, "auto")
            except Exception as exc:
                logger.error("Failed to create agent %s: %s", agent_name, exc)
                await self._send_message(chat_id, f"Failed to create agent: {exc}")
                return

        # Start typing indicator
        typing_task = asyncio.create_task(self._typing_loop(chat_id))

        # Collect all deltas into a batch reply
        chunks: List[str] = []
        text_messages: List[str] = []

        async def on_event(out: OutboundMessage) -> None:
            if out.msg_type == "delta":
                chunks.append(out.content)
            elif out.msg_type == "text":
                text_messages.append(out.content)
            elif out.msg_type == "error":
                text_messages.append(f"Error: {out.content}")

        try:
            await self._core.process_message(
                agent=agent,
                agent_name=agent_name,
                session_id=session_id,
                message=text,
                on_event=on_event,
            )
        except Exception as exc:
            logger.error("process_message error for chat %s: %s", chat_id, exc)
            text_messages.append(f"Processing error: {exc}")
        finally:
            typing_task.cancel()
            try:
                await typing_task
            except asyncio.CancelledError:
                pass

        # Send reply
        full_reply = "".join(chunks).strip()
        if not full_reply and text_messages:
            full_reply = "\n".join(text_messages).strip()
        if not full_reply:
            full_reply = "(no response)"

        converted = self._convert_markdown(full_reply)
        for part in self._split_message(converted):
            await self._send_message(chat_id, part)

    # ------------------------------------------------------------------
    # Markdown conversion
    # ------------------------------------------------------------------

    @staticmethod
    def _convert_markdown(text: str) -> str:
        """Convert standard Markdown to Telegram MarkdownV2."""
        if HAS_TELEGRAMIFY:
            try:
                return telegramify_markdown.markdownify(text)
            except Exception:
                pass
        # Fallback: convert headings to bold
        lines = text.split('\n')
        result = []
        for line in lines:
            stripped = line.lstrip()
            if stripped.startswith('#'):
                heading = stripped.lstrip('#').strip()
                result.append(f"*{heading}*")
            else:
                result.append(line)
        return '\n'.join(result)

    # ------------------------------------------------------------------
    # Typing indicator
    # ------------------------------------------------------------------

    async def _typing_loop(self, chat_id: str) -> None:
        """Send typing action every 4 seconds until cancelled."""
        try:
            while True:
                await self._send_chat_action(chat_id, "typing")
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Telegram API helpers
    # ------------------------------------------------------------------

    async def _send_message(self, chat_id: str, text: str) -> None:
        if not text:
            return
        # Truncate as last resort
        if len(text) > TELEGRAM_MSG_LIMIT:
            text = text[: TELEGRAM_MSG_LIMIT - 20] + "\n\n... (truncated)"
        if self._client is None:
            return
        try:
            resp = await self._client.post(
                f"{self._base_url}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "MarkdownV2",
                },
            )
            # Fallback to plain text if Markdown fails
            data = resp.json()
            if not data.get("ok"):
                await self._client.post(
                    f"{self._base_url}/sendMessage",
                    json={"chat_id": chat_id, "text": text},
                )
        except Exception as exc:
            logger.error("Failed to send Telegram message to %s: %s", chat_id, exc)

    async def _send_chat_action(self, chat_id: str, action: str = "typing") -> None:
        if self._client is None:
            return
        try:
            await self._client.post(
                f"{self._base_url}/sendChatAction",
                json={"chat_id": chat_id, "action": action},
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Message splitting
    # ------------------------------------------------------------------

    @staticmethod
    def _split_message(text: str, limit: int = TELEGRAM_MSG_LIMIT) -> List[str]:
        """Split long text into Telegram-safe chunks.

        Strategy: split by paragraph (``\\n\\n``), then by line (``\\n``) if a
        paragraph still exceeds the limit.  Never hard-truncate mid-word.
        """
        if not text:
            return []
        if len(text) <= limit:
            return [text]

        parts: List[str] = []
        current = ""

        for paragraph in text.split("\n\n"):
            # If a single paragraph exceeds limit, split by line
            if len(paragraph) > limit:
                for line in paragraph.split("\n"):
                    if len(current) + len(line) + 1 > limit:
                        if current:
                            parts.append(current)
                            current = ""
                        # If a single line still exceeds limit, hard-split
                        while len(line) > limit:
                            parts.append(line[:limit])
                            line = line[limit:]
                        current = line
                    else:
                        current = f"{current}\n{line}" if current else line
            else:
                candidate = f"{current}\n\n{paragraph}" if current else paragraph
                if len(candidate) > limit:
                    if current:
                        parts.append(current)
                    current = paragraph
                else:
                    current = candidate

        if current:
            parts.append(current)

        return parts

    # ------------------------------------------------------------------
    # Binding persistence (JSON file)
    # ------------------------------------------------------------------

    def _load_bindings(self) -> None:
        try:
            if self._bindings_path.exists():
                raw = json.loads(self._bindings_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._bindings = {str(k): str(v) for k, v in raw.items()}
        except Exception as exc:
            logger.warning("Failed to load Telegram bindings: %s", exc)

    def _save_bindings(self) -> None:
        try:
            self._bindings_path.parent.mkdir(parents=True, exist_ok=True)
            self._bindings_path.write_text(
                json.dumps(self._bindings, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("Failed to save Telegram bindings: %s", exc)
