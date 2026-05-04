"""Tests for memory observability CLI helpers."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from src.everbot.cli.memory_cli import collect_memory_stats, cmd_memory
from src.everbot.core.memory.event_store import EventStore
from src.everbot.core.memory.models import MemoryEntry
from src.everbot.core.memory.profile_store import ProfileStore
from src.everbot.infra.user_data import UserDataManager, reset_user_data_manager


def _entry(
    *,
    id: str,
    content: str,
    category: str,
    score: float,
    kind: str = "profile",
    event_at: str | None = None,
    due_at: str | None = None,
) -> MemoryEntry:
    return MemoryEntry(
        id=id,
        content=content,
        category=category,
        score=score,
        created_at="2026-05-01T10:00:00+00:00",
        last_activated="2026-05-01T10:00:00+00:00",
        activation_count=1,
        source_session="s1",
        kind=kind,
        event_at=event_at,
        due_at=due_at,
    )


def test_collect_memory_stats_summarizes_profile_and_events(tmp_path: Path):
    user_data = UserDataManager(alfred_home=tmp_path)
    workspace = user_data.get_agent_dir("demo")
    workspace.mkdir(parents=True)

    ProfileStore(workspace / "MEMORY.md").save(
        [
            _entry(id="p1", content="用户喜欢 Python", category="preference", score=0.9),
            _entry(id="p2", content="旧线索", category="fact", score=0.1),
        ],
        last_processed_count=12,
    )

    now = datetime(2026, 5, 10, tzinfo=timezone.utc)
    EventStore(workspace / "events").append(
        [
            _entry(
                id="e1",
                content="切到 deepseek-chat",
                category="decision",
                score=0.8,
                kind="event",
                event_at=(now - timedelta(days=2)).isoformat(),
            ),
            _entry(
                id="e2",
                content="周五交付 demo",
                category="todo",
                score=0.7,
                kind="event",
                event_at=(now - timedelta(days=20)).isoformat(),
                due_at=(now + timedelta(days=3)).isoformat(),
            ),
        ]
    )

    stats = collect_memory_stats("demo", user_data=user_data, now=now)

    assert stats["profile"]["total"] == 2
    assert stats["profile"]["active"] == 1
    assert stats["profile"]["archived"] == 1
    assert stats["profile"]["last_processed_count"] == 12
    assert stats["profile"]["categories"] == {"fact": 1, "preference": 1}
    assert stats["events"]["total"] == 2
    assert stats["events"]["pending_todo_count"] == 1
    assert stats["events"]["windows"]["last_7_days"] == 1
    assert stats["events"]["windows"]["last_30_days"] == 2
    assert stats["prompt"]["has_profile_block"] is True


def test_cmd_memory_stats_json_outputs_structured_payload(tmp_path: Path, capsys):
    reset_user_data_manager()
    user_data = UserDataManager(alfred_home=tmp_path)
    workspace = user_data.get_agent_dir("demo")
    workspace.mkdir(parents=True)
    ProfileStore(workspace / "MEMORY.md").save(
        [_entry(id="p1", content="用户喜欢 Python", category="preference", score=0.9)]
    )

    args = SimpleNamespace(
        memory_command="stats",
        agent="demo",
        alfred_home=str(tmp_path),
        json=True,
    )

    cmd_memory(args)
    out = capsys.readouterr().out
    payload = json.loads(out)

    assert payload["agent"] == "demo"
    assert payload["profile"]["total"] == 1
