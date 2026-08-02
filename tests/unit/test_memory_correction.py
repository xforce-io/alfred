"""Unit tests for explicit memory correction and atomic fact boundaries (#188)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.everbot.core.jobs.memory_review import _analyze_memory_consolidation
from src.everbot.core.memory.manager import MemoryManager
from src.everbot.core.memory.models import MemoryEntry


def _entry(entry_id: str, content: str, *, score: float = 0.8) -> MemoryEntry:
    return MemoryEntry(
        id=entry_id,
        content=content,
        category="fact",
        score=score,
        created_at="2026-07-01T00:00:00+00:00",
        last_activated="2026-07-01T00:00:00+00:00",
        activation_count=1,
        source_session="old-session",
    )


def test_explicit_correction_supersedes_old_fact_and_is_idempotent(tmp_path):
    manager = MemoryManager(tmp_path / "MEMORY.md")
    existing = [_entry("old001", "用户负责项目 A")]
    review = {
        "corrections": [{
            "content": "用户现在不负责项目 A",
            "category": "fact",
            "supersedes_ids": ["old001"],
            "source_session": "web_session_demo_42",
        }],
    }

    projected, stats = manager.preview_review(review, existing)
    active = [entry for entry in projected if entry.status == "active"]
    old = next(entry for entry in projected if entry.id == "old001")

    assert stats["corrected"] == 1
    assert len(active) == 1
    assert active[0].content == "用户现在不负责项目 A"
    assert active[0].supersedes == ["old001"]
    assert active[0].source_session == "web_session_demo_42"
    assert old.status == "superseded"
    assert old.superseded_by == [active[0].id]

    replayed, replay_stats = manager.preview_review(review, projected)
    assert replay_stats["corrected"] == 0
    assert len(replayed) == len(projected)


def test_partially_overlapping_corrections_coalesce_one_active_replacement(tmp_path):
    manager = MemoryManager(tmp_path / "MEMORY.md")
    existing = [
        _entry("old001", "用户负责项目 A 的研发"),
        _entry("old002", "用户负责项目 A 的发布"),
    ]
    review = {
        "corrections": [
            {
                "content": "用户现在不负责项目 A",
                "category": "fact",
                "supersedes_ids": ["old001"],
                "source_session": "web_session_demo_42",
            },
            {
                "content": "用户现在不负责项目 A",
                "category": "fact",
                "supersedes_ids": ["old001", "old002"],
                "source_session": "web_session_demo_42",
            },
        ],
    }

    projected, stats = manager.preview_review(review, existing)

    active = [entry for entry in projected if entry.status == "active"]
    assert len(active) == 1
    assert active[0].content == "用户现在不负责项目 A"
    assert set(active[0].supersedes) == {"old001", "old002"}
    for source_id in ("old001", "old002"):
        source = next(entry for entry in projected if entry.id == source_id)
        assert source.status == "superseded"
        assert source.superseded_by == [active[0].id]
    assert stats["corrected"] == 2

    replayed, replay_stats = manager.preview_review(review, projected)
    assert replay_stats["corrected"] == 0
    assert [entry.to_dict() for entry in replayed] == [
        entry.to_dict() for entry in projected
    ]


def test_correction_without_active_conflict_is_rejected(tmp_path):
    manager = MemoryManager(tmp_path / "MEMORY.md")
    existing = [_entry("other1", "用户喜欢 Python")]

    projected, stats = manager.preview_review({
        "corrections": [{
            "content": "用户现在不负责项目 A",
            "category": "fact",
            "supersedes_ids": ["missing"],
        }],
    }, existing)

    assert stats["corrected"] == 0
    assert [entry.to_dict() for entry in projected] == [entry.to_dict() for entry in existing]


def test_split_giant_record_creates_independent_durable_facts(tmp_path):
    manager = MemoryManager(tmp_path / "MEMORY.md")
    giant = _entry(
        "giant1",
        "用户偏好简洁输出；负责项目A；昨天连接超时；临时排查任务进行中",
        score=0.22,
    )
    review = {
        "split_entries": [{
            "id": "giant1",
            "entries": [
                {"content": "用户偏好简洁输出", "category": "preference", "importance": "high"},
                {"content": "用户负责项目 A", "category": "fact", "importance": "medium"},
                {"content": "昨天连接超时", "category": "incident", "importance": "low"},
            ],
        }],
    }

    projected, stats = manager.preview_review(review, [giant])
    active = [entry for entry in projected if entry.status == "active"]
    source = next(entry for entry in projected if entry.id == "giant1")

    assert stats["split"] == 1
    assert {entry.content for entry in active} == {"用户偏好简洁输出", "用户负责项目 A"}
    assert all(entry.supersedes == ["giant1"] for entry in active)
    assert source.status == "superseded"
    assert set(source.superseded_by) == {entry.id for entry in active}


def test_prompt_memory_excludes_superseded_entries(tmp_path):
    manager = MemoryManager(tmp_path / "MEMORY.md")
    old = _entry("old001", "用户负责项目 A")
    old.status = "superseded"
    old.superseded_by = ["new001"]
    new = _entry("new001", "用户现在不负责项目 A")
    new.supersedes = ["old001"]
    manager.store.save([old, new])

    prompt = manager.get_prompt_memories()

    assert "用户现在不负责项目 A" in prompt
    assert "用户负责项目 A" not in prompt

    recalled = manager.recall("项目 A", kind="profile", top_k=10)
    assert [item["id"] for item in recalled] == ["new001"]


@pytest.mark.asyncio
async def test_review_prompt_keeps_recent_correction_from_long_digest():
    llm = AsyncMock()
    llm.complete.return_value = "{}"
    digest = (
        "[source_session:web_session_demo_long]\n"
        + ("[assistant] earlier context\n" * 100)
        + "[user] 我现在不负责项目 A 啦"
    )

    await _analyze_memory_consolidation(
        llm,
        [digest],
        [_entry("old001", "用户负责项目 A")],
    )

    prompt = llm.complete.await_args.args[0]
    assert "source_session:web_session_demo_long" in prompt
    assert "我现在不负责项目 A 啦" in prompt


@pytest.mark.asyncio
async def test_review_prompt_excludes_superseded_entries():
    llm = AsyncMock()
    llm.complete.return_value = "{}"
    old = _entry("old001", "用户负责项目 A")
    old.status = "superseded"
    old.superseded_by = ["new001"]
    new = _entry("new001", "用户现在不负责项目 A")
    new.supersedes = ["old001"]

    await _analyze_memory_consolidation(
        llm,
        ["[source_session:web_session_demo_new]\n[user] 普通对话"],
        [old, new],
    )

    prompt = llm.complete.await_args.args[0]
    assert "用户现在不负责项目 A" in prompt
    assert "用户负责项目 A" not in prompt
    assert "[old001]" not in prompt
