"""Telegram bot command handlers.

Each handler receives the channel instance and chat_id, keeping the
command logic free of transport details.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..core.channel.session_resolver import ChannelSessionResolver
from ..core.runtime.control import get_local_status

if TYPE_CHECKING:
    from .telegram_channel import TelegramChannel

logger = logging.getLogger(__name__)


async def dispatch_command(
    ch: TelegramChannel, chat_id: str, text: str, raw_msg: dict
) -> None:
    """Route ``/command`` text to the appropriate handler."""
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower().split("@")[0]  # strip @botname suffix
    arg = parts[1].strip() if len(parts) > 1 else ""

    handler = _COMMANDS.get(cmd)
    if handler is not None:
        await handler(ch, chat_id, arg)
    else:
        await ch._send_message(
            chat_id, f"Unknown command: {cmd}\nType /help for available commands."
        )


async def _cmd_start(ch: TelegramChannel, chat_id: str, arg: str) -> None:
    agent_name = arg or ch._default_agent
    if not agent_name:
        await ch._send_message(
            chat_id, "Usage: /start <agent_name>\nExample: /start daily_insight"
        )
        return
    ch._bindings[chat_id] = agent_name
    ch._save_bindings()
    await ch._send_message(chat_id, f"Bound to agent: {agent_name}")


async def _cmd_ping(ch: TelegramChannel, chat_id: str, _arg: str) -> None:
    agent = ch._bindings.get(chat_id, "(none)")
    global_depth = ch._inbound_queue.qsize()
    chat_depth = 0
    if chat_id in ch._chat_queues:
        chat_depth = ch._chat_queues[chat_id].qsize()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        "pong",
        f"Agent: {agent}",
        f"Queue: global={global_depth}, chat={chat_depth}",
        f"Time: {now}",
    ]
    await ch._send_message(chat_id, "\n".join(lines))


async def _cmd_status(ch: TelegramChannel, chat_id: str, _arg: str) -> None:
    status = get_local_status(ch._user_data)
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
    await ch._send_message(chat_id, "\n".join(lines))


async def _cmd_heartbeat(ch: TelegramChannel, chat_id: str, _arg: str) -> None:
    status = get_local_status(ch._user_data)
    snapshot = status.get("snapshot") or {}
    heartbeats = snapshot.get("heartbeats", {})

    if not heartbeats:
        await ch._send_message(chat_id, "No heartbeat results available.")
        return

    lines = []
    for agent_name, hb in heartbeats.items():
        ts = hb.get("timestamp", "N/A")
        preview = hb.get("result_preview", "")
        lines.append(f"[{agent_name}] {ts}\n{preview}")

    await ch._send_message(chat_id, "\n\n".join(lines))


async def _cmd_tasks(ch: TelegramChannel, chat_id: str, _arg: str) -> None:
    status = get_local_status(ch._user_data)
    snapshot = status.get("snapshot") or {}
    task_states = snapshot.get("task_states", {})

    if not task_states:
        await ch._send_message(chat_id, "No task data available.")
        return

    lines = []
    for agent_name, ts_data in task_states.items():
        tasks = ts_data.get("tasks", []) if isinstance(ts_data, dict) else []
        lines.append(f"[{agent_name}] {len(tasks)} task(s)")
        for t in tasks[:10]:  # limit display
            title = t.get("title") or t.get("id", "?")
            state = t.get("state", "?")
            lines.append(f"  - {title} ({state})")

    await ch._send_message(chat_id, "\n".join(lines))


async def _cmd_new(ch: TelegramChannel, chat_id: str, _arg: str) -> None:
    """Clear conversation history for the current chat."""
    agent_name = ch._bindings.get(chat_id)
    if not agent_name:
        await ch._send_message(chat_id, "No agent bound. Use /start <agent_name> first.")
        return

    session_id = ChannelSessionResolver.resolve("telegram", agent_name, chat_id)
    try:
        cleared = await ch._session_manager.clear_session_history(session_id)
    except Exception as exc:
        logger.error("Failed to clear session %s: %s", session_id, exc)
        await ch._send_message(chat_id, "Failed to clear conversation. Please try again.")
        return
    if cleared:
        await ch._send_message(chat_id, "Conversation cleared. Starting fresh.")
    else:
        await ch._send_message(chat_id, "No conversation history to clear.")


async def _cmd_help(ch: TelegramChannel, chat_id: str, _arg: str) -> None:
    text = (
        "EverBot Telegram Assistant\n\n"
        "/start <agent> — Bind to an agent\n"
        "/new — Clear history and start a fresh conversation\n"
        "/ping — Health check (no LLM call)\n"
        "/status — Show daemon status\n"
        "/heartbeat — Show recent heartbeat results\n"
        "/tasks — Show task list\n"
        "/help — Show this help\n\n"
        "Send any text to chat with the bound agent (conversation history is preserved)."
    )
    await ch._send_message(chat_id, text)


_COMMANDS = {
    "/start": _cmd_start,
    "/ping": _cmd_ping,
    "/status": _cmd_status,
    "/heartbeat": _cmd_heartbeat,
    "/tasks": _cmd_tasks,
    "/new": _cmd_new,
    "/help": _cmd_help,
}
