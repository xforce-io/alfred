"""Web app wiring for deny-by-default (#227): lifespan refuses without an
API key, unknown / traversal agent names are rejected before any handler runs."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src.everbot.web import app as web_app
from src.everbot.web.auth import API_KEY_REQUIRED_MESSAGE

KEY = "unit-test-key"


def test_lifespan_refuses_to_start_without_api_key():
    with patch("src.everbot.web.auth._get_configured_api_key", return_value=""):
        with pytest.raises(RuntimeError, match=API_KEY_REQUIRED_MESSAGE):
            with TestClient(web_app.app):
                pass


@pytest.fixture
def client(tmp_path, monkeypatch):
    from src.everbot.infra import user_data as ud

    udm = ud.UserDataManager(alfred_home=tmp_path)
    (tmp_path / "agents" / "real_agent").mkdir(parents=True)
    monkeypatch.setattr(web_app, "get_user_data_manager", lambda: udm)
    with patch("src.everbot.web.auth._get_configured_api_key", return_value=KEY):
        with TestClient(web_app.app) as c:
            yield c


@pytest.mark.parametrize("agent", ["ghost_agent", "...", "a%20b", "x" * 65])
def test_unknown_or_traversal_agent_is_404_on_authenticated_routes(client, agent):
    r = client.get(f"/api/agents/{agent}/sessions", headers={"X-API-Key": KEY})
    assert r.status_code == 404
    assert r.json()["detail"].startswith("Unknown agent")
    r = client.post(f"/api/agents/{agent}/sessions/reset", headers={"X-API-Key": KEY})
    assert r.status_code == 404


def test_encoded_slash_in_agent_name_never_reaches_a_handler(client):
    # Starlette's ``{agent_name}`` segment does not match "/", so an encoded
    # "../.." falls through the router with a plain 404.
    r = client.post("/api/agents/..%2F..%2Fetc/sessions/reset", headers={"X-API-Key": KEY})
    assert r.status_code == 404


def test_auth_runs_before_agent_check(client):
    r = client.get("/api/agents/ghost_agent/sessions")
    assert r.status_code == 401


def test_websocket_unknown_agent_closes_4004(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/ws/chat/ghost_agent?api_key={KEY}"):
            pass
    assert exc.value.code == 4004


def test_websocket_unauthenticated_closes_4001(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/chat/real_agent"):
            pass
    assert exc.value.code == 4001
