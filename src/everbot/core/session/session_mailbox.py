"""Mailbox event deposit, history injection, and acknowledgement.

Extracted from :class:`SessionManager` to isolate the deduplication and
ordering logic that guards cross-session message injection.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, TYPE_CHECKING

from .session_data import SessionData
from .history_utils import evict_oldest_heartbeat

if TYPE_CHECKING:
    from .session import SessionManager

logger = logging.getLogger(__name__)


def parse_iso_datetime(value: Any) -> Optional[datetime]:
    """Parse an ISO datetime string → UTC datetime, or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_mailbox_event_stale(
    event: Dict[str, Any],
    *,
    now_utc: datetime,
    stale_after: timedelta = timedelta(hours=24),
) -> bool:
    """Return True when event is marked stale and exceeds max age."""
    if not bool(event.get("suppress_if_stale", False)):
        return False
    event_ts = parse_iso_datetime(event.get("timestamp"))
    if event_ts is None:
        return False
    return (now_utc - event_ts) > stale_after


async def deposit_mailbox_event(
    mgr: SessionManager,
    session_id: str,
    event: Dict[str, Any],
    *,
    timeout: float = 5.0,
    blocking: bool = True,
) -> bool:
    """Append one event into session mailbox atomically with idempotency."""
    if not isinstance(event, dict):
        return False

    event_obj = dict(event)
    now_utc = datetime.now(timezone.utc)
    if not isinstance(event_obj.get("timestamp"), str) or not str(event_obj.get("timestamp")).strip():
        event_obj["timestamp"] = now_utc.isoformat()
    event_id = str(event_obj.get("event_id") or "").strip()
    dedupe_key = str(event_obj.get("dedupe_key") or "").strip()
    inserted = {"value": False}
    dropped_duplicate = {"value": False}
    dropped_stale = {"value": False}

    def _mutator(session_data: SessionData) -> None:
        if not isinstance(session_data.mailbox, list):
            session_data.mailbox = []
        mailbox = [e for e in session_data.mailbox if isinstance(e, dict)]

        if event_id:
            existing_ids = {str(e.get("event_id") or "").strip() for e in mailbox}
            if event_id in existing_ids:
                dropped_duplicate["value"] = True
                return

        if is_mailbox_event_stale(event_obj, now_utc=now_utc):
            dropped_stale["value"] = True
            return

        if dedupe_key:
            filtered = []
            removed_any = False
            for existing in mailbox:
                existing_key = str(existing.get("dedupe_key") or "").strip()
                if existing_key and existing_key == dedupe_key:
                    removed_any = True
                    continue
                filtered.append(existing)
            mailbox = filtered
            if removed_any:
                dropped_duplicate["value"] = True

        mailbox.append(dict(event_obj))
        session_data.mailbox = mailbox
        inserted["value"] = True

    # bump_updated_at=False: mailbox deposits are background writes and must not
    # be treated as user activity by get_last_activity_time (idle_hours fix).
    updated = await mgr.update_atomic(session_id, _mutator, timeout=timeout, blocking=blocking,
                                      bump_updated_at=False)
    if updated is None:
        return False
    if inserted["value"]:
        mgr.record_metric("mailbox_deposit_count")
    if dropped_duplicate["value"]:
        mgr.record_metric("mailbox_dedup_drop_count")
    if dropped_stale["value"]:
        mgr.record_metric("mailbox_stale_drop_count")
    return True


