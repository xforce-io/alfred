"""Origin guard for state-changing HTTP requests and WebSocket handshakes (#227).

CORS only governs what a browser lets a page *read*; it does not stop a
cross-site page from opening a WebSocket or firing a simple POST at
``127.0.0.1:8765``. This middleware closes that gap:

- Requests without an ``Origin`` header (curl, SDKs, same-site navigations)
  pass through: they are not cross-site browser requests.
- Requests carrying an ``Origin`` must match either the request's own
  origin (same-origin UI) or an entry in the allow-list.
- Safe HTTP methods (GET/HEAD/OPTIONS) are not guarded: they have no side
  effects and are already behind the API key.
"""

from __future__ import annotations

import logging
from typing import Callable, Iterable, Optional

logger = logging.getLogger(__name__)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
WS_CLOSE_ORIGIN_NOT_ALLOWED = 4003


def _header(scope: dict, name: bytes) -> Optional[str]:
    for key, value in scope.get("headers") or ():
        if key == name:
            return value.decode("latin-1")
    return None


def _same_origin(scope: dict) -> Optional[str]:
    host = _header(scope, b"host")
    if not host:
        return None
    scheme = scope.get("scheme") or "http"
    if scope["type"] == "websocket":
        scheme = "https" if scheme == "wss" else "http"
    return f"{scheme}://{host}".lower()


def is_origin_allowed(scope: dict, allowed: Iterable[str]) -> bool:
    """Decide whether *scope* may proceed under the origin policy."""
    if scope["type"] == "http" and scope.get("method", "GET").upper() in SAFE_METHODS:
        return True
    origin = _header(scope, b"origin")
    if origin is None:
        return True
    origin = origin.strip().lower()
    if origin == _same_origin(scope):
        return True
    return origin in {o.lower() for o in allowed}


class OriginGuardMiddleware:
    """Pure ASGI middleware so it can reject WebSocket handshakes too."""

    def __init__(self, app, allowed_origins: Callable[[], Iterable[str]]):
        self.app = app
        self._allowed_origins = allowed_origins

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or is_origin_allowed(
            scope, self._allowed_origins()
        ):
            await self.app(scope, receive, send)
            return

        logger.warning(
            "Rejected %s request from disallowed origin %s (path=%s)",
            scope["type"], _header(scope, b"origin"), scope.get("path"),
        )
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": WS_CLOSE_ORIGIN_NOT_ALLOWED})
            return
        body = b'{"detail":"Origin not allowed"}'
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})
