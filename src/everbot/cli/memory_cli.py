"""CLI helpers for observing the memory system."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from ..core.memory.event_store import EventStore
from ..core.memory.manager import MemoryManager
from ..core.memory.models import MemoryEntry
from ..core.memory.profile_store import ProfileStore
from ..infra.user_data import UserDataManager, get_user_data_manager


def _parse_dt(raw: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-like timestamp into an aware datetime."""
    if not raw:
        return None
    try:
        value = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _file_info(path: Path) -> Dict[str, Any]:
    """Return basic file stats without failing on missing paths."""
    if not path.exists():
        return {"exists": False, "path": str(path), "size_bytes": 0, "modified_at": None}
    stat = path.stat()
    modified = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
    return {
        "exists": True,
        "path": str(path),
        "size_bytes": stat.st_size,
        "modified_at": modified,
    }


def _score_summary(entries: Iterable[MemoryEntry]) -> Dict[str, Any]:
    """Summarize entry scores."""
    scores = [entry.score for entry in entries]
    if not scores:
        return {"min": None, "avg": None, "max": None}
    return {
        "min": round(min(scores), 4),
        "avg": round(sum(scores) / len(scores), 4),
        "max": round(max(scores), 4),
    }


def _category_counts(entries: Iterable[MemoryEntry]) -> Dict[str, int]:
    """Return stable category counts."""
    counts = Counter(entry.category for entry in entries)
    return dict(sorted(counts.items()))


def _latest_dt(entries: Iterable[MemoryEntry], attr: str) -> Optional[str]:
    """Return the latest parseable datetime for one MemoryEntry attribute."""
    values = [_parse_dt(getattr(entry, attr, None)) for entry in entries]
    valid = [value for value in values if value is not None]
    if not valid:
        return None
    return max(valid).isoformat()


def _event_window_counts(entries: Iterable[MemoryEntry], now: datetime) -> Dict[str, int]:
    """Count events by event_at age windows."""
    buckets = {"last_7_days": 0, "last_30_days": 0, "last_90_days": 0}
    for entry in entries:
        event_at = _parse_dt(entry.event_at)
        if event_at is None:
            continue
        age_days = (now - event_at).total_seconds() / 86400.0
        if age_days <= 7:
            buckets["last_7_days"] += 1
        if age_days <= 30:
            buckets["last_30_days"] += 1
        if age_days <= 90:
            buckets["last_90_days"] += 1
    return buckets


