"""Event memory store — append-only monthly markdown files.

Events are time-anchored occurrences (decisions, todos, incidents, ...)
stored under ``events/YYYY-MM.md`` based on each entry's ``event_at`` month.

Each file is append-only: writes never rewrite existing entries. This
preserves the audit trail and avoids the "全量重写 vs 增量追加" semantic
conflict that would arise from sharing storage with profile memories.

Deduplication is the caller's responsibility — typically handled by the
``MemoryManager`` watermark that ensures the same conversation slice is
not re-extracted.
"""

import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List

from .models import MemoryEntry

logger = logging.getLogger(__name__)

# Header pattern:
#   ### [id] category | score | event_at | due_at | last_activated_date | activation_count
# Both event_at and due_at accept any non-whitespace token; due_at is "-" when absent.
_HEADER_RE = re.compile(
    r"^###\s+\[(\w+)\]\s+(\w+)\s*\|\s*([\d.]+)"
    r"\s*\|\s*(\S+)"
    r"\s*\|\s*(\S+)"
    r"\s*\|\s*([\d-]+)"
    r"\s*\|\s*(\d+)\s*$"
)
_MONTH_FILE_RE = re.compile(r"^(\d{4})-(\d{2})\.md$")


class EventStore:
    """Append-only event memory store.

    All entries carry ``kind="event"`` and require ``event_at``. Storage is
    keyed by the *event* month (not the ingestion month): an event from
    2026-05-01 ingested in 2026-06 still lands in ``events/2026-05.md``.
    """

    def __init__(self, events_dir: Path):
        self.events_dir = Path(events_dir)

    # ------------------------------------------------------------------ writes

    def append(self, entries: List[MemoryEntry]) -> int:
        """Append entries to their corresponding month file.

        Returns the number of entries actually written. Entries without
        ``event_at`` are skipped with a warning.
        """
        if not entries:
            return 0

        by_month: Dict[str, List[MemoryEntry]] = defaultdict(list)
        for entry in entries:
            month = self._month_key(entry.event_at)
            if month is None:
                logger.warning(
                    "Event %s has unusable event_at %r; skipping",
                    entry.id, entry.event_at,
                )
                continue
            by_month[month].append(entry)

        if not by_month:
            return 0

        self.events_dir.mkdir(parents=True, exist_ok=True)
        written = 0
        for month, month_entries in by_month.items():
            path = self.events_dir / f"{month}.md"
            is_new = not path.exists()
            with path.open("a", encoding="utf-8") as fp:
                if is_new:
                    fp.write(f"# Event Memory — {month}\n\n## Events\n\n")
                for entry in month_entries:
                    fp.write(_format_entry(entry))
                    written += 1
        return written

    # ------------------------------------------------------------------- reads

    def load_recent(self, days: int = 30) -> List[MemoryEntry]:
        """Load events whose ``event_at`` falls within the past ``days`` days.

        Only the month files overlapping the time window are read.
        """
        if not self.events_dir.exists():
            return []

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days)
        candidates: List[MemoryEntry] = []
        for month_key in self._months_between(cutoff, now):
            path = self.events_dir / f"{month_key}.md"
            if path.exists():
                candidates.extend(self._load_file(path))

        return [
            entry for entry in candidates
            if self._parse_event_at(entry.event_at) >= cutoff
        ]

    def load_all(self) -> List[MemoryEntry]:
        """Load every event from every month file."""
        if not self.events_dir.exists():
            return []
        entries: List[MemoryEntry] = []
        for path in sorted(self.events_dir.iterdir()):
            if path.is_file() and _MONTH_FILE_RE.match(path.name):
                entries.extend(self._load_file(path))
        return entries

    def list_months(self) -> List[str]:
        """Return sorted list of month keys (YYYY-MM) that have files."""
        if not self.events_dir.exists():
            return []
        keys = []
        for path in self.events_dir.iterdir():
            m = _MONTH_FILE_RE.match(path.name) if path.is_file() else None
            if m:
                keys.append(f"{m.group(1)}-{m.group(2)}")
        return sorted(keys)

    # --------------------------------------------------------------- internals

    def _load_file(self, path: Path) -> List[MemoryEntry]:
        """Parse one ``YYYY-MM.md`` file into entries."""
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            logger.warning("Failed to read event file %s", path, exc_info=True)
            return []

        entries: List[MemoryEntry] = []
        current = None
        content_lines: List[str] = []

        def _flush():
            if current is None:
                return
            current["content"] = "\n".join(content_lines).strip()
            if not current["content"]:
                return
            try:
                entries.append(MemoryEntry.from_dict(current))
            except Exception:
                logger.debug("Skipping corrupt event entry: %s", current.get("id"))

        for line in text.split("\n"):
            m = _HEADER_RE.match(line.strip())
            if m:
                _flush()
                due_raw = m.group(5)
                current = {
                    "id": m.group(1),
                    "category": m.group(2),
                    "score": float(m.group(3)),
                    "event_at": m.group(4),
                    "due_at": None if due_raw == "-" else due_raw,
                    "last_activated": m.group(6),
                    "activation_count": int(m.group(7)),
                    "kind": "event",
                }
                content_lines = []
            elif current is not None:
                stripped = line.strip()
                if stripped.startswith("## ") or stripped.startswith("# "):
                    continue
                content_lines.append(line)

        _flush()
        return entries

    @staticmethod
    def _month_key(event_at: str) -> str | None:
        """Extract YYYY-MM from an ISO8601 timestamp; None if unparseable."""
        if not event_at or len(event_at) < 7:
            return None
        prefix = event_at[:7]
        if re.match(r"^\d{4}-\d{2}$", prefix):
            return prefix
        return None

    @staticmethod
    def _parse_event_at(raw: str) -> datetime:
        """Parse an event_at string to UTC-aware datetime; epoch on failure."""
        if not raw:
            return datetime.min.replace(tzinfo=timezone.utc)
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    @staticmethod
    def _months_between(start: datetime, end: datetime) -> List[str]:
        """Yield YYYY-MM keys for every month overlapping [start, end]."""
        keys: List[str] = []
        year, month = start.year, start.month
        end_year, end_month = end.year, end.month
        while (year, month) <= (end_year, end_month):
            keys.append(f"{year:04d}-{month:02d}")
            month += 1
            if month > 12:
                month = 1
                year += 1
        return keys


def _format_entry(entry: MemoryEntry) -> str:
    """Format an event entry as a markdown block."""
    event_at = entry.event_at or "-"
    due_at = entry.due_at or "-"
    last_act = (
        entry.last_activated[:10]
        if len(entry.last_activated) >= 10
        else entry.last_activated
    )
    header = (
        f"### [{entry.id}] {entry.category} | {entry.score:.2f} | "
        f"{event_at} | {due_at} | {last_act} | {entry.activation_count}"
    )
    return f"{header}\n{entry.content}\n\n"
