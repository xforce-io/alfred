"""Integration tests for Memory Review's recoverable commit boundary (#188)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from src.everbot.core.jobs.memory_review import run
from src.everbot.core.memory.manager import IntegrityError, MemoryManager
from src.everbot.core.memory.models import MemoryEntry
from src.everbot.core.runtime.skill_context import SkillContext
from src.everbot.core.scanners.reflection_state import ReflectionState


def _seed(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    session_id = "web_session_demo_atomic"
    (sessions / f"{session_id}.json").write_text(json.dumps({
        "session_id": session_id,
        "updated_at": "2026-08-02T08:00:00+00:00",
        "session_type": "primary",
        "agent_name": "demo",
        "history_messages": [{"role": "user", "content": "我现在不负责项目 A"}],
    }, ensure_ascii=False), encoding="utf-8")
    manager = MemoryManager(tmp_path / "MEMORY.md")
    manager.store.save([MemoryEntry(
        id="old001",
        content="用户负责项目 A",
        category="fact",
        score=0.8,
        created_at="2026-06-01T00:00:00+00:00",
        last_activated="2026-07-01T00:00:00+00:00",
        activation_count=1,
        source_session="old",
    )])
    (tmp_path / "USER.md").write_text("# 用户画像\n\n- 项目: 负责 A\n", encoding="utf-8")
    context = SkillContext(
        sessions_dir=sessions,
        workspace_path=tmp_path,
        agent_name="demo",
        memory_manager=manager,
        mailbox=AsyncMock(),
        llm=AsyncMock(),
        scan_result=None,
    )
    review = json.dumps({
        "corrections": [{
            "content": "用户现在不负责项目 A",
            "category": "fact",
            "supersedes_ids": ["old001"],
            "source_session": session_id,
        }],
    }, ensure_ascii=False)
    return context, review


def _snapshots(tmp_path):
    paths = [tmp_path / "MEMORY.md", tmp_path / "USER.md", tmp_path / ".reflection_state.json"]
    return {path: path.read_bytes() if path.exists() else None for path in paths}


def _assert_snapshots(tmp_path, expected):
    assert _snapshots(tmp_path) == expected
    assert not ReflectionState.load(tmp_path).get_watermark("memory-review")


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ConnectionError("offline"), TimeoutError("timeout")])
async def test_llm_failure_does_not_mutate_memory_profile_or_watermark(tmp_path, error):
    context, _review = _seed(tmp_path)
    before = _snapshots(tmp_path)
    context.llm.complete.side_effect = error

    with pytest.raises(type(error)):
        await run(context)

    _assert_snapshots(tmp_path, before)


@pytest.mark.asyncio
async def test_compression_timeout_does_not_apply_completed_analysis(tmp_path):
    context, review = _seed(tmp_path)
    before = _snapshots(tmp_path)
    context.llm.complete.side_effect = [review, TimeoutError("compression timeout")]

    with pytest.raises(TimeoutError, match="compression timeout"):
        await run(context)

    _assert_snapshots(tmp_path, before)


@pytest.mark.asyncio
async def test_profile_write_failure_rolls_back_memory_and_retries_successfully(tmp_path):
    context, review = _seed(tmp_path)
    before = _snapshots(tmp_path)
    context.llm.complete.side_effect = [review, "- 项目状态: 不再负责项目 A"]

    with patch(
        "src.everbot.core.jobs.memory_review._atomic_write_profile",
        side_effect=OSError("disk full"),
    ):
        with pytest.raises(OSError, match="disk full"):
            await run(context)

    _assert_snapshots(tmp_path, before)

    context.llm = AsyncMock()
    context.llm.complete.side_effect = [review, "- 项目状态: 不再负责项目 A"]
    result = await run(context)
    assert result == "profile_rebuilt:1; corrected=1; split=0"
    assert ReflectionState.load(tmp_path).get_watermark("memory-review")


@pytest.mark.asyncio
async def test_watermark_failure_rolls_back_memory_and_user(tmp_path):
    context, review = _seed(tmp_path)
    before = _snapshots(tmp_path)
    context.llm.complete.side_effect = [review, "- 项目状态: 不再负责项目 A"]

    with patch.object(ReflectionState, "save", return_value=False):
        with pytest.raises(OSError, match="watermark"):
            await run(context)

    _assert_snapshots(tmp_path, before)


@pytest.mark.asyncio
async def test_concurrent_memory_insert_is_preserved_and_review_retries(tmp_path):
    context, review = _seed(tmp_path)
    calls = 0

    async def complete(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entries = context.memory_manager.load_entries()
            entries.append(MemoryEntry(
                id="newcon",
                content="用户偏好简洁输出",
                category="preference",
                score=0.8,
                created_at="2026-08-02T08:00:00+00:00",
                last_activated="2026-08-02T08:00:00+00:00",
                activation_count=1,
                source_session="concurrent-session",
            ))
            context.memory_manager.store.save(entries)
            return review
        return "- 项目状态: 不再负责项目 A"

    context.llm.complete.side_effect = complete

    with pytest.raises(IntegrityError, match="concurrently"):
        await run(context)

    entries = {entry.id: entry for entry in context.memory_manager.load_entries()}
    assert "newcon" in entries
    assert entries["old001"].status == "active"
    assert not ReflectionState.load(tmp_path).get_watermark("memory-review")
    assert "负责 A" in (tmp_path / "USER.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_integrity_error_before_review_write_does_not_restore_over_concurrent_insert(
    tmp_path,
):
    """A pre-write CAS failure must not restore snapshots over another writer."""
    context, review = _seed(tmp_path)
    context.llm.complete.side_effect = [review, "- 项目状态: 不再负责项目 A"]
    original_commit = context.memory_manager.commit_review

    def insert_then_reject(*args, **kwargs):
        entries = context.memory_manager.load_entries()
        entries.append(MemoryEntry(
            id="latecon",
            content="用户偏好简洁输出",
            category="preference",
            score=0.8,
            created_at="2026-08-02T08:00:00+00:00",
            last_activated="2026-08-02T08:00:00+00:00",
            activation_count=1,
            source_session="late-session",
        ))
        context.memory_manager.store.save(entries)
        raise IntegrityError("Memory changed concurrently; review must retry")

    context.memory_manager.commit_review = insert_then_reject
    try:
        with pytest.raises(IntegrityError, match="concurrently"):
            await run(context)
    finally:
        context.memory_manager.commit_review = original_commit

    entries = {entry.id: entry for entry in context.memory_manager.load_entries()}
    assert "latecon" in entries
    assert entries["old001"].status == "active"
    assert not ReflectionState.load(tmp_path).get_watermark("memory-review")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corrections",
    [
        [{
            "content": "用户现在不负责项目 A",
            "category": "fact",
            "supersedes_ids": ["old001"],
            "source_session": "outside-review-batch",
        }],
        [
            {
                "content": "用户现在不负责项目 A",
                "category": "fact",
                "supersedes_ids": ["old001"],
                "source_session": "web_session_demo_atomic",
            },
            {
                "content": "用户偏好简洁输出",
                "category": "preference",
                "supersedes_ids": ["old001"],
                "source_session": "outside-review-batch",
            },
        ],
    ],
)
async def test_untrusted_correction_source_fails_without_advancing_watermark(
    tmp_path, corrections,
):
    context, _review = _seed(tmp_path)
    before = _snapshots(tmp_path)
    context.llm.complete.return_value = json.dumps(
        {"corrections": corrections}, ensure_ascii=False,
    )

    with pytest.raises(ValueError, match="source_session"):
        await run(context)

    _assert_snapshots(tmp_path, before)


@pytest.mark.asyncio
async def test_single_session_missing_source_is_backfilled(tmp_path):
    context, _review = _seed(tmp_path)
    context.llm.complete.side_effect = [
        json.dumps({
            "corrections": [{
                "content": "用户现在不负责项目 A",
                "category": "fact",
                "supersedes_ids": ["old001"],
            }],
        }, ensure_ascii=False),
        "- 项目状态: 不再负责项目 A",
    ]

    result = await run(context)

    assert result == "profile_rebuilt:1; corrected=1; split=0"
    replacement = next(
        entry for entry in context.memory_manager.load_entries()
        if entry.status == "active"
    )
    assert replacement.source_session == "web_session_demo_atomic"


@pytest.mark.asyncio
async def test_multi_session_missing_source_fails_without_advancing_watermark(tmp_path):
    context, _review = _seed(tmp_path)
    second_id = "web_session_demo_second"
    (context.sessions_dir / f"{second_id}.json").write_text(json.dumps({
        "session_id": second_id,
        "updated_at": "2026-08-02T08:01:00+00:00",
        "session_type": "primary",
        "agent_name": "demo",
        "history_messages": [{"role": "user", "content": "补充说明"}],
    }, ensure_ascii=False), encoding="utf-8")
    before = _snapshots(tmp_path)
    context.llm.complete.return_value = json.dumps({
        "corrections": [{
            "content": "用户现在不负责项目 A",
            "category": "fact",
            "supersedes_ids": ["old001"],
        }],
    }, ensure_ascii=False)

    with pytest.raises(ValueError, match="source_session"):
        await run(context)

    _assert_snapshots(tmp_path, before)


@pytest.mark.asyncio
async def test_watermark_never_advances_past_sessions_sent_to_judge(tmp_path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    timestamps = [f"2026-08-02T08:0{i}:00+00:00" for i in range(1, 6)]
    session_ids = [f"web_session_demo_backlog_{i}" for i in range(1, 6)]
    for index, (session_id, updated_at) in enumerate(
        zip(session_ids, timestamps), start=1,
    ):
        user_text = (
            "我现在不负责项目 A"
            if index == 5
            else f"普通对话 {index}"
        )
        (sessions_dir / f"{session_id}.json").write_text(json.dumps({
            "session_id": session_id,
            "updated_at": updated_at,
            "session_type": "primary",
            "agent_name": "demo",
            "history_messages": [{"role": "user", "content": user_text}],
        }, ensure_ascii=False), encoding="utf-8")

    manager = MemoryManager(tmp_path / "MEMORY.md")
    manager.store.save([MemoryEntry(
        id="old001",
        content="用户负责项目 A",
        category="fact",
        score=0.8,
        created_at="2026-06-01T00:00:00+00:00",
        last_activated="2026-07-01T00:00:00+00:00",
        activation_count=1,
        source_session="old",
    )])
    (tmp_path / "USER.md").write_text(
        "# 用户画像\n\n- 项目: 负责 A\n", encoding="utf-8",
    )
    context = SkillContext(
        sessions_dir=sessions_dir,
        workspace_path=tmp_path,
        agent_name="demo",
        memory_manager=manager,
        mailbox=AsyncMock(),
        llm=AsyncMock(),
        scan_result=None,
    )
    analysis_prompts = []

    async def complete(prompt, **_kwargs):
        if "## Recent Conversation Context" in prompt:
            analysis_prompts.append(prompt)
            if "我现在不负责项目 A" in prompt:
                return json.dumps({
                    "corrections": [{
                        "content": "用户现在不负责项目 A",
                        "category": "fact",
                        "supersedes_ids": ["old001"],
                        "source_session": session_ids[4],
                    }],
                }, ensure_ascii=False)
            return "{}"
        return "- 项目状态: 不再负责项目 A"

    context.llm.complete.side_effect = complete

    await run(context)

    first_watermark = ReflectionState.load(tmp_path).get_watermark("memory-review")
    assert first_watermark == timestamps[2]
    assert session_ids[3] not in analysis_prompts[0]
    assert session_ids[4] not in analysis_prompts[0]
    assert manager.load_entries()[0].status == "active"

    await run(context)

    assert ReflectionState.load(tmp_path).get_watermark("memory-review") == timestamps[4]
    entries = manager.load_entries()
    assert next(entry for entry in entries if entry.id == "old001").status == "superseded"
    active = [entry for entry in entries if entry.status == "active"]
    assert [entry.content for entry in active] == ["用户现在不负责项目 A"]


@pytest.mark.asyncio
async def test_no_session_bootstrap_rejects_stale_profile_after_memory_change(tmp_path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    manager = MemoryManager(tmp_path / "MEMORY.md")
    manager.store.save([MemoryEntry(
        id="old001",
        content="用户负责项目 A",
        category="fact",
        score=0.8,
        created_at="2026-06-01T00:00:00+00:00",
        last_activated="2026-07-01T00:00:00+00:00",
        activation_count=1,
        source_session="old",
    )])
    user_path = tmp_path / "USER.md"
    user_path.write_text("# 用户画像\n\nlegacy profile\n", encoding="utf-8")
    context = SkillContext(
        sessions_dir=sessions_dir,
        workspace_path=tmp_path,
        agent_name="demo",
        memory_manager=manager,
        mailbox=AsyncMock(),
        llm=AsyncMock(),
        scan_result=None,
    )

    async def mutate_memory_then_render(*_args, **_kwargs):
        entries = manager.load_entries()
        entries.append(MemoryEntry(
            id="newcon",
            content="用户偏好简洁输出",
            category="preference",
            score=0.8,
            created_at="2026-08-02T08:00:00+00:00",
            last_activated="2026-08-02T08:00:00+00:00",
            activation_count=1,
            source_session="concurrent-session",
        ))
        manager.store.save(entries)
        return "- 项目状态: 负责项目 A"

    context.llm.complete.side_effect = mutate_memory_then_render

    with pytest.raises(IntegrityError, match="concurrently"):
        await run(context)

    assert user_path.read_text(encoding="utf-8") == "# 用户画像\n\nlegacy profile\n"
    assert {entry.id for entry in manager.load_entries()} == {"old001", "newcon"}


@pytest.mark.asyncio
async def test_no_session_bootstrap_write_failure_restores_user_profile(tmp_path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    manager = MemoryManager(tmp_path / "MEMORY.md")
    manager.store.save([MemoryEntry(
        id="old001",
        content="用户负责项目 A",
        category="fact",
        score=0.8,
        created_at="2026-06-01T00:00:00+00:00",
        last_activated="2026-07-01T00:00:00+00:00",
        activation_count=1,
        source_session="old",
    )])
    user_path = tmp_path / "USER.md"
    original_user = "# 用户画像\n\nlegacy profile\n"
    user_path.write_text(original_user, encoding="utf-8")
    context = SkillContext(
        sessions_dir=sessions_dir,
        workspace_path=tmp_path,
        agent_name="demo",
        memory_manager=manager,
        mailbox=AsyncMock(),
        llm=AsyncMock(),
        scan_result=None,
    )
    context.llm.complete.return_value = "- 项目状态: 负责项目 A"

    def partial_write_then_fail(_path, _content):
        user_path.write_text("partial profile", encoding="utf-8")
        raise OSError("disk full after replace")

    with patch(
        "src.everbot.core.jobs.memory_review._atomic_write_profile",
        side_effect=partial_write_then_fail,
    ):
        with pytest.raises(OSError, match="disk full"):
            await run(context)

    assert user_path.read_text(encoding="utf-8") == original_user
    assert manager.load_entries()[0].status == "active"
