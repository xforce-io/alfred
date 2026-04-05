"""Logging helpers for daemon-safe output."""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
from pathlib import Path

_TELEGRAM_BOT_URL_RE = re.compile(r"(https://api\.telegram\.org/bot)([^/\s]+)")
_BEARER_RE = re.compile(r"(?i)(authorization:\s*bearer\s+)([a-z0-9._\-]+)")
_API_KEY_RE = re.compile(r"(?i)\b(api[_-]?key|token)\b(['\"=: ]+)([a-z0-9._\-]{8,})")


def redact_sensitive_text(text: str) -> str:
    """Redact secrets that may appear in log lines."""
    if not text:
        return text
    text = _TELEGRAM_BOT_URL_RE.sub(r"\1***REDACTED***", text)
    text = _BEARER_RE.sub(r"\1***REDACTED***", text)
    text = _API_KEY_RE.sub(r"\1\2***REDACTED***", text)
    return text


class RedactingFormatter(logging.Formatter):
    """Formatter that redacts sensitive values after formatting."""

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        return redact_sensitive_text(rendered)


class SuppressSuccessfulTelegramPolling(logging.Filter):
    """Drop successful Telegram polling request logs from noisy HTTP clients."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        lowered = message.lower()
        if "api.telegram.org" not in lowered:
            return True
        if "getupdates" not in lowered:
            return True
        if "200 ok" not in lowered:
            return True
        return False


def rotate_log_file_if_needed(
    path: Path,
    *,
    max_bytes: int,
    backup_count: int,
) -> None:
    """Rotate a plain-text log file when it grows beyond the configured size."""
    if backup_count < 1:
        return
    try:
        if not path.exists() or path.stat().st_size < max_bytes:
            return
    except OSError:
        return

    oldest = path.with_name(f"{path.name}.{backup_count}")
    if oldest.exists():
        oldest.unlink()

    for idx in range(backup_count - 1, 0, -1):
        src = path.with_name(f"{path.name}.{idx}")
        dst = path.with_name(f"{path.name}.{idx + 1}")
        if src.exists():
            src.replace(dst)

    path.replace(path.with_name(f"{path.name}.1"))


def configure_daemon_logging(*, level: str, log_file: Path | None = None) -> None:
    """Configure root logging with redaction and reduced transport noise."""
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    formatter = RedactingFormatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    polling_filter = SuppressSuccessfulTelegramPolling()

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(polling_filter)
    root.addHandler(stream_handler)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(polling_filter)
        root.addHandler(file_handler)

    logging.getLogger("httpx").setLevel(logging.INFO)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    if os.environ.get("ALFRED_DEBUG_HTTP") == "1":
        logging.getLogger("httpx").setLevel(logging.DEBUG)
