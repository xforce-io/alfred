"""Origin guard decision matrix (#227 S2)."""

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src.everbot.web.origin_guard import (
    OriginGuardMiddleware,
    WS_CLOSE_ORIGIN_NOT_ALLOWED,
    is_origin_allowed,
)

ALLOWED = ["http://localhost:8765", "https://Trusted.example"]


def _scope(kind: str, method: str = "POST", origin=None, host="127.0.0.1:8765", scheme="http"):
    headers = [(b"host", host.encode())]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    scope = {"type": kind, "headers": headers, "scheme": scheme, "path": "/x"}
    if kind == "http":
        scope["method"] = method
    return scope


@pytest.mark.parametrize(
    "scope, expected",
    [
        (_scope("http", "POST"), True),                                   # no Origin → pass
        (_scope("http", "POST", origin="http://127.0.0.1:8765"), True),   # same-origin
        (_scope("http", "POST", origin="HTTP://127.0.0.1:8765"), True),   # case-insensitive
        (_scope("http", "POST", origin="http://localhost:8765"), True),   # allow-list
        (_scope("http", "POST", origin="https://trusted.example"), True), # allow-list, case
        (_scope("http", "POST", origin="https://evil.example"), False),   # cross-site
        (_scope("http", "GET", origin="https://evil.example"), True),     # safe method
        (_scope("http", "OPTIONS", origin="https://evil.example"), True), # preflight
        (_scope("websocket", origin="https://evil.example"), False),      # WS cross-site
        (_scope("websocket", origin="http://127.0.0.1:8765"), True),      # WS same-origin
        (_scope("websocket", origin="https://127.0.0.1:8765", scheme="wss"), True),
        (_scope("websocket"), True),                                      # WS no Origin
    ],
)
def test_is_origin_allowed_matrix(scope, expected):
    assert is_origin_allowed(scope, ALLOWED) is expected


@pytest.fixture
def client():
    app = FastAPI()

    @app.post("/api/thing")
    def post_thing():
        return {"ok": True}

    @app.get("/api/thing")
    def get_thing():
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        await sock.send_text("hi")
        await sock.close()

    app.add_middleware(OriginGuardMiddleware, allowed_origins=lambda: ALLOWED)
    return TestClient(app)


def test_http_post_from_foreign_origin_is_403(client):
    r = client.post("/api/thing", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert r.json() == {"detail": "Origin not allowed"}


def test_http_post_without_origin_or_from_allowed_origin_passes(client):
    assert client.post("/api/thing").status_code == 200
    assert client.post("/api/thing", headers={"Origin": "http://localhost:8765"}).status_code == 200


def test_http_get_is_not_guarded(client):
    assert client.get("/api/thing", headers={"Origin": "https://evil.example"}).status_code == 200


def test_websocket_from_foreign_origin_is_closed_4003(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws", headers={"Origin": "https://evil.example"}):
            pass
    assert exc.value.code == WS_CLOSE_ORIGIN_NOT_ALLOWED


def test_websocket_from_allowed_origin_connects(client):
    with client.websocket_connect("/ws", headers={"Origin": "http://localhost:8765"}) as sock:
        assert sock.receive_text() == "hi"
