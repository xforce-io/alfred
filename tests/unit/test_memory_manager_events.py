"""Tests for MemoryManager — event pipeline + dual-block prompt injection.

These tests exercise the integration between profile and event memory:
- ``process_session_end`` runs both extraction paths
- ``get_prompt_memories`` returns the concatenated profile + event block
- Failures in one path leave the other untouched
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.everbot.core.memory.event_store import EventStore
from src.everbot.core.memory.manager import MemoryManager
from src.everbot.core.memory.models import MemoryEntry
from src.everbot.core.memory.profile_store import ProfileStore


def _ctx():
    ctx = MagicMock()
    config = MagicMock()
    config.fast_llm = "test-model"
    ctx.get_config.return_value = config
    return ctx


def _patch_extractors(profile_payload: dict, event_payload: dict):
    """Patch both extractors' _call_llm to return the given JSON payloads."""
    return [
        patch(
            "src.everbot.core.memory.profile_extractor.ProfileExtractor._call_llm",
            new_callable=AsyncMock,
            return_value=json.dumps(profile_payload),
        ),
        patch(
            "src.everbot.core.memory.event_extractor.EventExtractor._call_llm",
            new_callable=AsyncMock,
            return_value=json.dumps(event_payload),
        ),
    ]


def _seed_event(events_dir: Path, **overrides) -> MemoryEntry:
    defaults = dict(
        id="seed01",
        content="种子事件",
        category="decision",
        score=0.7,
        created_at="2026-05-01T00:00:00+00:00",
        last_activated="2026-05-01T00:00:00+00:00",
        activation_count=1,
        source_session="s0",
        kind="event",
        event_at="2026-05-01T10:00:00+00:00",
        due_at=None,
    )
    defaults.update(overrides)
    entry = MemoryEntry(**defaults)
    EventStore(events_dir).append([entry])
    return entry


# =====================================================================
# Dual pipeline
# =====================================================================


@pytest.mark.asyncio
class TestDualPipeline:
    async def test_both_layers_get_written(self, tmp_path: Path):
        md = tmp_path / "MEMORY.md"
        events_dir = tmp_path / "events"
        profile_payload = {
            "new_memories": [{"content": "用户喜欢简洁代码", "category": "preference",
                              "importance": "high"}],
            "reinforced_ids": [],
        }
        event_payload = {
            "new_events": [{
                "content": "决定切到 deepseek-chat",
                "category": "decision",
                "event_at": "2026-05-01T10:30:00+00:00",
                "importance": "high",
            }]
        }
        with _patch_extractors(profile_payload, event_payload)[0], \
             _patch_extractors(profile_payload, event_payload)[1]:
            mm = MemoryManager(md, _ctx(), events_dir=events_dir)
            stats = await mm.process_session_end(
                [{"role": "user", "content": "切到 deepseek-chat"}], "s1"
            )

        assert stats["profile"]["new_count"] == 1
        assert stats["event"]["new_count"] == 1
        assert ProfileStore(md).load()[0].content == "用户喜欢简洁代码"
        assert EventStore(events_dir).load_all()[0].content == "决定切到 deepseek-chat"

    async def test_event_failure_does_not_break_profile(self, tmp_path: Path):
        md = tmp_path / "MEMORY.md"
        events_dir = tmp_path / "events"
        profile_payload = {
            "new_memories": [{"content": "用户偏好 X", "category": "preference",
                              "importance": "medium"}],
            "reinforced_ids": [],
        }
        with patch(
            "src.everbot.core.memory.profile_extractor.ProfileExtractor._call_llm",
            new_callable=AsyncMock, return_value=json.dumps(profile_payload),
        ), patch(
            "src.everbot.core.memory.event_extractor.EventExtractor._call_llm",
            new_callable=AsyncMock, side_effect=RuntimeError("LLM down"),
        ):
            mm = MemoryManager(md, _ctx(), events_dir=events_dir)
            stats = await mm.process_session_end(
                [{"role": "user", "content": "x"}], "s1"
            )

        assert stats["profile"]["new_count"] == 1
        assert stats["event"]["new_count"] == 0
        assert len(ProfileStore(md).load()) == 1

    async def test_no_context_returns_empty_stats_for_both(self, tmp_path: Path):
        md = tmp_path / "MEMORY.md"
        mm = MemoryManager(md, context=None, events_dir=tmp_path / "events")
        stats = await mm.process_session_end(
            [{"role": "user", "content": "hi"}], "s1"
        )
        assert stats["profile"]["new_count"] == 0
        assert stats["event"]["new_count"] == 0

    async def test_default_events_dir_alongside_memory_md(self, tmp_path: Path):
        md = tmp_path / "MEMORY.md"
        # Don't pass events_dir — default = memory_path.parent / "events"
        mm = MemoryManager(md, _ctx())
        event_payload = {
            "new_events": [{
                "content": "事件",
                "category": "decision",
                "event_at": "2026-05-01T10:00:00+00:00",
                "importance": "medium",
            }]
        }
        with patch(
            "src.everbot.core.memory.profile_extractor.ProfileExtractor._call_llm",
            new_callable=AsyncMock, return_value='{"new_memories": [], "reinforced_ids": []}',
        ), patch(
            "src.everbot.core.memory.event_extractor.EventExtractor._call_llm",
            new_callable=AsyncMock, return_value=json.dumps(event_payload),
        ):
            await mm.process_session_end([{"role": "user", "content": "x"}], "s1")
        assert (tmp_path / "events" / "2026-05.md").exists()


