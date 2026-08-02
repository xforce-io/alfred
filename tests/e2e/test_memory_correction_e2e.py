"""E2E(#188): session correction → review → MEMORY/USER → next-session prompt."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from unittest.mock import patch

import pytest

from src.everbot.core.jobs.memory_review import run
from src.everbot.core.memory.manager import MemoryManager
from src.everbot.core.memory.models import MemoryEntry
from src.everbot.core.runtime.skill_context import SkillContext
from src.everbot.core.runtime.cron import CronExecutor
from src.everbot.core.runtime.cron_delivery import CronDelivery
from src.everbot.core.scanners.reflection_state import ReflectionState
from src.everbot.core.tasks.routine_manager import RoutineManager
from src.everbot.infra.workspace import WorkspaceLoader


def _old_assignment() -> MemoryEntry:
    return MemoryEntry(
        id="old001",
        content="用户是项目 A 的核心研发者并负责该项目",
        category="fact",
        score=0.8,
        created_at="2026-06-01T00:00:00+00:00",
        last_activated="2026-07-01T00:00:00+00:00",
        activation_count=3,
        source_session="old-session",
    )


def _write_session(path, *, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "session_id": path.stem,
        "updated_at": "2026-08-02T08:00:00+00:00",
        "session_type": "primary",
        "agent_name": "demo",
        "history_messages": [
            {"role": "user", "content": content},
            {"role": "assistant", "content": "我会更新记忆。"},
        ],
    }, ensure_ascii=False), encoding="utf-8")


def _context(tmp_path, manager, llm) -> SkillContext:
    return SkillContext(
        sessions_dir=tmp_path / "sessions",
        workspace_path=tmp_path,
        agent_name="demo",
        memory_manager=manager,
        mailbox=AsyncMock(),
        llm=llm,
        scan_result=None,
    )


@pytest.mark.asyncio
async def test_explicit_correction_reaches_next_session_prompt(tmp_path):
    manager = MemoryManager(tmp_path / "MEMORY.md")
    manager.store.save([_old_assignment()])
    (tmp_path / "USER.md").write_text(
        "# 用户画像\n\n- 项目: 项目 A 核心研发\n",
        encoding="utf-8",
    )
    session_id = "web_session_demo_20260802"
    _write_session(
        tmp_path / "sessions" / f"{session_id}.json",
        content="我现在不负责项目 A 啦，请不要再把我当作该项目研发。",
    )
    llm = AsyncMock()
    llm.complete.side_effect = [
        json.dumps({
            "corrections": [{
                "content": "用户现在不负责项目 A",
                "category": "fact",
                "supersedes_ids": ["old001"],
                "source_session": session_id,
            }],
            "split_entries": [],
            "merge_pairs": [],
            "deprecate_ids": [],
            "reinforce_ids": [],
            "refined_entries": [],
        }, ensure_ascii=False),
        "- 项目状态: 不再负责项目 A",
    ]

    result = await run(_context(tmp_path, manager, llm))

    assert result == "profile_rebuilt:1; corrected=1; split=0"
    entries = {entry.id: entry for entry in manager.load_entries()}
    assert entries["old001"].status == "superseded"
    replacement = next(entry for entry in entries.values() if entry.status == "active")
    assert replacement.content == "用户现在不负责项目 A"
    assert replacement.supersedes == ["old001"]
    assert replacement.source_session == session_id

    user_profile = (tmp_path / "USER.md").read_text(encoding="utf-8")
    assert "不再负责项目 A" in user_profile
    assert "核心研发" not in user_profile

    # The next session sees only the derived USER projection; superseded raw
    # MEMORY content is retained for traceability but is not injected.
    next_session_prompt = WorkspaceLoader(tmp_path).build_system_prompt()
    assert "不再负责项目 A" in next_session_prompt
    assert "核心研发者并负责该项目" not in next_session_prompt

    # A deterministic next-session answer check: the injected facts cannot
    # support an affirmative answer to “do I still own project A?”.
    answer = "不再负责" if "不再负责项目 A" in next_session_prompt else "仍负责"
    assert answer == "不再负责"

    # Re-running with the same already-watermarked session is a no-op.
    assert await run(_context(tmp_path, manager, AsyncMock())) is None
    assert len(manager.load_entries()) == 2


@pytest.mark.asyncio
async def test_no_active_memory_clears_stale_profile_and_prompt(tmp_path):
    manager = MemoryManager(tmp_path / "MEMORY.md")
    stale = _old_assignment()
    stale.score = 0.22
    manager.store.save([stale])
    (tmp_path / "USER.md").write_text(
        "# 用户画像\n\n- 项目: 项目 A 核心研发\n",
        encoding="utf-8",
    )

    result = await run(_context(tmp_path, manager, AsyncMock()))

    assert result == "profile_cleared"
    assert "（暂无活跃画像）" in (tmp_path / "USER.md").read_text(encoding="utf-8")
    prompt = WorkspaceLoader(tmp_path).build_system_prompt()
    assert "项目 A 核心研发" not in prompt
    assert "用户是项目 A" not in prompt


class _E2EUserData:
    def __init__(self, root: Path):
        self.sessions_dir = root / "sessions"
        self.heartbeat_events_file = root / "heartbeat-events.jsonl"

    def get_agent_skill_logs_dir(self, _agent_name: str) -> Path:
        return self.heartbeat_events_file.parent / "skill-logs"

    def get_agent_skill_eval_dir(self, _agent_name: str) -> Path:
        return self.heartbeat_events_file.parent / "skill-eval"

    def get_skill_log_recorder(self, **_kwargs):
        return None


@pytest.mark.asyncio
async def test_cron_gate_runs_correction_to_next_session_prompt(tmp_path):
    """Production cron/gate/job wiring, with only the LLM boundary stubbed."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    user_data = _E2EUserData(tmp_path)
    manager = MemoryManager(workspace / "MEMORY.md")
    manager.store.save([_old_assignment()])
    (workspace / "USER.md").write_text(
        "# 用户画像\n\n- 项目: 项目 A 核心研发\n", encoding="utf-8",
    )
    session_id = "web_session_demo_cron_e2e"
    _write_session(
        user_data.sessions_dir / f"{session_id}.json",
        content="我现在不负责项目 A 啦，请更新记忆。",
    )

    routine_manager = RoutineManager(workspace)
    routine_manager.add_routine(
        title="Memory Review",
        schedule="2h",
        job="memory-review",
        scanner="session",
        execution_mode="inline",
        next_run_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
    )
    tasks = routine_manager.load_task_list()
    session_manager = MagicMock()
    session_manager.get_primary_session_id.return_value = "web_session_demo_primary"
    session_manager.get_heartbeat_session_id.return_value = "heartbeat_session_demo"
    delivery = CronDelivery(
        session_manager=session_manager,
        primary_session_id="web_session_demo_primary",
        heartbeat_session_id="heartbeat_session_demo",
        agent_name="demo",
        realtime_push=False,
    )

    responses = [
        json.dumps({
            "corrections": [{
                "content": "用户现在不负责项目 A",
                "category": "fact",
                "supersedes_ids": ["old001"],
                "source_session": session_id,
            }],
        }, ensure_ascii=False),
        "- 项目状态: 不再负责项目 A",
    ]

    async def complete(_self, _prompt, **_kwargs):
        return responses.pop(0)

    with patch(
        "src.everbot.infra.user_data.get_user_data_manager",
        return_value=user_data,
    ), patch(
        "src.everbot.core.runtime.cron._SkillLLMClient.complete",
        new=complete,
    ):
        executor = CronExecutor(
            agent_name="demo",
            workspace_path=workspace,
            session_manager=session_manager,
            agent_factory=AsyncMock(),
            routine_manager=routine_manager,
            delivery=delivery,
        )
        executor._resolve_skill_model = lambda: "test-model"
        result = await executor.tick(
            tasks,
            run_agent=AsyncMock(),
            inject_context=AsyncMock(),
            run_id="memory-review-e2e",
        )

    assert result.executed == 1
    assert result.failed == 0
    assert result.results[0].status == "done"
    assert ReflectionState.load(workspace).get_watermark("memory-review")
    prompt = WorkspaceLoader(workspace).build_system_prompt()
    assert "不再负责项目 A" in prompt
    assert "核心研发者并负责该项目" not in prompt
    events = user_data.heartbeat_events_file.read_text(encoding="utf-8")
    assert '"event": "job_completed"' in events
