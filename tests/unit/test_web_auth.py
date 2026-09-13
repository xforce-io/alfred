"""Unit tests for web API authentication module (src/everbot/web/auth.py)."""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest
from fastapi import HTTPException

from src.everbot.web.auth import (
    API_KEY_REQUIRED_MESSAGE,
    _get_configured_api_key,
    _extract_api_key,
    require_api_key,
    verify_api_key,
    verify_ws_api_key,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_request(headers: dict | None = None, query_params: dict | None = None) -> MagicMock:
    req = MagicMock()
    req.headers = headers or {}
    req.query_params = query_params or {}
    return req


def _make_websocket(query_params: dict | None = None) -> MagicMock:
    ws = MagicMock()
    ws.query_params = query_params or {}
    return ws


# ===========================================================================
# _get_configured_api_key
# ===========================================================================

class TestGetConfiguredApiKey:
    @patch("src.everbot.web.auth.get_config")
    def test_returns_key_when_configured(self, mock_load):
        mock_load.return_value = {"everbot": {"web": {"api_key": "secret123"}}}
        assert _get_configured_api_key() == "secret123"

    @patch("src.everbot.web.auth.get_config")
    def test_returns_empty_when_not_configured(self, mock_load):
        mock_load.return_value = {"everbot": {}}
        assert _get_configured_api_key() == ""

    @patch("src.everbot.web.auth.get_config")
    def test_returns_empty_when_key_is_none(self, mock_load):
        mock_load.return_value = {"everbot": {"web": {"api_key": None}}}
        assert _get_configured_api_key() == ""

    @patch("src.everbot.web.auth.get_config")
    def test_returns_empty_when_key_is_empty_string(self, mock_load):
        mock_load.return_value = {"everbot": {"web": {"api_key": ""}}}
        assert _get_configured_api_key() == ""

    @patch("src.everbot.web.auth.get_config")
    def test_config_error_propagates(self, mock_load):
        """#227: a broken config must not degrade into 'no key configured'."""
        mock_load.side_effect = FileNotFoundError("no config")
        with pytest.raises(FileNotFoundError):
            _get_configured_api_key()

    @patch("src.everbot.web.auth.get_config")
    def test_expands_env_reference(self, mock_load, monkeypatch):
        monkeypatch.setenv("EVERBOT_TEST_WEB_KEY", "from-env")
        mock_load.return_value = {"everbot": {"web": {"api_key": "${EVERBOT_TEST_WEB_KEY}"}}}
        assert _get_configured_api_key() == "from-env"

    @patch("src.everbot.web.auth.get_config")
    def test_unresolved_env_reference_raises(self, mock_load, monkeypatch):
        """An unset ${VAR} must not silently become the literal key text."""
        monkeypatch.delenv("EVERBOT_TEST_WEB_KEY", raising=False)
        mock_load.return_value = {"everbot": {"web": {"api_key": "${EVERBOT_TEST_WEB_KEY}"}}}
        with pytest.raises(ValueError, match="EVERBOT_TEST_WEB_KEY"):
            _get_configured_api_key()


class TestRequireApiKey:
    @patch("src.everbot.web.auth._get_configured_api_key", return_value="")
    def test_empty_key_raises(self, _mock):
        with pytest.raises(RuntimeError, match=API_KEY_REQUIRED_MESSAGE):
            require_api_key()

    @patch("src.everbot.web.auth._get_configured_api_key", return_value="k")
    def test_configured_key_returned(self, _mock):
        assert require_api_key() == "k"


# ===========================================================================
# _extract_api_key
# ===========================================================================

class TestExtractApiKey:
    def test_extracts_from_header(self):
        req = _make_request(headers={"x-api-key": "header_key"})
        assert _extract_api_key(req) == "header_key"

    def test_extracts_from_query_param(self):
        req = _make_request(query_params={"api_key": "param_key"})
        assert _extract_api_key(req) == "param_key"

    def test_header_takes_precedence_over_param(self):
        req = _make_request(
            headers={"x-api-key": "header_key"},
            query_params={"api_key": "param_key"},
        )
        assert _extract_api_key(req) == "header_key"

    def test_returns_none_when_neither_present(self):
        req = _make_request()
        assert _extract_api_key(req) is None


# ===========================================================================
# verify_api_key (HTTP routes)
# ===========================================================================

class TestVerifyApiKey:
    @pytest.mark.asyncio
    @patch("src.everbot.web.auth._get_configured_api_key", return_value="")
    async def test_no_key_configured_rejects_with_503(self, _mock):
        """#227 deny by default: an unconfigured key is a server error, not open access."""
        req = _make_request(headers={"x-api-key": "anything"})
        with pytest.raises(HTTPException) as exc_info:
            await verify_api_key(req)
        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    @patch("src.everbot.web.auth._get_configured_api_key", return_value="secret123")
    async def test_correct_key_in_header(self, _mock):
        req = _make_request(headers={"x-api-key": "secret123"})
        await verify_api_key(req)  # should not raise

    @pytest.mark.asyncio
    @patch("src.everbot.web.auth._get_configured_api_key", return_value="secret123")
    async def test_correct_key_in_query_param(self, _mock):
        req = _make_request(query_params={"api_key": "secret123"})
        await verify_api_key(req)  # should not raise

    @pytest.mark.asyncio
    @patch("src.everbot.web.auth._get_configured_api_key", return_value="secret123")
    async def test_wrong_key_raises_401(self, _mock):
        req = _make_request(headers={"x-api-key": "wrong_key"})
        with pytest.raises(HTTPException) as exc_info:
            await verify_api_key(req)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    @patch("src.everbot.web.auth._get_configured_api_key", return_value="secret123")
    async def test_missing_key_raises_401(self, _mock):
        req = _make_request()
        with pytest.raises(HTTPException) as exc_info:
            await verify_api_key(req)
        assert exc_info.value.status_code == 401


# ===========================================================================
# verify_ws_api_key (WebSocket routes)
# ===========================================================================

class TestVerifyWsApiKey:
    @pytest.mark.asyncio
    @patch("src.everbot.web.auth._get_configured_api_key", return_value="")
    async def test_no_key_configured_returns_false(self, _mock):
        """#227 deny by default."""
        ws = _make_websocket(query_params={"api_key": "anything"})
        assert await verify_ws_api_key(ws) is False

    @pytest.mark.asyncio
    @patch("src.everbot.web.auth._get_configured_api_key", return_value="ws_secret")
    async def test_correct_key_returns_true(self, _mock):
        ws = _make_websocket(query_params={"api_key": "ws_secret"})
        assert await verify_ws_api_key(ws) is True

    @pytest.mark.asyncio
    @patch("src.everbot.web.auth._get_configured_api_key", return_value="ws_secret")
    async def test_wrong_key_returns_false(self, _mock):
        ws = _make_websocket(query_params={"api_key": "bad"})
        assert await verify_ws_api_key(ws) is False

    @pytest.mark.asyncio
    @patch("src.everbot.web.auth._get_configured_api_key", return_value="ws_secret")
    async def test_missing_key_returns_false(self, _mock):
        ws = _make_websocket()
        assert await verify_ws_api_key(ws) is False
