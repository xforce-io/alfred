"""Tests for MemoryMerger — scoring, reinforcement, decay, merge."""

import pytest
from datetime import datetime, timedelta, timezone

from src.everbot.core.memory.merger import MemoryMerger, MergeResult
from src.everbot.core.memory.models import MemoryEntry


def _make_entry(**overrides) -> MemoryEntry:
    defaults = dict(
        id="abc123",
        content="测试记忆",
        category="fact",
        score=0.6,
        created_at="2026-02-01T00:00:00+00:00",
        last_activated="2026-02-20T00:00:00+00:00",
        activation_count=3,
        source_session="session_001",
    )
    defaults.update(overrides)
    return MemoryEntry(**defaults)


class TestCreateEntry:
    """Initial scoring based on importance."""

    def test_high_importance(self):
        merger = MemoryMerger()
        entry = merger.create_entry("重要偏好", "preference", importance="high")
        assert entry.score == 0.8
        assert entry.category == "preference"
        assert entry.activation_count == 1

    def test_medium_importance(self):
        merger = MemoryMerger()
        entry = merger.create_entry("一般信息", "fact", importance="medium")
        assert entry.score == 0.6

    def test_low_importance(self):
        merger = MemoryMerger()
        entry = merger.create_entry("次要信息", "fact", importance="low")
        assert entry.score == 0.4

    def test_unknown_importance_defaults_medium(self):
        merger = MemoryMerger()
        entry = merger.create_entry("未知", "fact", importance="unknown")
        assert entry.score == 0.6


class TestReinforce:
    """Reinforcement with diminishing returns."""

    def test_reinforce_boosts_score(self):
        merger = MemoryMerger()
        entry = _make_entry(score=0.6, activation_count=3)
        merger.reinforce(entry)
        # 0.6 + (1.0 - 0.6) * 0.2 = 0.6 + 0.08 = 0.68
        assert abs(entry.score - 0.68) < 0.001
        assert entry.activation_count == 4

    def test_reinforce_diminishing_returns(self):
        merger = MemoryMerger()
        entry = _make_entry(score=0.9, activation_count=10)
        merger.reinforce(entry)
        # 0.9 + (1.0 - 0.9) * 0.2 = 0.9 + 0.02 = 0.92
        assert abs(entry.score - 0.92) < 0.001

    def test_reinforce_low_score(self):
        merger = MemoryMerger()
        entry = _make_entry(score=0.2, activation_count=1)
        merger.reinforce(entry)
        # 0.2 + (1.0 - 0.2) * 0.2 = 0.2 + 0.16 = 0.36
        assert abs(entry.score - 0.36) < 0.001


class TestDecay:
    """Time-based decay with 7-day protection."""

    def test_no_decay_within_protection_period(self):
        merger = MemoryMerger()
        now = datetime(2026, 2, 20, tzinfo=timezone.utc)
        entry = _make_entry(
            score=0.8,
            last_activated=(now - timedelta(days=5)).isoformat(),
        )
        merger.apply_decay([entry], now=now)
        assert entry.score == 0.8  # No change within 7 days

    def test_decay_after_protection_period(self):
        merger = MemoryMerger()
        now = datetime(2026, 2, 20, tzinfo=timezone.utc)
        entry = _make_entry(
            score=0.8,
            last_activated=(now - timedelta(days=17)).isoformat(),
        )
        merger.apply_decay([entry], now=now)
        # 10 days of decay: 0.8 * 0.99^10 ≈ 0.7234
        expected = 0.8 * (0.99 ** 10)
        assert abs(entry.score - expected) < 0.001

    def test_decay_exactly_at_boundary(self):
        merger = MemoryMerger()
        now = datetime(2026, 2, 20, tzinfo=timezone.utc)
        entry = _make_entry(
            score=0.8,
            last_activated=(now - timedelta(days=7)).isoformat(),
        )
        merger.apply_decay([entry], now=now)
        # Exactly 7 days: no decay (days > 7, not >=)
        assert entry.score == 0.8

    def test_long_decay_pushes_below_archive_threshold(self):
        merger = MemoryMerger()
        now = datetime(2026, 2, 20, tzinfo=timezone.utc)
        entry = _make_entry(
            score=0.3,
            last_activated=(now - timedelta(days=200)).isoformat(),
        )
        merger.apply_decay([entry], now=now)
        # 193 days of decay: 0.3 * 0.99^193 ≈ 0.043 < 0.05
        assert entry.score < 0.05