# =====================================================================
# get_prompt_memories
# =====================================================================


class TestPromptInjection:
    def test_empty_returns_empty_string(self, tmp_path: Path):
        mm = MemoryManager(tmp_path / "MEMORY.md", events_dir=tmp_path / "events")
        assert mm.get_prompt_memories() == ""

    def test_profile_only_returns_only_profile_block(self, tmp_path: Path):
        md = tmp_path / "MEMORY.md"
        ProfileStore(md).save([
            MemoryEntry(
                id="p1", content="用户喜欢 Python", category="preference",
                score=0.9, created_at="2026-05-01", last_activated="2026-05-01",
                activation_count=1, source_session="s",
            )
        ])
        mm = MemoryManager(md, events_dir=tmp_path / "events")
        out = mm.get_prompt_memories()
        assert "# 历史记忆" in out
        assert "# 近期事件" not in out
        assert "Python" in out

    def test_events_only_returns_only_event_block(self, tmp_path: Path):
        events_dir = tmp_path / "events"
        now = datetime.now(timezone.utc)
        _seed_event(events_dir, event_at=(now - timedelta(days=2)).isoformat(),
                    score=0.7, content="切到 deepseek")
        mm = MemoryManager(tmp_path / "MEMORY.md", events_dir=events_dir)
        out = mm.get_prompt_memories()
        assert "# 历史记忆" not in out
        assert "# 近期事件" in out
        assert "deepseek" in out

    def test_both_blocks_concatenated_with_blank_line(self, tmp_path: Path):
        md = tmp_path / "MEMORY.md"
        events_dir = tmp_path / "events"
        ProfileStore(md).save([
            MemoryEntry(
                id="p1", content="用户喜欢 Python", category="preference",
                score=0.9, created_at="2026-05-01", last_activated="2026-05-01",
                activation_count=1, source_session="s",
            )
        ])
        now = datetime.now(timezone.utc)
        _seed_event(events_dir, event_at=(now - timedelta(days=1)).isoformat(),
                    score=0.7, content="切到 deepseek")

        mm = MemoryManager(md, events_dir=events_dir)
        out = mm.get_prompt_memories()
        # Profile block precedes event block
        profile_pos = out.index("# 历史记忆")
        event_pos = out.index("# 近期事件")
        assert profile_pos < event_pos
        # Separated by a blank line
        assert "\n\n# 近期事件" in out

    def test_event_outside_window_excluded(self, tmp_path: Path):
        events_dir = tmp_path / "events"
        # Event 100 days ago — well outside the 30-day default window
        old = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
        _seed_event(events_dir, event_at=old, score=0.9, content="老事件")

        mm = MemoryManager(tmp_path / "MEMORY.md", events_dir=events_dir)
        assert mm.get_prompt_memories() == ""

    def test_decayed_below_threshold_excluded(self, tmp_path: Path):
        events_dir = tmp_path / "events"
        # Event 25 days ago, initial score 0.4 → after decay ~0.4 * 0.5^(25/30) ≈ 0.225
        # 0.225 < 0.3 inject threshold → should be filtered
        old = (datetime.now(timezone.utc) - timedelta(days=25)).isoformat()
        _seed_event(events_dir, event_at=old, score=0.4, content="边缘事件")

        mm = MemoryManager(tmp_path / "MEMORY.md", events_dir=events_dir)
        assert mm.get_prompt_memories() == ""

    def test_due_at_rendered_in_event_line(self, tmp_path: Path):
        events_dir = tmp_path / "events"
        now = datetime.now(timezone.utc)
        _seed_event(
            events_dir,
            event_at=(now - timedelta(days=1)).isoformat(),
            category="todo",
            content="周五交付",
            score=0.7,
            due_at=(now + timedelta(days=2)).isoformat(),
        )
        mm = MemoryManager(tmp_path / "MEMORY.md", events_dir=events_dir)
        out = mm.get_prompt_memories()
        assert "(due: " in out

    def test_event_block_caps_at_top_k(self, tmp_path: Path):
        events_dir = tmp_path / "events"
        now = datetime.now(timezone.utc)
        for i in range(15):
            _seed_event(
                events_dir,
                id=f"evt{i:02d}",
                event_at=(now - timedelta(days=1)).isoformat(),
                score=0.7,
                content=f"事件 {i}",
            )

        mm = MemoryManager(tmp_path / "MEMORY.md", events_dir=events_dir)
        out = mm.get_prompt_memories(event_top_k=5)
        # Count rendered event lines (those starting with "- [")
        event_lines = [
            line for line in out.split("\n")
            if line.startswith("- [")
        ]
        assert len(event_lines) == 5


