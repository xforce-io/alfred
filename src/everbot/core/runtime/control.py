"""
Local control helpers for EverBot (CLI/Web).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from ..agent.factory import get_agent_factory
from ...infra.config import get_config
from .heartbeat import HeartbeatRunner
from ...infra.process import is_pid_running, read_pid_file, remove_pid_file
from ..session.session import SessionManager
from ...infra.user_data import UserDataManager, get_user_data_manager


def get_local_status(user_data: Optional[UserDataManager] = None) -> Dict[str, Any]:
    """
    Get local daemon status from PID + snapshot file.

    This does not attempt network calls; it relies on local files written by the daemon.
    """
    user_data = user_data or get_user_data_manager()
    pid = read_pid_file(user_data.pid_file)

    running = False
    if pid is not None:
        running = is_pid_running(pid)
        if not running:
            remove_pid_file(user_data.pid_file)

    snapshot: Optional[Dict[str, Any]] = None
    try:
        if user_data.status_file.exists():
            snapshot = json.loads(user_data.status_file.read_text(encoding="utf-8"))
    except Exception:
        snapshot = None

    return {
        "running": running,
        "pid": pid if running else None,
        "snapshot": snapshot,
    }


async def run_heartbeat_once(
    agent_name: str,
    *,
    config_path: Optional[str] = None,
    dolphin_config_path: Optional[str] = None,
    model: Optional[str] = None,
    force: bool = False,
) -> str:
    """Run a single heartbeat for an agent."""
    user_data = get_user_data_manager()
    user_data.ensure_directories()

    config = get_config(config_path)
    agents_config = config.get("everbot", {}).get("agents", {})
    agent_config = agents_config.get(agent_name, {})
    heartbeat_config = agent_config.get("heartbeat", {}) or {}

    workspace_path = Path(
        agent_config.get("workspace", str(user_data.get_agent_dir(agent_name)))
    ).expanduser()
    if not workspace_path.exists():
        user_data.init_agent_workspace(agent_name)

    model_name = agent_config.get("model") or config.get("everbot", {}).get("default_model") or model

    agent_factory = get_agent_factory(
        global_config_path=dolphin_config_path,
        default_model=model_name,
    )

    session_manager = SessionManager(user_data.sessions_dir)

    async def on_result(name: str, result: str) -> None:
        log_file = user_data.heartbeat_log_file
        log_file.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().isoformat()
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] [{name}] {result[:200]}\n")

    active_hours: Tuple[int, int] = tuple(heartbeat_config.get("active_hours", [8, 22]))  # type: ignore[assignment]

    runner_kwargs = {
        "agent_name": agent_name,
        "workspace_path": workspace_path,
        "session_manager": session_manager,
        "agent_factory": agent_factory.create_agent,
        "interval_minutes": int(heartbeat_config.get("interval", 30)),
        "active_hours": active_hours,
        "max_retries": int(heartbeat_config.get("max_retries", 3)),
        "ack_max_chars": int(heartbeat_config.get("ack_max_chars", 300)),
        "realtime_status_hint": bool(heartbeat_config.get("realtime_status_hint", True)),
        "broadcast_scope": str(heartbeat_config.get("broadcast_scope", "agent")),
        "routine_reflection": bool(heartbeat_config.get("routine_reflection", True)),
        "on_result": on_result,
        "heartbeat_max_history": int(heartbeat_config.get("heartbeat_max_history", 10)),
        "reflect_force_interval_hours": int(heartbeat_config.get("reflect_force_interval_hours", 24)),
    }
    runner = HeartbeatRunner(**runner_kwargs)

    return await runner.run_once_with_options(force=force)
