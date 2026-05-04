"""Tests for EventStore — append-only monthly markdown files."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.everbot.core.memory.event_store import EventStore
from src.everbot.core.memory.models import MemoryEntry


def _make_event(
    *,
    id: str = "evt001",
    content: str = "用户决定切到 deepseek-chat",
    category: str = "decision",
    score: float = 0.8,
    event_at: str | None = "2026-05-01T10:30:00+00:00",
    activation_count: int = 1,
) -> MemoryEntry:
    return MemoryEntry(
        id=id,
        content=content,
        category=category,
        score=score,
        created_at=event_at or "2026-05-01T00:00:00+00:00",
        last_activated=event_at or "2026-05-01T00:00:00+00:00",
        activation_count=activation_count,
        source_session="s1",
        kind="event",
        event_at=event_at,
    )


class TestEventStoreAppend:
    def test_append_to_empty_dir_creates_file(self, tmp_path: Path):
        store = EventStore(tmp_path / "events")
        written = store.append([_make_event()])
        assert written == 1
        assert (tmp_path / "events" / "2026-05.md").exists()

    def test_append_groups_by_event_month(self, tmp_path: Path):
        store = EventStore(tmp_path / "events")
        entries = [
            _make_event(id="apr01", event_at="2026-04-15T10:00:00+00:00"),
            _make_event(id="may01", event_at="2026-05-01T10:00:00+00:00"),
            _make_event(id="may02", event_at="2026-05-20T10:00:00+00:00"),
        ]
        assert store.append(entries) == 3
        assert (tmp_path / "events" / "2026-04.md").exists()
        assert (tmp_path / "events" / "2026-05.md").exists()
        may_text = (tmp_path / "events" / "2026-05.md").read_text("utf-8")
        assert "[may01]" in may_text and "[may02]" in may_text
        assert "[apr01]" not in may_text

    def test_append_is_additive_not_rewrite(self, tmp_path: Path):
        """Two separate append calls preserve all entries."""
        store = EventStore(tmp_path / "events")
        store.append([_make_event(id="first", event_at="2026-05-01T10:00:00+00:00")])
        store.append([_make_event(id="second", event_at="2026-05-02T10:00:00+00:00")])
        text = (tmp_path / "events" / "2026-05.md").read_text("utf-8")
        assert "[first]" in text
        assert "[second]" in text

    def test_append_skips_entry_without_event_at(self, tmp_path: Path):
        store = EventStore(tmp_path / "events")
        bad = _make_event(id="bad", event_at=None)
        good = _make_event(id="good")
        assert store.append([bad, good]) == 1

    def test_append_empty_list_creates_no_files(self, tmp_path: Path):
        store = EventStore(tmp_path / "events")
        assert store.append([]) == 0
        assert not (tmp_path / "events").exists()


class TestEventStoreLoad:
    def test_load_all_empty_dir(self, tmp_path: Path):
        store = EventStore(tmp_path / "events")
        assert store.load_all() == []

    def test_load_all_returns_entries_with_event_kind(self, tmp_path: Path):
        store = EventStore(tmp_path / "events")
        store.append([_make_event(id="evt001")])
        loaded = store.load_all()
        assert len(loaded) == 1
        e = loaded[0]
        assert e.id == "evt001"
        assert e.kind == "event"
        assert e.event_at == "2026-05-01T10:30:00+00:00"
        assert e.category == "decision"

    def test_load_all_round_trip(self, tmp_path: Path):
        store = EventStore(tmp_path / "events")
        original = [
            _make_event(id="apr15", event_at="2026-04-15T10:00:00+00:00",
                        content="A 月事件", score=0.7),
            _make_event(id="may01", event_at="2026-05-01T10:00:00+00:00",
                        content="B 月事件", score=0.6),
        ]
        store.append(original)
        loaded = store.load_all()
        assert {e.id for e in loaded} == {"apr15", "may01"}
        for e in loaded:
            orig = next(o for o in original if o.id == e.id)
            assert e.content == orig.content
            assert abs(e.score - orig.score) < 0.01
            assert e.event_at == orig.event_at

    def test_due_at_round_trip(self, tmp_path: Path):
        store = EventStore(tmp_path / "events")
        todo = MemoryEntry(
            id="todo01",
            content="周五交付 demo",
            category="todo",
            score=0.6,
            created_at="2026-05-01T10:00:00+00:00",
            last_activated="2026-05-01T10:00:00+00:00",
            activation_count=1,
            source_session="s1",
            kind="event",
            event_at="2026-05-01T10:00:00+00:00",
            due_at="2026-05-03T18:00:00+00:00",
        )
        store.append([todo])
        loaded = store.load_all()
        assert len(loaded) == 1
        assert loaded[0].due_at == "2026-05-03T18:00:00+00:00"

    def test_no_due_at_serializes_as_dash(self, tmp_path: Path):
        store = EventStore(tmp_path / "events")
        store.append([_make_event(id="evt", event_at="2026-05-01T10:00:00+00:00")])
        text = (tmp_path / "events" / "2026-05.md").read_text("utf-8")
        # Header has " | - | " in the due_at slot
        assert " | - | " in text
        # Round-trip: due_at parsed back to None
        loaded = store.load_all()
        assert loaded[0].due_at is None

    def test_load_recent_filters_by_event_at(self, tmp_path: Path):
        store = EventStore(tmp_path / "events")
        now = datetime.now(timezone.utc)
        recent = (now - timedelta(days=5)).isoformat()
        old = (now - timedelta(days=60)).isoformat()
        store.append([
            _make_event(id="old1", event_at=old),
            _make_event(id="new1", event_at=recent),
        ])
        loaded = store.load_recent(days=30)
        assert {e.id for e in loaded} == {"new1"}

    def test_load_recent_includes_boundary(self, tmp_path: Path):
        """Events older than the window are excluded even if file is scanned."""
        store = EventStore(tmp_path / "events")
        now = datetime.now(timezone.utc)
        # Two events in the same month file, one inside window, one outside.
        store.append([
            _make_event(id="inside", event_at=(now - timedelta(days=5)).isoformat()),
            _make_event(id="outside", event_at=(now - timedelta(days=100)).isoformat()),
        ])
        loaded = store.load_recent(days=14)
        assert {e.id for e in loaded} == {"inside"}

    def test_tolerates_corrupt_entries(self, tmp_path: Path):
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        path = events_dir / "2026-05.md"
        path.write_text(
            "# Event Memory — 2026-05\n\n"
            "## Events\n\n"
            "### [good01] decision | 0.80 | 2026-05-01T10:00:00+00:00 | - | 2026-05-01 | 1\n"
            "正常事件\n\n"
            "garbage line not matching anything\n"
            "### [good02] todo | 0.60 | 2026-05-02T11:00:00+00:00 | 2026-05-05 | 2026-05-02 | 1\n"
            "另一条正常事件\n\n",
            encoding="utf-8",
        )
        store = EventStore(events_dir)
        loaded = store.load_all()
        assert {e.id for e in loaded} == {"good01", "good02"}

    def test_skips_unrelated_files(self, tmp_path: Path):
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        (events_dir / "README.md").write_text("not an event file", encoding="utf-8")
        (events_dir / "archive").mkdir()
        store = EventStore(events_dir)
        assert store.load_all() == []
        assert store.list_months() == []

    def test_list_months(self, tmp_path: Path):
        store = EventStore(tmp_path / "events")
        store.append([
            _make_event(id="a", event_at="2026-03-15T00:00:00+00:00"),
            _make_event(id="b", event_at="2026-05-15T00:00:00+00:00"),
        ])
        assert store.list_months() == ["2026-03", "2026-05"]


class TestEventStoreMonthKey:
    @pytest.mark.parametrize("event_at, expected", [
        ("2026-05-01T10:30:00+00:00", "2026-05"),
        ("2026-12-31", "2026-12"),
        ("2026-1-5", None),     # malformed (single-digit month)
        ("not-a-date", None),
        ("", None),
    ])
    def test_month_key(self, event_at, expected):
        assert EventStore._month_key(event_at) == expected
