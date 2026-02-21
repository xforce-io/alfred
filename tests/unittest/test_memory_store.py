"""Tests for MemoryStore — parsing, saving, round-trip, fault tolerance."""

import pytest
from pathlib import Path
from datetime import datetime, timezone

from src.everbot.core.memory.models import MemoryEntry
from src.everbot.core.memory.store import MemoryStore


def _make_entry(**overrides) -> MemoryEntry:
    defaults = dict(
        id="abc123",
        content="用户喜欢简洁代码",
        category="preference",
        score=0.8,
        created_at="2026-02-01T00:00:00+00:00",
        last_activated="2026-02-20T00:00:00+00:00",
        activation_count=5,
        source_session="session_001",
    )
    defaults.update(overrides)
    return MemoryEntry(**defaults)


class TestMemoryStoreLoad:
    """Parsing MEMORY.md into entries."""

    def test_load_normal(self, tmp_path: Path):
        md = tmp_path / "MEMORY.md"
        md.write_text(
            "# Agent Memory\n\n"
            "## Active Memories\n\n"
            "### [abc123] preference | 0.80 | 2026-02-20 | 5\n"
            "用户喜欢简洁代码\n\n"
            "### [def456] fact | 0.65 | 2026-02-19 | 3\n"
            "主要使用 Python 开发\n\n",
            encoding="utf-8",
        )
        store = MemoryStore(md)
        entries = store.load()
        assert len(entries) == 2
        assert entries[0].id == "abc123"
        assert entries[0].category == "preference"
        assert entries[0].score == 0.8
        assert entries[0].content == "用户喜欢简洁代码"
        assert entries[1].id == "def456"

    def test_load_empty_file(self, tmp_path: Path):
        md = tmp_path / "MEMORY.md"
        md.write_text("", encoding="utf-8")
        store = MemoryStore(md)
        assert store.load() == []

    def test_load_file_not_exist(self, tmp_path: Path):
        md = tmp_path / "MEMORY.md"
        store = MemoryStore(md)
        assert store.load() == []

    def test_load_tolerates_corrupt_entries(self, tmp_path: Path):
        md = tmp_path / "MEMORY.md"
        md.write_text(
            "# Agent Memory\n\n"
            "### [abc123] preference | 0.80 | 2026-02-20 | 5\n"
            "有效记忆\n\n"
            "这是一行垃圾数据\n"
            "### [def456] fact | 0.65 | 2026-02-19 | 3\n"
            "另一条有效记忆\n\n",
            encoding="utf-8",
        )
        store = MemoryStore(md)
        entries = store.load()
        assert len(entries) == 2

    def test_load_skips_entry_without_content(self, tmp_path: Path):
        md = tmp_path / "MEMORY.md"
        md.write_text(
            "### [abc123] preference | 0.80 | 2026-02-20 | 5\n"
            "\n"
            "### [def456] fact | 0.65 | 2026-02-19 | 3\n"
            "有内容的记忆\n",
            encoding="utf-8",
        )
        store = MemoryStore(md)
        entries = store.load()
        assert len(entries) == 1
        assert entries[0].id == "def456"


class TestMemoryStoreSave:
    """Writing entries to MEMORY.md."""

    def test_save_creates_file(self, tmp_path: Path):
        md = tmp_path / "MEMORY.md"
        store = MemoryStore(md)
        store.save([_make_entry()])
        assert md.exists()
        content = md.read_text(encoding="utf-8")
        assert "abc123" in content
        assert "preference" in content

    def test_save_creates_backup(self, tmp_path: Path):
        md = tmp_path / "MEMORY.md"
        md.write_text("old content", encoding="utf-8")
        store = MemoryStore(md)
        store.save([_make_entry()])
        bak = tmp_path / "MEMORY.md.bak"
        assert bak.exists()
        assert bak.read_text(encoding="utf-8") == "old content"

    def test_save_partitions_active_and_archived(self, tmp_path: Path):
        md = tmp_path / "MEMORY.md"
        store = MemoryStore(md)
        entries = [
            _make_entry(id="hi", score=0.9),
            _make_entry(id="lo", score=0.15),
        ]
        store.save(entries)
        content = md.read_text(encoding="utf-8")
        active_pos = content.index("## Active Memories")
        archived_pos = content.index("## Archived Memories")
        hi_pos = content.index("[hi]")
        lo_pos = content.index("[lo]")
        assert active_pos < hi_pos < archived_pos < lo_pos

    def test_save_discards_very_low_score(self, tmp_path: Path):
        md = tmp_path / "MEMORY.md"
        store = MemoryStore(md)
        entries = [
            _make_entry(id="keep", score=0.3),
            _make_entry(id="drop", score=0.04),
        ]
        store.save(entries)
        content = md.read_text(encoding="utf-8")
        assert "keep" in content
        assert "drop" not in content

    def test_save_sorts_by_score_descending(self, tmp_path: Path):
        md = tmp_path / "MEMORY.md"
        store = MemoryStore(md)
        entries = [
            _make_entry(id="low", score=0.5),
            _make_entry(id="high", score=0.9),
            _make_entry(id="mid", score=0.7),
        ]
        store.save(entries)
        content = md.read_text(encoding="utf-8")
        assert content.index("[high]") < content.index("[mid]") < content.index("[low]")


class TestMemoryStoreRoundTrip:
    """Save then load should preserve data."""

    def test_round_trip(self, tmp_path: Path):
        md = tmp_path / "MEMORY.md"
        store = MemoryStore(md)
        original = [
            _make_entry(id="aaa111", content="记忆 A", score=0.9),
            _make_entry(id="bbb222", content="记忆 B", score=0.6),
            _make_entry(id="ccc333", content="记忆 C", score=0.15),
        ]
        store.save(original)
        loaded = store.load()
        assert len(loaded) == 3
        ids = {e.id for e in loaded}
        assert ids == {"aaa111", "bbb222", "ccc333"}
        for entry in loaded:
            orig = next(e for e in original if e.id == entry.id)
            assert entry.content == orig.content
            assert abs(entry.score - orig.score) < 0.01
