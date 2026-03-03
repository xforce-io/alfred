"""WebSocket client for communicating with an Alfred agent."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional
from urllib.parse import urlparse

import httpx
import websockets
from websockets.asyncio.client import connect as ws_connect

logger = logging.getLogger(__name__)


async def _reset_session(ws_url: str, agent_name: str, api_key: str = "") -> None:
    """Reset agent sessions via REST API before starting a new conversation."""
    parsed = urlparse(ws_url)
    base = f"http://{parsed.hostname}:{parsed.port}"
    url = f"{base}/api/agents/{agent_name}/sessions/reset"

    headers = {}
    if api_key:
        headers["x-api-key"] = api_key

    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, timeout=10)
        logger.info("Reset sessions: %d %s", resp.status_code, resp.text[:200])


async def send_session(
    ws_url: str,
    messages: list[str],
    *,
    api_key: str = "",
    timeout: float = 120.0,
    reset_first: bool = False,
    agent_name: str = "",
) -> str:
    """Connect to agent via WebSocket, send messages in sequence, return final answer.

    Args:
        ws_url: Full WebSocket URL (e.g. ws://localhost:8765/ws/chat/demo_agent)
        messages: List of messages to send in order
        api_key: Optional API key
        timeout: Max seconds to wait for each turn's response
        reset_first: If True, reset agent sessions via REST API before connecting
        agent_name: Agent name (needed for reset_first)

    Returns:
        The assembled answer text from the last message's response.
    """
    url = ws_url
    if api_key:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}api_key={api_key}"

    if reset_first and agent_name:
        await _reset_session(ws_url, agent_name, api_key)

    logger.info("Connecting to %s", url)

    async with ws_connect(url, proxy=None, ping_timeout=300, close_timeout=30) as ws:
        # Wait for welcome message
        welcome_raw = await asyncio.wait_for(ws.recv(), timeout=10)
        welcome = json.loads(welcome_raw)
        logger.debug("Welcome: %s", welcome)

        last_answer = ""

        for msg in messages:
            logger.info("Sending: %s", msg[:80])
            await ws.send(json.dumps({"message": msg}))

            # Collect streamed response until "end" event
            answer_parts: list[str] = []
            try:
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                    event = json.loads(raw)
                    evt_type = event.get("type", "")

                    if evt_type == "delta":
                        content = event.get("content", "")
                        answer_parts.append(content)
                    elif evt_type == "end":
                        break
                    elif evt_type == "error":
                        err = event.get("content", "unknown error")
                        logger.error("Agent error: %s", err)
                        answer_parts.append(f"[ERROR] {err}")
                        break
                    # skip status, history, mailbox_drain, etc.
            except asyncio.TimeoutError:
                logger.warning("Timeout waiting for response to: %s", msg[:80])
                answer_parts.append("[TIMEOUT]")

            last_answer = "".join(answer_parts)
            logger.debug("Answer (%d chars): %s", len(last_answer), last_answer[:200])

    return last_answer


async def send_new_and_query(
    ws_url: str,
    query: str,
    *,
    api_key: str = "",
    timeout: float = 120.0,
    agent_name: str = "",
) -> str:
    """Reset session via REST API, then send query and return the answer."""
    return await send_session(
        ws_url,
        [query],
        api_key=api_key,
        timeout=timeout,
        reset_first=True,
        agent_name=agent_name,
    )
