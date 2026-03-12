"""Unit tests for CORS origin configuration in web app module."""

from __future__ import annotations

import os
from unittest import mock

from src.everbot.web.app import _get_cors_origins


class TestGetCorsOrigins:
    """Tests for the _get_cors_origins helper."""

    def test_default_origins_include_localhost(self):
        """Default origins should include common localhost variants."""
        with mock.patch.dict(os.environ, {}, clear=True):
            origins = _get_cors_origins()
        assert "http://localhost" in origins
        assert "http://127.0.0.1" in origins
        assert "http://localhost:8080" in origins
        assert "http://127.0.0.1:8080" in origins

    def test_default_origins_do_not_include_wildcard(self):
        """Default origins must never contain '*'."""
        with mock.patch.dict(os.environ, {}, clear=True):
            origins = _get_cors_origins()
        assert "*" not in origins

    def test_env_var_adds_extra_origins(self):
        """EVERBOT_CORS_ORIGINS env var should add additional origins."""
        with mock.patch.dict(os.environ, {"EVERBOT_CORS_ORIGINS": "https://example.com,https://app.example.com"}):
            origins = _get_cors_origins()
        assert "https://example.com" in origins
        assert "https://app.example.com" in origins
        # defaults still present
        assert "http://localhost" in origins

    def test_env_var_strips_whitespace(self):
        """Extra origins should be stripped of whitespace."""
        with mock.patch.dict(os.environ, {"EVERBOT_CORS_ORIGINS": " https://a.com , https://b.com "}):
            origins = _get_cors_origins()
        assert "https://a.com" in origins
        assert "https://b.com" in origins

    def test_env_var_deduplicates(self):
        """Should not add duplicates if env var overlaps with defaults."""
        with mock.patch.dict(os.environ, {"EVERBOT_CORS_ORIGINS": "http://localhost,https://new.com"}):
            origins = _get_cors_origins()
        assert origins.count("http://localhost") == 1
        assert "https://new.com" in origins

    def test_empty_env_var(self):
        """Empty EVERBOT_CORS_ORIGINS should not add any extra origins."""
        with mock.patch.dict(os.environ, {"EVERBOT_CORS_ORIGINS": ""}):
            origins = _get_cors_origins()
        # Only defaults
        assert len(origins) == 6

    def test_env_var_skips_empty_entries(self):
        """Trailing commas or empty segments should be ignored."""
        with mock.patch.dict(os.environ, {"EVERBOT_CORS_ORIGINS": "https://a.com,,, ,https://b.com,"}):
            origins = _get_cors_origins()
        assert "https://a.com" in origins
        assert "https://b.com" in origins
        assert "" not in origins
