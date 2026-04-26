"""Tests for the skill_notification realtime bridge added in SLM v2.

Skill notifications need a dual-write: persisted to primary mailbox AND
emitted on the realtime event bus so user-facing channels (Telegram, web
SSE) deliver them. Without the realtime emit, notifications sit in the web
session mailbox forever and the user chatting via Telegram never sees them.
"""

from __future__ import annotations

from typing import Any, List
from unittest.mock import AsyncMock

import pytest

from src.everbot.core.runtime.skill_context import MailboxAdapter


class _FakeSessionManager:
    def __init__(self):
        self.deposits: List[Any] = []

    async def deposit_mailbox_event(self, session_id, event, *, timeout, blocking):
        self.deposits.append((session_id, event))
        return True


@pytest.mark.asyncio
async def test_deposit_persists_to_mailbox_and_emits_realtime(monkeypatch):
    sm = _FakeSessionManager()
    adapter = MailboxAdapter(sm, primary_session_id="web_session_demo", agent_name="demo")

    captured: List[Any] = []

    async def fake_emit(source_session_id, data, **kwargs):
        captured.append({"source_session_id": source_session_id, "data": data, "kwargs": kwargs})

    monkeypatch.setattr("src.everbot.core.runtime.events.emit", fake_emit)

    ok = await adapter.deposit("test summary", "test detail")
    assert ok is True

    # 1. Persisted to mailbox
    assert len(sm.deposits) == 1
    sid, event = sm.deposits[0]
    assert sid == "web_session_demo"
    assert event["event_type"] == "skill_notification"
    assert event["summary"] == "test summary"
    assert event["detail"] == "test detail"

    # 2. Realtime emit fired with proper source_type so telegram_channel routes it
    assert len(captured) == 1
    emit = captured[0]
    assert emit["source_session_id"] == "web_session_demo"
    assert emit["data"]["source_type"] == "skill_notification"
    assert emit["data"]["deliver"] is True
    assert emit["data"]["summary"] == "test summary"
    assert emit["kwargs"]["source_type"] == "skill_notification"
    assert emit["kwargs"]["agent_name"] == "demo"


@pytest.mark.asyncio
async def test_emit_failure_does_not_break_deposit(monkeypatch, caplog):
    sm = _FakeSessionManager()
    adapter = MailboxAdapter(sm, primary_session_id="web_session_demo", agent_name="demo")

    async def boom(*args, **kwargs):
        raise RuntimeError("event bus is broken")

    monkeypatch.setattr("src.everbot.core.runtime.events.emit", boom)

    # Mailbox persistence still succeeds; emit failure is best-effort.
    ok = await adapter.deposit("summary", "detail")
    assert ok is True
    assert len(sm.deposits) == 1


@pytest.mark.asyncio
async def test_mailbox_persistence_failure_returns_false_but_still_emits(monkeypatch):
    """If primary deposit fails, we still try to emit so the user sees it
    via realtime push and a per-channel mailbox mirror — the agent at least
    gets the notification through one path."""
    sm = _FakeSessionManager()

    async def fail_deposit(*args, **kwargs):
        return False

    sm.deposit_mailbox_event = fail_deposit  # type: ignore[assignment]
    adapter = MailboxAdapter(sm, primary_session_id="web_session_demo", agent_name="demo")

    captured: List[Any] = []

    async def fake_emit(source_session_id, data, **kwargs):
        captured.append(data["source_type"])

    monkeypatch.setattr("src.everbot.core.runtime.events.emit", fake_emit)

    ok = await adapter.deposit("s", "d")
    assert ok is False
    # But emit still ran — user-facing delivery still attempted.
    assert captured == ["skill_notification"]


def test_telegram_channel_filter_accepts_skill_notification():
    """The telegram_channel _on_background_event filter must include
    skill_notification so MailboxAdapter's emit reaches Telegram."""
    from src.everbot.channels import telegram_channel
    import inspect

    src = inspect.getsource(telegram_channel._on_background_event_filter_doc) \
        if hasattr(telegram_channel, "_on_background_event_filter_doc") else \
        inspect.getsource(telegram_channel.TelegramChannel._on_background_event)
    # Ensure the four expected source_types are all in the filter tuple.
    for st in ("heartbeat_delivery", "deferred_result", "inspector_push", "skill_notification"):
        assert f'"{st}"' in src, f"telegram filter missing {st}"
