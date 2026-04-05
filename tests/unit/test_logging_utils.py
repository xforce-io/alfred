"""Tests for daemon logging helpers."""

from __future__ import annotations

import logging
from pathlib import Path

from src.everbot.infra.logging_utils import (
    SuppressSuccessfulTelegramPolling,
    redact_sensitive_text,
    rotate_log_file_if_needed,
)


def test_redact_sensitive_text_hides_telegram_bot_token():
    original = (
        'HTTP Request: GET '
        'https://api.telegram.org/bot123456:ABC_SECRET/getUpdates?offset=0&timeout=10 '
        '"HTTP/1.1 200 OK"'
    )
    redacted = redact_sensitive_text(original)
    assert "ABC_SECRET" not in redacted
    assert "***REDACTED***" in redacted


def test_redact_sensitive_text_hides_bearer_and_api_key():
    original = "Authorization: Bearer secret-token api_key=abcdefghi123"
    redacted = redact_sensitive_text(original)
    assert "secret-token" not in redacted
    assert "abcdefghi123" not in redacted
    assert redacted.count("***REDACTED***") >= 2


def test_suppress_successful_telegram_polling_filter_drops_getupdates_200():
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=(
            'HTTP Request: GET '
            'https://api.telegram.org/bot123:token/getUpdates?offset=0&timeout=10 '
            '"HTTP/1.1 200 OK"'
        ),
        args=(),
        exc_info=None,
    )
    assert SuppressSuccessfulTelegramPolling().filter(record) is False


def test_suppress_successful_telegram_polling_filter_keeps_sendmessage():
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='HTTP Request: POST https://api.telegram.org/bot123:token/sendMessage "HTTP/1.1 200 OK"',
        args=(),
        exc_info=None,
    )
    assert SuppressSuccessfulTelegramPolling().filter(record) is True


def test_rotate_log_file_if_needed_rotates_when_oversized(tmp_path: Path):
    log_file = tmp_path / "heartbeat_events.jsonl"
    log_file.write_text("x" * 32, encoding="utf-8")

    rotate_log_file_if_needed(log_file, max_bytes=16, backup_count=2)

    assert not log_file.exists()
    rotated = tmp_path / "heartbeat_events.jsonl.1"
    assert rotated.exists()
    assert rotated.read_text(encoding="utf-8") == "x" * 32