async def inject_history_message(
    mgr: SessionManager,
    session_id: str,
    message: dict,
    *,
    timeout: float = 5.0,
    blocking: bool = True,
) -> bool:
    """Append one message into session history_messages atomically.

    Used by HeartbeatRunner to inject deliverable results into the
    primary session's conversation history so that subsequent chat
    turns see the heartbeat output as a real assistant message.
    """
    if not isinstance(message, dict):
        return False

    msg_obj = dict(message)

    def _mutator(session_data: SessionData) -> None:
        if not isinstance(session_data.history_messages, list):
            session_data.history_messages = []

        # --- Dedup by run_id ---
        run_id = (msg_obj.get("metadata") or {}).get("run_id")
        if run_id:
            for existing in session_data.history_messages:
                if (
                    isinstance(existing, dict)
                    and isinstance(existing.get("metadata"), dict)
                    and existing["metadata"].get("run_id") == run_id
                ):
                    return  # already injected

        msg_role = msg_obj.get("role")
        last_msg = (
            session_data.history_messages[-1]
            if session_data.history_messages
            and isinstance(session_data.history_messages[-1], dict)
            else None
        )

        # --- Content-based dedup for recent identical user messages ---
        _DEDUP_WINDOW = 5
        if msg_role == "user":
            incoming_content = msg_obj.get("content")
            recent_user_msgs = [
                m for m in session_data.history_messages
                if isinstance(m, dict) and m.get("role") == "user"
            ][-_DEDUP_WINDOW:]
            for existing in recent_user_msgs:
                if existing.get("content") == incoming_content:
                    return  # duplicate user message, skip

        # --- Heartbeat/deferred assistant after unanswered user question ---
        msg_source = (msg_obj.get("metadata") or {}).get("source", "")
        msg_run_id = (msg_obj.get("metadata") or {}).get("run_id")
        if (
            msg_role == "assistant"
            and msg_source in ("heartbeat", "deferred_result")
            and last_msg is not None
            and last_msg.get("role") == "user"
        ):
            _ack = {"role": "assistant", "content": "(acknowledged)"}
            _bg = {"role": "user", "content": "[Background notification follows]"}
            if msg_run_id:
                _ack["metadata"] = {"source": "system", "category": "placeholder", "run_id": msg_run_id}
                _bg["metadata"] = {"source": "system", "category": "placeholder", "run_id": msg_run_id}
            session_data.history_messages.append(_ack)
            session_data.history_messages.append(_bg)

        # --- Consecutive user message guard ---
        elif (
            msg_role == "user"
            and last_msg is not None
            and last_msg.get("role") == "user"
        ):
            session_data.history_messages.append(
                {"role": "assistant", "content": "(acknowledged)"}
            )

        session_data.history_messages.append(msg_obj)
        session_data.history_messages = evict_oldest_heartbeat(session_data.history_messages)

    # bump_updated_at=False: history injection from heartbeat is background activity.
    updated = await mgr.update_atomic(session_id, _mutator, timeout=timeout, blocking=blocking,
                                      bump_updated_at=False)
    if updated is not None:
        mgr.record_metric("history_inject_count")
    return updated is not None


async def ack_mailbox_events(
    mgr: SessionManager,
    session_id: str,
    event_ids: list[str],
    *,
    timeout: float = 5.0,
    blocking: bool = True,
    lock_already_held: bool = False,
) -> bool:
    """Remove consumed mailbox events by event_id atomically."""
    ids = {str(eid).strip() for eid in event_ids if str(eid).strip()}
    if not ids:
        return True

    def _mutator(session_data: SessionData) -> None:
        if not isinstance(session_data.mailbox, list):
            session_data.mailbox = []
        session_data.mailbox = [
            e for e in session_data.mailbox
            if not isinstance(e, dict) or str(e.get("event_id") or "").strip() not in ids
        ]

    # bump_updated_at=False: mailbox ack is background activity.
    if lock_already_held:
        updated = await mgr.persistence.update_atomic(
            session_id, _mutator, timeout=timeout, blocking=blocking,
            lock_already_held=True, bump_updated_at=False,
        )
    else:
        updated = await mgr.update_atomic(session_id, _mutator, timeout=timeout, blocking=blocking,
                                          bump_updated_at=False)
    if updated is not None:
        mgr.record_metric("mailbox_drain_count", float(len(ids)))
    return updated is not None
