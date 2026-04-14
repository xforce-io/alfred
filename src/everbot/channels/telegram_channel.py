"""TelegramChannel — mobile async assistant with multi-turn conversation.

Supports persistent multi-turn chat, heartbeat notification push, and commands.
This implementation subscribes directly to ``events.py`` (same pattern as ChatService)
rather than going through a ChannelManager (Phase 3, not yet implemented).

Binding persistence uses a JSON file at ``~/.alfred/telegram_bindings.json``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union

import httpx

try:
    from telegramify_markdown import convert as tg_md_convert
    HAS_TELEGRAMIFY = True
except ImportError:
    HAS_TELEGRAMIFY = False

from ..core.channel.core_service import ChannelCoreService
from ..core.channel.models import OutboundMessage
from ..core.channel.session_resolver import ChannelSessionResolver
from ..core.runtime import events
from ..core.runtime.events import resolve_routing
from ..core.session.session import SessionManager
from ..infra.user_data import get_user_data_manager
from ..core.agent.agent_service import AgentService
from ..core.models.constants import (
    TIMEOUT_FAST,
    TIMEOUT_MEDIUM,
    QUEUE_MAX_SIZE,
    QUEUE_MAX_SIZE_PER_CHAT,
    MAX_RETRIES,
    TYPING_INDICATOR_INTERVAL,
    POLLING_ERROR_SLEEP,
    POLLING_TIMEOUT,
    POLLING_MAX_CONSECUTIVE_ERRORS,
)
from . import telegram_commands
from . import telegram_media

logger = logging.getLogger(__name__)

TELEGRAM_MSG_LIMIT = 4096


def _extract_urls(text: str, entities: list) -> list[str]:
    """Extract URLs from Telegram entities (url + text_link), deduplicated and ordered."""
    seen: set[str] = set()
    urls: list[str] = []
    for ent in entities:
        etype = ent.get("type", "")
        url = ""
        if etype == "url":
            offset = ent.get("offset", 0)
            length = ent.get("length", 0)
            url = text[offset : offset + length]
        elif etype == "text_link":
            url = ent.get("url", "")
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


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
        name: str = "",
    ) -> None:
        self._bot_token = bot_token
        self._name = name
        self._base_url = f"https://api.telegram.org/bot{bot_token}"
        self._file_base_url = f"https://api.telegram.org/file/bot{bot_token}"
        self._session_manager = session_manager
        self._default_agent = default_agent
        self._allowed_chat_ids: Optional[Set[str]] = (
            set(allowed_chat_ids) if allowed_chat_ids else None
        )

        self._user_data = get_user_data_manager()
        self._agent_service = AgentService()
        self._core = ChannelCoreService(
            session_manager=self._session_manager,
            agent_service=self._agent_service,
            user_data=self._user_data,
            skill_log_recorder_factory=lambda agent_name: self._user_data.get_skill_log_recorder(
                agent_name=agent_name,
                workspace_path=self._user_data.get_agent_dir(agent_name),
            ),
        )

        # chat_id -> agent_name
        self._bindings: Dict[str, str] = {}
        bindings_suffix = f"_{name}" if name else ""
        self._bindings_path = self._user_data.alfred_home / f"telegram_bindings{bindings_suffix}.json"

        self._client: Optional[httpx.AsyncClient] = None
        self._running = False
        self._poll_task: Optional[asyncio.Task] = None

        # Phase 1: Polling/processing decoupling
        self._inbound_queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX_SIZE)
        self._chat_queues: Dict[str, asyncio.Queue] = {}
        self._chat_workers: Dict[str, asyncio.Task] = {}
        self._dispatcher_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Subscribe to events, create httpx client, start polling."""
        self._load_bindings()
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
        self._running = True
        events.subscribe(self._on_background_event)
        self._dispatcher_task = asyncio.create_task(self._dispatcher_loop())
        self._poll_task = asyncio.create_task(self._polling_loop())
        tag = f"[{self._name}] " if self._name else ""
        logger.info(
            "%sTelegramChannel started, restored %d binding(s)", tag, len(self._bindings)
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
        if self._dispatcher_task is not None:
            self._dispatcher_task.cancel()
            try:
                await self._dispatcher_task
            except asyncio.CancelledError:
                pass
            self._dispatcher_task = None
        for task in self._chat_workers.values():
            task.cancel()
        for task in self._chat_workers.values():
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._chat_workers.clear()
        self._chat_queues.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        tag = f"[{self._name}] " if self._name else ""
        logger.info("%sTelegramChannel stopped", tag)

    # ------------------------------------------------------------------
    # Event subscription (heartbeat delivery push)
    # ------------------------------------------------------------------

    async def _on_background_event(
        self, source_session_id: str, data: Dict[str, Any]
    ) -> None:
        """Filter heartbeat_delivery / deferred_result events and push to Telegram."""
        routing = resolve_routing(data)
        if not routing.deliver:
            return
        if routing.target_channel not in (None, "telegram"):
            return

        source_type = data.get("source_type")
        if source_type not in ("heartbeat_delivery", "deferred_result", "inspector_push"):
            return

        agent_name = data.get("agent_name")
        if not agent_name:
            return

        # Build notification text
        detail = str(data.get("detail") or data.get("summary") or "").strip()
        if not detail:
            return

        if source_type == "deferred_result":
            msg_prefix = "[Deferred Result]"
        elif source_type == "inspector_push":
            msg_prefix = None
        else:
            msg_prefix = "[Heartbeat]"

        if msg_prefix:
            raw_text = f"{msg_prefix} {agent_name}\n\n{detail}"
        else:
            raw_text = detail

        text, entities = self._convert_markdown(raw_text)

        run_id = data.get("run_id") or ""
        target_chat = None
        if routing.scope == "session":
            target_chat = ChannelSessionResolver.extract_channel_session_id(
                routing.target_session_id or ""
            )
            if not target_chat:
                return

        for chat_id, bound_agent in list(self._bindings.items()):
            if bound_agent != agent_name:
                continue
            if target_chat and str(chat_id) != target_chat:
                continue
            sent = await self._send_message(chat_id, text, entities)
            # Deposit heartbeat result into channel session mailbox so the
            # next user turn sees it via "## Background Updates" prefix.
            # This replaces the old inject_history_message approach which
            # created fake assistant messages and placeholder pairs that
            # broke role alternation on restore.
            if not sent:
                logger.warning(
                    "Skip mailbox mirror for %s chat %s because Telegram delivery failed",
                    source_type, chat_id,
                )
                continue
            if source_type != "deferred_result" and hasattr(self._session_manager, "deposit_mailbox_event"):
                from ..core.models.system_event import build_system_event

                tg_session_id = ChannelSessionResolver.resolve(
                    "telegram", agent_name, chat_id,
                )
                event = build_system_event(
                    event_type="heartbeat_result",
                    source_session_id=source_session_id,
                    summary=detail[:300],
                    detail=detail,
                    dedupe_key=f"heartbeat:{agent_name}:{run_id}",
                )
                ok = await self._session_manager.deposit_mailbox_event(
                    tg_session_id, event, timeout=TIMEOUT_FAST, blocking=False,
                )
                if not ok:
                    logger.warning(
                        "Failed to deposit %s event into tg session %s mailbox",
                        source_type, tg_session_id,
                    )

    # ------------------------------------------------------------------
    # Long-polling loop
    # ------------------------------------------------------------------

    async def _polling_loop(self) -> None:
        # Drain all pending updates so they are processed immediately after restart
        # instead of waiting for the first long-poll cycle.
        offset = 0
        try:
            # Fetch all pending updates without waiting (timeout=0).
            # No offset means Telegram returns from the earliest unconfirmed update.
            resp = await self._client.get(  # type: ignore[union-attr]
                f"{self._base_url}/getUpdates",
                params={"timeout": 0},
            )
            result = resp.json()
            pending = result.get("result", [])
            for upd in pending:
                offset = upd["update_id"] + 1
                try:
                    self._inbound_queue.put_nowait(upd)
                except asyncio.QueueFull:
                    logger.warning(
                        "Inbound queue full during drain, dropping update %s",
                        upd.get("update_id"),
                    )
            if pending:
                logger.info(
                    "Drained %d pending Telegram update(s) after restart, resuming from offset %d",
                    len(pending), offset,
                )
        except Exception as exc:
            logger.warning("Failed to drain pending Telegram updates: %s", exc)

        consecutive_errors = 0
        while self._running:
            try:
                resp = await self._client.get(  # type: ignore[union-attr]
                    f"{self._base_url}/getUpdates",
                    params={"offset": offset, "timeout": POLLING_TIMEOUT},
                )
                result = resp.json()
                updates = result.get("result", [])
                consecutive_errors = 0
                for update in updates:
                    offset = update["update_id"] + 1
                    try:
                        self._inbound_queue.put_nowait(update)
                    except asyncio.QueueFull:
                        logger.warning(
                            "Inbound queue full, dropping update %s",
                            update.get("update_id"),
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                consecutive_errors += 1
                logger.error(
                    "Telegram polling error (attempt %d, %s): %r",
                    consecutive_errors,
                    type(exc).__name__,
                    exc,
                )
                if consecutive_errors >= POLLING_MAX_CONSECUTIVE_ERRORS:
                    logger.warning(
                        "Telegram polling: %d consecutive errors, recreating httpx client",
                        consecutive_errors,
                    )
                    try:
                        await self._client.aclose()
                    except Exception:
                        pass
                    self._client = httpx.AsyncClient(
                        timeout=httpx.Timeout(POLLING_TIMEOUT + 5, connect=10.0)
                    )
                    consecutive_errors = 0
                await asyncio.sleep(POLLING_ERROR_SLEEP)

    # ------------------------------------------------------------------
    # Dispatcher & per-chat workers
    # ------------------------------------------------------------------

    async def _dispatcher_loop(self) -> None:
        """Read from inbound queue and route to per-chat workers."""
        while self._running:
            try:
                update = await asyncio.wait_for(
                    self._inbound_queue.get(), timeout=TIMEOUT_FAST
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise

            msg = update.get("message") or {}
            chat_id = str((msg.get("chat") or {}).get("id", ""))
            if not chat_id:
                continue

            if chat_id not in self._chat_queues:
                self._chat_queues[chat_id] = asyncio.Queue(maxsize=QUEUE_MAX_SIZE_PER_CHAT)
            chat_q = self._chat_queues[chat_id]

            try:
                chat_q.put_nowait(update)
            except asyncio.QueueFull:
                logger.warning(
                    "Chat queue full for chat_id=%s, dropping update", chat_id
                )
                continue

            if chat_id not in self._chat_workers or self._chat_workers[chat_id].done():
                self._chat_workers[chat_id] = asyncio.create_task(
                    self._chat_worker(chat_id)
                )

    async def _chat_worker(self, chat_id: str) -> None:
        """Process messages for a single chat sequentially."""
        q = self._chat_queues.get(chat_id)
        if q is None:
            return
        while True:
            try:
                update = await asyncio.wait_for(q.get(), timeout=TIMEOUT_MEDIUM)
            except asyncio.TimeoutError:
                break
            except asyncio.CancelledError:
                raise
            try:
                await self._handle_update(update)
            except Exception as exc:
                logger.error(
                    "Error handling update for chat %s: %s", chat_id, exc
                )

    # ------------------------------------------------------------------
    # Media extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_media_text(msg: dict) -> str:
        """Extract a structured text description from a media message."""
        return telegram_media.extract_media_text(msg, _extract_urls)

    # ------------------------------------------------------------------
    # Update routing
    # ------------------------------------------------------------------

    async def _handle_update(self, update: dict) -> None:
        msg = update.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        if not chat_id:
            return

        # message can be str or list (multimodal)
        message: Union[str, list] = ""

        if text:
            # Append hidden URLs from entities (text_link) not already in text
            urls = _extract_urls(text, msg.get("entities") or [])
            for u in urls:
                if u not in text:
                    text += f"\n{u}"
            message = text
        else:
            # Unified media message handling (voice, photo, video, document, etc.)
            text = self._extract_media_text(msg)
            agent_name = self._bindings.get(chat_id, "")

            # Photo: download and build multimodal message
            if msg.get("photo") and text:
                photos = msg["photo"]
                file_id = photos[-1].get("file_id", "") if photos else ""
                local_path = await self._download_photo(file_id, agent_name)
                if local_path:
                    try:
                        with open(local_path, "rb") as f:
                            img_data = base64.b64encode(f.read()).decode("utf-8")
                        message = [
                            {"type": "text", "text": text},
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_data}"}},
                        ]
                    except Exception as exc:
                        logger.error("Failed to encode photo %s: %s", local_path, exc)
                        text += " (图片编码失败)"
                        message = text
                else:
                    text += " (图片下载失败)"
                    message = text
            else:
                message = text

            # Voice: try to download the file and append path
            if msg.get("voice") and isinstance(message, str) and message:
                local_path = await self._download_voice(
                    msg["voice"].get("file_id", ""), agent_name,
                )
                if local_path:
                    message += f" path={local_path}"
                else:
                    message += " (文件下载失败)"
            # Document: download and append path
            if msg.get("document") and isinstance(message, str) and message:
                d = msg["document"]
                local_path = await self._download_document(
                    d.get("file_id", ""),
                    d.get("file_name", ""),
                    agent_name,
                    declared_size=d.get("file_size", 0),
                )
                if local_path == telegram_media.DOWNLOAD_TOO_LARGE:
                    file_mb = d.get("file_size", 0) / (1024 * 1024)
                    message += f" (文件太大({file_mb:.1f} MB)，超过 Telegram Bot 20 MB 下载限制，无法接收)"
                elif local_path:
                    message += f" path={local_path}"
                else:
                    message += " (文件下载失败，请重新上传文件，或者确认文件有效性后再为你处理)"

        if not message:
            return

        # Access control
        if self._allowed_chat_ids is not None and chat_id not in self._allowed_chat_ids:
            logger.debug("Ignoring message from unauthorized chat_id=%s", chat_id)
            return

        # Commands only from plain text messages
        if isinstance(message, str) and message.startswith("/"):
            await self._handle_command(chat_id, message, msg)
        else:
            await self._handle_message(chat_id, message, msg)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def _handle_command(
        self, chat_id: str, text: str, raw_msg: dict
    ) -> None:
        await telegram_commands.dispatch_command(self, chat_id, text, raw_msg)

    # ------------------------------------------------------------------
    # Chat message handling
    # ------------------------------------------------------------------

    async def _handle_message(
        self, chat_id: str, message: Union[str, list], raw_msg: dict
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

        # Register Telegram skillkit so the agent can send files/photos
        self._ensure_telegram_skillkit(agent)

        # Start typing indicator
        typing_task = asyncio.create_task(self._typing_loop(chat_id))

        # Streaming state
        streaming_message_id: Optional[int] = None
        accumulated_text = ""
        last_update_time = 0.0
        min_update_interval = 1.0   # 1s throttle — Telegram rate-limits editMessageText at ~30/min
        min_streaming_chars = 15    # Min chars before starting streaming
        streaming_cursor = "▌"      # Typing cursor indicator
        streaming_failed = False

        # Collect tool calls and errors for final summary
        chunks: List[str] = []
        text_messages: List[str] = []
        tool_call_count = 0
        tool_call_failures: List[str] = []

        async def flush_streaming() -> None:
            """Flush accumulated text to Telegram if streaming is active."""
            nonlocal streaming_message_id, last_update_time, streaming_failed, min_update_interval
            if streaming_failed or not accumulated_text:
                return

            # Skip streaming for very short messages (batch send instead)
            if streaming_message_id is None and len(accumulated_text) < min_streaming_chars:
                return

            # Skip streaming for very long messages (batch send with splitting instead)
            # Telegram editMessageText has 4096 char limit and cannot split into multiple messages
            if len(accumulated_text) > TELEGRAM_MSG_LIMIT:
                logger.debug("Message too long for streaming (%d chars), switching to batch mode", len(accumulated_text))
                streaming_failed = True
                return

            current_time = asyncio.get_event_loop().time()
            if current_time - last_update_time < min_update_interval:
                return

            display_text = accumulated_text + streaming_cursor

            if streaming_message_id is None:
                # First message - send new
                try:
                    resp = await self._client.post(
                        f"{self._base_url}/sendMessage",
                        json={"chat_id": chat_id, "text": display_text},
                    )
                    data = resp.json()
                    if data.get("ok"):
                        streaming_message_id = data["result"]["message_id"]
                        last_update_time = current_time
                    else:
                        logger.warning("Failed to start streaming: %s", data.get("description"))
                        streaming_failed = True
                except Exception as exc:
                    logger.warning("Exception starting streaming: %s", exc)
                    streaming_failed = True
            else:
                # Update existing message
                success = await self._edit_message(
                    chat_id, streaming_message_id, display_text
                )
                if success:
                    last_update_time = current_time
                else:
                    # Edit failed (likely 429) — back off by doubling the interval
                    min_update_interval = min(min_update_interval * 2, 10.0)
                    last_update_time = current_time  # prevent immediate retry

        async def on_event(out: OutboundMessage) -> None:
            nonlocal tool_call_count, accumulated_text
            if out.msg_type == "delta":
                chunks.append(out.content)
                accumulated_text += out.content
                await flush_streaming()
            elif out.msg_type == "round_reset":
                # New agentic round — discard intermediate text
                chunks.clear()
                accumulated_text = ""
            elif out.msg_type == "skill":
                meta = out.metadata or {}
                status = (meta.get("status") or "").lower()
                name = meta.get("skill_name", "")

                # Hide internal resource-loading tools entirely
                if name in ("_load_resource_skill", "_load_skill_resource"):
                    return

                if status in ("processing", "running"):
                    tool_call_count += 1
                elif status == "failed":
                    output = meta.get("skill_output") or ""
                    output_brief = output[:80] + "..." if len(output) > 80 else output
                    tool_call_failures.append(f"  {name}: {output_brief}")
            elif out.msg_type == "text":
                text_messages.append(out.content)
            elif out.msg_type == "error":
                text_messages.append(f"Error: {out.content}")

        try:
            await self._core.process_message(
                agent=agent,
                agent_name=agent_name,
                session_id=session_id,
                message=message,
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

        # Build final reply
        full_reply = "".join(chunks).strip()
        if text_messages:
            extra = "\n".join(text_messages).strip()
            if extra:
                full_reply = f"{full_reply}\n\n{extra}" if full_reply else extra
        if not full_reply:
            full_reply = "(no response)"

        # Append tool call summary (compact, at the end)
        if tool_call_count > 0 or tool_call_failures:
            summary_parts = []
            if tool_call_failures:
                failed = len(tool_call_failures)
                ok = tool_call_count - failed
                summary_parts.append(f"🔧 {ok} ok, {failed} failed:")
                summary_parts.extend(tool_call_failures)
            else:
                summary_parts.append(f"🔧 {tool_call_count} commands executed")
            full_reply = f"{full_reply}\n\n{chr(10).join(summary_parts)}"

        # If streaming worked, update with final text (remove cursor, apply formatting)
        if streaming_message_id and not streaming_failed:
            final_text, final_entities = self._convert_markdown(full_reply[:TELEGRAM_MSG_LIMIT])
            await self._edit_message(chat_id, streaming_message_id, final_text, final_entities)
            return

        # If streaming started but failed mid-way, delete the unformatted
        # streaming message so the batch send below doesn't duplicate it.
        if streaming_message_id and streaming_failed:
            try:
                await self._client.post(
                    f"{self._base_url}/deleteMessage",
                    json={"chat_id": chat_id, "message_id": streaming_message_id},
                )
            except Exception as exc:
                logger.debug("Failed to delete streaming message: %s", exc)

        # Fallback: batch send (original behavior)
        converted_text, converted_entities = self._convert_markdown(full_reply)
        sent_any = False
        parts = self._split_message(converted_text)
        if len(parts) <= 1:
            for part in parts:
                success = await self._send_message(chat_id, part, converted_entities)
                if success:
                    sent_any = True
        else:
            search_from = 0
            for part in parts:
                part_start = converted_text.find(part, search_from)
                if part_start < 0:
                    part_start = search_from
                utf16_offset = self._utf16_len(converted_text[:part_start])
                utf16_length = self._utf16_len(part)
                part_entities = self._slice_entities(
                    converted_entities, utf16_offset, utf16_length,
                )
                success = await self._send_message(
                    chat_id, part, part_entities or None,
                )
                if success:
                    sent_any = True
                search_from = part_start + len(part)

        if not sent_any:
            fallback = full_reply[:200]
            await self._send_plain_message(
                chat_id, f"[delivery error] {fallback}"
            )

    # ------------------------------------------------------------------
    # Markdown conversion
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_tables(text: str) -> str:
        """Convert pre-formatted tables (``+----`` separators) to standard
        Markdown tables so that ``telegramify_markdown`` can recognise them.

        LLMs sometimes produce tables with ``----+----`` separators or
        data rows without leading ``|``.  This normalises the most common
        variants into ``| --- | --- |`` format.
        """
        import re

        lines = text.split("\n")
        out: list[str] = []
        i = 0
        while i < len(lines):
            line = lines[i]
            # Detect a separator line like "----+-----+-----" or "---+---"
            if re.match(r"^[\s\-+]+$", line) and "+" in line and "-" in line:
                # Look back for a header row and forward for data rows
                # Convert this separator to |---|---|
                cols = [seg for seg in re.split(r"\+", line) if seg.strip("-").strip("-") is not None]
                ncols = len(cols)
                md_sep = "| " + " | ".join(["---"] * ncols) + " |"

                # Fix the header row (line before separator) if it uses bare pipes
                if out and "|" in out[-1] and not out[-1].strip().startswith("|"):
                    cells = [c.strip() for c in out[-1].split("|")]
                    out[-1] = "| " + " | ".join(cells) + " |"

                out.append(md_sep)
                i += 1

                # Fix subsequent data rows that use bare pipes
                while i < len(lines):
                    row = lines[i]
                    if "|" in row and not row.strip().startswith("|"):
                        # Check it looks like a data row (has similar # of pipes)
                        cells = [c.strip() for c in row.split("|")]
                        if len(cells) >= ncols - 1:
                            out.append("| " + " | ".join(cells) + " |")
                            i += 1
                            continue
                    break
                continue
            out.append(line)
            i += 1
        return "\n".join(out)

    @staticmethod
    def _convert_markdown(text: str) -> tuple:
        """Convert standard Markdown to Telegram text + entities.

        Returns (text, entities_list) where entities_list is a list of
        dicts ready for the Telegram API, or None if conversion failed.
        """
        if HAS_TELEGRAMIFY:
            try:
                plain, entities = tg_md_convert(text)
                entity_dicts = [e.to_dict() for e in entities]
                if entity_dicts:
                    return plain, entity_dicts
                # No entities produced — try normalising tables first
                if "+" in text and "---" in text:
                    normalised = TelegramChannel._normalize_tables(text)
                    if normalised != text:
                        plain2, entities2 = tg_md_convert(normalised)
                        entity_dicts2 = [e.to_dict() for e in entities2]
                        if entity_dicts2:
                            return plain2, entity_dicts2
                return plain, entity_dicts
            except Exception as exc:
                logger.warning("telegramify_markdown conversion failed: %s", exc)
        # Fallback: strip heading markers, no entities
        lines = text.split('\n')
        result = []
        for line in lines:
            stripped = line.lstrip()
            if stripped.startswith('#'):
                heading = stripped.lstrip('#').strip()
                result.append(heading)
            else:
                result.append(line)
        return '\n'.join(result), None

    @staticmethod
    def _utf16_len(text: str) -> int:
        """Return the length of *text* in UTF-16 code units (what Telegram uses)."""
        return len(text.encode("utf-16-le")) // 2

    @staticmethod
    def _slice_entities(
        entities: Optional[list], part_offset: int, part_length: int,
    ) -> list:
        """Extract and re-offset entities that fall within a text slice.

        All values (part_offset, part_length, entity offsets/lengths) must be
        in UTF-16 code units to match the Telegram Bot API convention.
        """
        if not entities:
            return []
        part_end = part_offset + part_length
        result = []
        for ent in entities:
            e_offset = ent.get("offset", 0)
            e_length = ent.get("length", 0)
            e_end = e_offset + e_length
            # Skip entities entirely outside this part
            if e_end <= part_offset or e_offset >= part_end:
                continue
            # Clamp to part boundaries
            new_offset = max(e_offset, part_offset) - part_offset
            new_end = min(e_end, part_end) - part_offset
            new_length = new_end - new_offset
            if new_length <= 0:
                continue
            sliced = dict(ent)
            sliced["offset"] = new_offset
            sliced["length"] = new_length
            result.append(sliced)
        return result

    # ------------------------------------------------------------------
    # Telegram Skillkit registration
    # ------------------------------------------------------------------

    def _ensure_telegram_skillkit(self, agent: Any) -> None:
        """Register TelegramSkillkit on the agent if not already present."""
        gs = getattr(agent, "global_skills", None)
        if gs is None:
            return
        installed = getattr(gs, "installedSkillset", None)
        if installed is None:
            return
        if installed.hasSkill("_tg_send_file"):
            return

        from .telegram_skillkit import TelegramSkillkit

        tg_skillkit = TelegramSkillkit(bot_token=self._bot_token)
        installed.addSkillkit(tg_skillkit)
        logger.info("Registered TelegramSkillkit on agent")

    # ------------------------------------------------------------------
    # Typing indicator
    # ------------------------------------------------------------------

    async def _typing_loop(self, chat_id: str) -> None:
        """Send typing action every 4 seconds until cancelled."""
        try:
            while True:
                await self._send_chat_action(chat_id, "typing")
                await asyncio.sleep(TYPING_INDICATOR_INTERVAL)
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Telegram API helpers
    # ------------------------------------------------------------------

    async def _send_message(self, chat_id: str, text: str, entities: Optional[list] = None) -> bool:
        """Send message with entities (preferred) or plain text, with retry.

        Returns True if the message was delivered successfully.
        """
        if not text:
            return True
        if len(text) > TELEGRAM_MSG_LIMIT:
            text = text[: TELEGRAM_MSG_LIMIT - 20] + "\n\n... (truncated)"
            entities = None  # entities offsets would be invalid after truncation
        if self._client is None:
            return False

        for attempt in range(MAX_RETRIES):
            try:
                payload: Dict[str, Any] = {"chat_id": chat_id, "text": text}
                if entities:
                    payload["entities"] = entities
                resp = await self._client.post(
                    f"{self._base_url}/sendMessage", json=payload,
                )
                data = resp.json()
                if data.get("ok"):
                    return True

                # Fallback to plain text if entities send fails
                if entities:
                    logger.warning(
                        "sendMessage with entities failed: %s — retrying without entities",
                        data.get("description", "unknown"),
                    )
                    resp = await self._client.post(
                        f"{self._base_url}/sendMessage",
                        json={"chat_id": chat_id, "text": text},
                    )
                    data = resp.json()
                    if data.get("ok"):
                        return True

                logger.warning(
                    "sendMessage failed for chat %s (attempt %d/%d): %s",
                    chat_id, attempt + 1, MAX_RETRIES,
                    data.get("description", "unknown"),
                )
            except Exception as exc:
                logger.warning(
                    "sendMessage exception for chat %s (attempt %d/%d): [%s] %r",
                    chat_id, attempt + 1, MAX_RETRIES, type(exc).__name__, exc,
                )

            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(2 ** attempt)  # 1s, 2s

        logger.error("Failed to send Telegram message to %s after %d retries", chat_id, MAX_RETRIES)
        return False

    async def _send_plain_message(self, chat_id: str, text: str) -> bool:
        """Send a plain text message (no Markdown), with retry. Last-resort fallback."""
        if not text:
            return True
        if len(text) > TELEGRAM_MSG_LIMIT:
            text = text[: TELEGRAM_MSG_LIMIT - 20] + "\n\n... (truncated)"
        if self._client is None:
            return False

        for attempt in range(MAX_RETRIES):
            try:
                resp = await self._client.post(
                    f"{self._base_url}/sendMessage",
                    json={"chat_id": chat_id, "text": text},
                )
                data = resp.json()
                if data.get("ok"):
                    return True
                logger.warning(
                    "sendPlainMessage failed for chat %s (attempt %d/%d): %s",
                    chat_id, attempt + 1, MAX_RETRIES,
                    data.get("description", "unknown"),
                )
            except Exception as exc:
                logger.warning(
                    "sendPlainMessage exception for chat %s (attempt %d/%d): [%s] %r",
                    chat_id, attempt + 1, MAX_RETRIES, type(exc).__name__, exc,
                )
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(2 ** attempt)
        return False

    async def _edit_message(self, chat_id: str, message_id: int, text: str,
                            entities: list | None = None) -> bool:
        """Edit an existing message. Returns True if successful."""
        if not text:
            return True
        if len(text) > TELEGRAM_MSG_LIMIT:
            text = text[: TELEGRAM_MSG_LIMIT - 20] + "\n\n... (truncated)"
        if self._client is None:
            return False

        payload: dict = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if entities:
            payload["entities"] = entities

        try:
            resp = await self._client.post(
                f"{self._base_url}/editMessageText", json=payload,
            )
            data = resp.json()
            if data.get("ok"):
                return True
            # Log but don't retry - edit conflicts are expected in streaming
            logger.debug("editMessageText failed: %s", data.get("description", "unknown"))
        except Exception as exc:
            logger.debug("editMessageText exception: [%s] %r", type(exc).__name__, exc)
        return False

    # ------------------------------------------------------------------
    # Download helpers — delegated to telegram_media module
    # ------------------------------------------------------------------

    @staticmethod
    def _sanitize_filename(raw: str) -> str:
        return telegram_media.sanitize_filename(raw)

    def _safe_local_path(self, target_dir: Path, filename: str) -> Optional[Path]:
        return telegram_media.safe_local_path(target_dir, filename)

    async def _download_document(
        self, file_id: str, file_name: str, agent_name: str, *, declared_size: int = 0,
    ) -> Optional[str]:
        """Download a Telegram document file and return the local path."""
        target_dir = self._user_data.get_agent_tmp_dir(agent_name)
        return await telegram_media.download_document(
            self._client, self._base_url, self._file_base_url,
            file_id, file_name, target_dir, declared_size=declared_size,
        )

    async def _download_voice(self, file_id: str, agent_name: str) -> Optional[str]:
        """Download a Telegram voice file and return the local path."""
        target_dir = self._user_data.get_agent_tmp_dir(agent_name)
        return await telegram_media.download_voice(
            self._client, self._base_url, self._file_base_url,
            file_id, target_dir,
        )

    async def _download_photo(self, file_id: str, agent_name: str) -> Optional[str]:
        """Download a Telegram photo and return the local path."""
        target_dir = self._user_data.get_agent_tmp_dir(agent_name)
        return await telegram_media.download_photo(
            self._client, self._base_url, self._file_base_url,
            file_id, target_dir,
        )

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
