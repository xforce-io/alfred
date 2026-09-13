"""API Key authentication for EverBot Web API.

Supports Header (X-API-Key) and query param (?api_key=xxx).

Deny by default (#227): ``everbot.web.api_key`` is required. The web app
refuses to start when it is missing, and any request arriving without a
configured key is rejected instead of being let through.
"""

from __future__ import annotations

import logging
import secrets
from typing import Optional

from fastapi import HTTPException, Request, WebSocket, status

from ..infra.config import expand_env_refs, get_config

logger = logging.getLogger(__name__)

API_KEY_REQUIRED_MESSAGE = "everbot.web.api_key is required"


def _get_configured_api_key() -> str:
    """Return the configured api_key with ``${ENV}`` references expanded.

    Raises when the config cannot be read or a referenced variable is
    unset: a broken config must never degrade into "no key configured"
    or into the literal ``${NAME}`` text acting as the key.
    """
    config = get_config()
    raw = str(config.get("everbot", {}).get("web", {}).get("api_key", "") or "")
    return expand_env_refs(raw).strip()


def require_api_key() -> str:
    """Return the configured key or raise; called at app startup."""
    key = _get_configured_api_key()
    if not key:
        raise RuntimeError(API_KEY_REQUIRED_MESSAGE)
    return key


def _extract_api_key(request: Request) -> Optional[str]:
    """Extract API key from header or query parameter."""
    key = request.headers.get("x-api-key")
    if key:
        return key
    key = request.query_params.get("api_key")
    if key:
        return key
    return None


def _matches(provided: Optional[str], configured: str) -> bool:
    return bool(provided) and bool(configured) and secrets.compare_digest(provided, configured)


async def verify_api_key(request: Request) -> None:
    """FastAPI dependency: verify API key for HTTP routes.

    Requires a matching key via X-API-Key header or ?api_key= param.
    A missing configured key is a server misconfiguration → 503.
    """
    configured_key = _get_configured_api_key()
    if not configured_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=API_KEY_REQUIRED_MESSAGE,
        )
    if not _matches(_extract_api_key(request), configured_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )


async def verify_ws_api_key(websocket: WebSocket) -> bool:
    """Verify API key for WebSocket connections.

    Returns True only when a key is configured and the ``api_key`` query
    parameter matches it; the caller closes the connection otherwise.
    """
    configured_key = _get_configured_api_key()
    if not configured_key:
        logger.error("Rejecting WebSocket: %s", API_KEY_REQUIRED_MESSAGE)
        return False
    return _matches(websocket.query_params.get("api_key"), configured_key)