# =====================================================================
# recall (keyword search)
# =====================================================================


class TestRecall:
    def _seed(self, tmp_path: Path):
        md = tmp_path / "MEMORY.md"
        events_dir = tmp_path / "events"
        ProfileStore(md).save([
            MemoryEntry(
                id="p1", content="用户主要用 Python 开发后端", category="fact",
                score=0.9, created_at="2026-05-01", last_activated="2026-05-01",
                activation_count=1, source_session="s",
            ),
            MemoryEntry(
                id="p2", content="用户偏好简洁的代码风格", category="preference",
                score=0.8, created_at="2026-05-01", last_activated="2026-05-01",
                activation_count=1, source_session="s",
            ),
        ])
        _seed_event(events_dir, id="e1", content="切到 deepseek-chat 模型",
                    event_at="2026-05-01T10:00:00+00:00")
        _seed_event(events_dir, id="e2", content="周五交付 KWeaver demo",
                    event_at="2026-05-02T10:00:00+00:00")
        return MemoryManager(md, events_dir=events_dir)

    def test_recall_both_layers_by_default(self, tmp_path: Path):
        mm = self._seed(tmp_path)
        results = mm.recall("python", kind="both")
        assert any(r["id"] == "p1" for r in results)
        assert all(r["rank_score"] > 0 for r in results)

    def test_recall_profile_only(self, tmp_path: Path):
        mm = self._seed(tmp_path)
        results = mm.recall("代码", kind="profile")
        assert all(r["kind"] == "profile" for r in results)

    def test_recall_event_only(self, tmp_path: Path):
        mm = self._seed(tmp_path)
        results = mm.recall("deepseek", kind="event")
        assert len(results) == 1
        assert results[0]["id"] == "e1"
        assert results[0]["kind"] == "event"

    def test_recall_no_matches_returns_empty(self, tmp_path: Path):
        mm = self._seed(tmp_path)
        assert mm.recall("rust") == []

    def test_recall_empty_memory_returns_empty(self, tmp_path: Path):
        mm = MemoryManager(tmp_path / "MEMORY.md", events_dir=tmp_path / "events")
        assert mm.recall("anything") == []

    def test_recall_top_k_caps_results(self, tmp_path: Path):
        mm = self._seed(tmp_path)
        # All 4 entries mention "用户" or 类似 — sort by relevance
        results = mm.recall("用户", kind="both", top_k=2)
        assert len(results) <= 2

    def test_recall_invalid_kind_raises(self, tmp_path: Path):
        mm = self._seed(tmp_path)
        with pytest.raises(ValueError):
            mm.recall("x", kind="garbage")

    def test_recall_event_window_filters(self, tmp_path: Path):
        events_dir = tmp_path / "events"
        now = datetime.now(timezone.utc)
        _seed_event(events_dir, id="recent",
                    event_at=(now - timedelta(days=2)).isoformat(),
                    content="最近的 deepseek 决定")
        _seed_event(events_dir, id="old",
                    event_at=(now - timedelta(days=200)).isoformat(),
                    content="古老的 deepseek 决定")

        mm = MemoryManager(tmp_path / "MEMORY.md", events_dir=events_dir)
        # days=30 → only "recent" should appear
        results = mm.recall("deepseek", kind="event", days=30)
        assert {r["id"] for r in results} == {"recent"}
        # days=None → both visible
        results = mm.recall("deepseek", kind="event", days=None)
        assert {r["id"] for r in results} == {"recent", "old"}

    def test_recall_payload_includes_rank_score(self, tmp_path: Path):
        mm = self._seed(tmp_path)
        results = mm.recall("deepseek")
        assert "rank_score" in results[0]
        # All MemoryEntry fields should also be present
        for key in ("id", "content", "category", "score", "kind", "event_at"):
            assert key in results[0]