def collect_memory_stats(
    agent_name: str,
    *,
    user_data: Optional[UserDataManager] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Collect read-only memory health stats for one agent."""
    if not agent_name:
        raise ValueError("agent name is required")

    user_data = user_data or get_user_data_manager()
    now = now or datetime.now(timezone.utc)
    workspace = user_data.get_agent_dir(agent_name)
    memory_path = workspace / "MEMORY.md"
    events_dir = workspace / "events"

    profile_store = ProfileStore(memory_path)
    profile_entries = profile_store.load()
    active_profile = [entry for entry in profile_entries if entry.score >= 0.2]
    archived_profile = [entry for entry in profile_entries if entry.score < 0.2]

    event_store = EventStore(events_dir)
    event_entries = event_store.load_all()
    event_months = event_store.list_months()
    invalid_event_at = [
        entry.id for entry in event_entries
        if _parse_dt(entry.event_at) is None
    ]
    pending_todos = [
        entry for entry in event_entries
        if entry.category == "todo"
        and (due_at := _parse_dt(entry.due_at)) is not None
        and due_at >= now
    ]

    prompt_text = MemoryManager(memory_path).get_prompt_memories()
    prompt_lines = [line for line in prompt_text.splitlines() if line.startswith("- [")]

    event_files = [
        _file_info(events_dir / f"{month}.md")
        for month in event_months
    ]

    return {
        "agent": agent_name,
        "workspace": str(workspace),
        "profile": {
            "file": _file_info(memory_path),
            "total": len(profile_entries),
            "active": len(active_profile),
            "archived": len(archived_profile),
            "last_processed_count": profile_store.last_processed_count,
            "categories": _category_counts(profile_entries),
            "scores": _score_summary(profile_entries),
            "latest_activated_at": _latest_dt(profile_entries, "last_activated"),
        },
        "events": {
            "dir": str(events_dir),
            "months": event_months,
            "files": event_files,
            "total": len(event_entries),
            "categories": _category_counts(event_entries),
            "scores": _score_summary(event_entries),
            "windows": _event_window_counts(event_entries, now),
            "latest_event_at": _latest_dt(event_entries, "event_at"),
            "invalid_event_at_count": len(invalid_event_at),
            "invalid_event_at_ids": invalid_event_at[:20],
            "pending_todo_count": len(pending_todos),
        },
        "prompt": {
            "chars": len(prompt_text),
            "entry_lines": len(prompt_lines),
            "has_profile_block": "# 历史记忆" in prompt_text,
            "has_event_block": "# 近期事件" in prompt_text,
        },
    }


def _print_text_stats(stats: Dict[str, Any]) -> None:
    """Print a compact human-readable memory report."""
    profile = stats["profile"]
    events = stats["events"]
    prompt = stats["prompt"]

    print(f"Agent: {stats['agent']}")
    print(f"Workspace: {stats['workspace']}")
    print("")
    print("Profile memory:")
    print(f"  file: {profile['file']['path']} ({'exists' if profile['file']['exists'] else 'missing'})")
    print(f"  entries: total={profile['total']}, active={profile['active']}, archived={profile['archived']}")
    print(f"  last_processed_count: {profile['last_processed_count']}")
    print(f"  categories: {profile['categories']}")
    print(f"  scores: {profile['scores']}")
    print(f"  latest_activated_at: {profile['latest_activated_at']}")
    print("")
    print("Event memory:")
    print(f"  dir: {events['dir']}")
    print(f"  months: {events['months']}")
    print(f"  entries: total={events['total']}, pending_todos={events['pending_todo_count']}")
    print(f"  windows: {events['windows']}")
    print(f"  categories: {events['categories']}")
    print(f"  scores: {events['scores']}")
    print(f"  latest_event_at: {events['latest_event_at']}")
    print(f"  invalid_event_at_count: {events['invalid_event_at_count']}")
    print("")
    print("Prompt injection estimate:")
    print(f"  chars: {prompt['chars']}")
    print(f"  entry_lines: {prompt['entry_lines']}")
    print(f"  blocks: profile={prompt['has_profile_block']}, event={prompt['has_event_block']}")


def cmd_memory(args: Any) -> None:
    """Handle the top-level memory command."""
    if args.memory_command == "stats":
        user_data = get_user_data_manager(
            Path(args.alfred_home).expanduser() if args.alfred_home else None
        )
        stats = collect_memory_stats(args.agent, user_data=user_data)
        if args.json:
            print(json.dumps(stats, ensure_ascii=False, indent=2))
        else:
            _print_text_stats(stats)
        return
    raise ValueError(f"unknown memory command: {args.memory_command}")


def register_memory_cli(subparsers: Any) -> None:
    """Register memory subcommands."""
    parser_memory = subparsers.add_parser("memory", help="查看 memory 系统状态")
    memory_subparsers = parser_memory.add_subparsers(dest="memory_command", required=True)

    parser_stats = memory_subparsers.add_parser("stats", help="统计指定 agent 的 memory 状态")
    parser_stats.add_argument("--agent", required=True, help="Agent 名称")
    parser_stats.add_argument(
        "--alfred-home",
        type=str,
        help="Alfred home 路径，默认使用 ~/.alfred 或 $ALFRED_HOME",
    )
    parser_stats.add_argument("--json", action="store_true", help="JSON 格式输出")
    parser_stats.set_defaults(func=cmd_memory)
