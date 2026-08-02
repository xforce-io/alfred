"""Integration tests for Memory Review's recoverable commit boundary (#188)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from src.everbot.core.jobs.memory_review import run
from src.everbot.core.memory.manager import MemoryManager
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

    from src.everbot.core.memory.manager import IntegrityError
    with pytest.raises(IntegrityError, match="concurrently"):
        await run(context)

    entries = {entry.id: entry for entry in context.memory_manager.load_entries()}
    assert "newcon" in entries
    assert entries["old001"].status == "active"
    assert not ReflectionState.load(tmp_path).get_watermark("memory-review")
    assert "负责 A" in (tmp_path / "USER.md").read_text(encoding="utf-8")
