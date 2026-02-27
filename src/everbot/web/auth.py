"""API Key authentication for EverBot Web API.

Supports Header (X-API-Key) and query param (?api_key=xxx).
When api_key is empty or unconfigured, authentication is skipped (backward compatible).
"""

from __future__ import annotations

import logging
import secrets
from typing import Optional

from fastapi import HTTPException, Request, WebSocket, status

from ..infra.config import get_config

logger = logging.getLogger(__name__)

_warned_no_key = False


def _get_configured_api_key() -> str:
    """Return the configured api_key, or empty string if not set."""
    try:
        config = get_config()
        return str(config.get("everbot", {}).get("web", {}).get("api_key", "") or "")
    except Exception:
        return ""


def _extract_api_key(request: Request) -> Optional[str]:
    """Extract API key from header or query parameter."""
    key = request.headers.get("x-api-key")
    if key:
        return key
    key = request.query_params.get("api_key")
    if key:
        return key
    return None


async def verify_api_key(request: Request) -> None:
    """FastAPI dependency: verify API key for HTTP routes.

    - If no api_key is configured, skip authentication (backward compatible).
    - If configured, require a matching key via X-API-Key header or ?api_key= param.
    """
    global _warned_no_key
    configured_key = _get_configured_api_key()
    if not configured_key:
        if not _warned_no_key:
            logger.warning("Web API key is not configured — all requests are allowed. "
                           "Set everbot.web.api_key in config.yaml to enable authentication.")
            _warned_no_key = True
        return

    provided_key = _extract_api_key(request)
    if not provided_key or not secrets.compare_digest(provided_key, configured_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )


async def verify_ws_api_key(websocket: WebSocket) -> bool:
    """Verify API key for WebSocket connections.

    Returns True if authenticated (or auth not required).
    Returns False if authentication failed (caller should close the connection).
    """
    global _warned_no_key
    configured_key = _get_configured_api_key()
    if not configured_key:
        if not _warned_no_key:
            logger.warning("Web API key is not configured — all requests are allowed. "
                           "Set everbot.web.api_key in config.yaml to enable authentication.")
            _warned_no_key = True
        return True

    provided_key = websocket.query_params.get("api_key")
    if not provided_key or not secrets.compare_digest(provided_key, configured_key):
        return False
    return True
