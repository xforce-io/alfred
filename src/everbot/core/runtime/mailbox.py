"""Mailbox formatting helpers for primary-session turns."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Tuple


def compose_message_with_mailbox_updates(
    trigger_message: str,
    mailbox_events: Iterable[Dict[str, Any]],
    *,
    now: datetime | None = None,
    stale_after: timedelta = timedelta(hours=24),
    max_events: int = 3,
) -> Tuple[str, List[str]]:
    """Prefix user message with mailbox updates and return ack event ids.

    The function applies two cleanup rules before composing the prefix:
    - dedupe by ``dedupe_key`` (keep the latest event for the same key)
    - drop stale events where ``suppress_if_stale`` is true and age exceeds ``stale_after``
    """

    def _append_unique_event_id(values: List[str], event_id: str) -> None:
        if not event_id:
            return
        if event_id not in values:
            values.append(event_id)

    def _parse_timestamp(value: Any) -> datetime | None:
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
        return parsed

    def _event_is_stale(event: Dict[str, Any], now_utc: datetime) -> bool:
        if not bool(event.get("suppress_if_stale", False)):
            return False
        event_ts = _parse_timestamp(event.get("timestamp"))
        if event_ts is None:
            return False
        return now_utc - event_ts > stale_after

    events = [e for e in (mailbox_events or []) if isinstance(e, dict)]
    if not events:
        return trigger_message, []

    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)

    # Keep latest event for the same dedupe_key.
    dedupe_keys_seen: set[str] = set()
    deduped_events_reversed: List[Dict[str, Any]] = []
    dropped_event_ids: List[str] = []
    for event in reversed(events):
        dedupe_key = str(event.get("dedupe_key") or "").strip()
        event_id = str(event.get("event_id") or "").strip()
        if dedupe_key:
            if dedupe_key in dedupe_keys_seen:
                _append_unique_event_id(dropped_event_ids, event_id)
                continue
            dedupe_keys_seen.add(dedupe_key)
        deduped_events_reversed.append(event)
    deduped_events = list(reversed(deduped_events_reversed))

    lines: List[str] = ["## Background Updates"]
    ack_ids: List[str] = []
    included_count = 0

    for event in deduped_events:
        event_id = str(event.get("event_id") or "").strip()
        if _event_is_stale(event, now_utc):
            _append_unique_event_id(dropped_event_ids, event_id)
            continue

        event_type = str(event.get("event_type") or "system_update")
        summary = str(event.get("summary") or "").strip()
        if not summary:
            _append_unique_event_id(dropped_event_ids, event_id)
            continue

        # Cap the number of events included in the message to avoid
        # drowning out the user's actual query.  Excess events are
        # still acked so they don't accumulate forever.
        if max_events > 0 and included_count >= max_events:
            _append_unique_event_id(dropped_event_ids, event_id)
            continue

        lines.append(f"- [{event_type}] {summary}")
        detail = str(event.get("detail") or "").strip()
        if detail:
            lines.append(f"  Detail: {detail}")
        _append_unique_event_id(ack_ids, event_id)
        included_count += 1

    for dropped_event_id in dropped_event_ids:
        _append_unique_event_id(ack_ids, dropped_event_id)

    if len(lines) == 1:
        return trigger_message, ack_ids

    lines.append("")
    lines.append("## User Message")
    lines.append(trigger_message)
    return "\n".join(lines), ack_ids