class TestMerge:
    """Merging new extractions, reinforcements, and existing entries."""

    def test_merge_adds_new_entries(self):
        merger = MemoryMerger()
        existing = [_make_entry(id="old1")]
        new_extractions = [
            {"content": "新记忆", "category": "fact", "importance": "high"},
        ]
        result = merger.merge(existing, new_extractions, [], source_session="s1")
        assert result.new_count == 1
        assert result.updated_count == 0
        assert len(result.entries) == 2

    def test_merge_reinforces_existing(self):
        merger = MemoryMerger()
        entry = _make_entry(id="old1", score=0.6, activation_count=3)
        result = merger.merge([entry], [], ["old1"], source_session="s1")
        assert result.updated_count == 1
        assert result.new_count == 0
        reinforced = next(e for e in result.entries if e.id == "old1")
        assert reinforced.score > 0.6
        assert reinforced.activation_count == 4

    def test_merge_skips_unknown_reinforcement_ids(self):
        merger = MemoryMerger()
        existing = [_make_entry(id="old1")]
        result = merger.merge(existing, [], ["nonexistent"], source_session="s1")
        assert result.updated_count == 0

    def test_merge_combined(self):
        merger = MemoryMerger()
        existing = [
            _make_entry(id="old1", score=0.7),
            _make_entry(id="old2", score=0.5),
        ]
        new_extractions = [
            {"content": "新的", "category": "preference", "importance": "medium"},
        ]
        result = merger.merge(existing, new_extractions, ["old1"], source_session="s1")
        assert result.new_count == 1
        assert result.updated_count == 1
        assert len(result.entries) == 3


class TestContentFilter:
    """Tests for the content_filter parameter in merge()."""

    @staticmethod
    def _internal_filter(content: str) -> bool:
        return "HEARTBEAT" in content or "内部系统" in content

    def test_filter_blocks_new_internal_entries(self):
        merger = MemoryMerger()
        existing = [_make_entry(id="old1")]
        new_extractions = [
            {"content": "HEARTBEAT.md 是心跳文件", "category": "fact", "importance": "high"},
            {"content": "用户喜欢Python", "category": "preference", "importance": "medium"},
        ]
        result = merger.merge(
            existing, new_extractions, [], source_session="s1",
            content_filter=self._internal_filter,
        )
        # Only the non-internal entry should be created
        assert result.new_count == 1
        contents = [e.content for e in result.entries]
        assert "用户喜欢Python" in contents
        assert "HEARTBEAT.md 是心跳文件" not in contents

    def test_filter_skips_reinforcement_of_internal_entries(self):
        merger = MemoryMerger()
        internal_entry = _make_entry(id="bad1", content="内部系统架构说明", score=0.7, activation_count=3)
        normal_entry = _make_entry(id="good1", content="用户偏好", score=0.6, activation_count=2)
        result = merger.merge(
            [internal_entry, normal_entry], [], ["bad1", "good1"],
            source_session="s1", content_filter=self._internal_filter,
        )
        # Internal entry should NOT be reinforced
        bad = next(e for e in result.entries if e.id == "bad1")
        assert bad.activation_count == 3  # unchanged
        # Normal entry should be reinforced
        good = next(e for e in result.entries if e.id == "good1")
        assert good.activation_count == 3  # incremented
        assert result.updated_count == 1  # only good1 counted

    def test_filter_suppresses_existing_bad_entries(self):
        merger = MemoryMerger()
        bad_entry = _make_entry(id="bad1", content="HEARTBEAT.md记录心跳", score=0.94)
        good_entry = _make_entry(id="good1", content="用户喜欢咖啡", score=0.8)
        result = merger.merge(
            [bad_entry, good_entry], [], [], source_session="s1",
            content_filter=self._internal_filter,
        )
        bad = next(e for e in result.entries if e.id == "bad1")
        good = next(e for e in result.entries if e.id == "good1")
        # Bad entry score halved: 0.94 * 0.5 = 0.47, below 0.5 injection threshold
        assert abs(bad.score - 0.47) < 0.001
        # Good entry untouched
        assert abs(good.score - 0.8) < 0.001
