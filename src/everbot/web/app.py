"""
EverBot Web UI - Refactored

设计哲学：
- Chat 是主入口（默认页面）
- Status/Logs 是调试窗口（Tab 切换）
- 最终会集成到不同 channel
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, WebSocket, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .auth import verify_api_key, verify_ws_api_key
from .services import AgentService, ChatService
from ..core.session.session import SessionData
from ..infra.user_data import get_user_data_manager
from ..infra.config import get_config, load_config, save_config
# FastAPI app
app = FastAPI(title="EverBot")

# CORS middleware — permissive by default for local use; tighten via config if needed.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Directory configuration
BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Mount static files
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# Service instances
agent_service = AgentService()
chat_service = ChatService()
logger = logging.getLogger(__name__)

# Task tracking (for async heartbeat operations)
_tasks: Dict[str, str] = {}


def _has_trajectory_messages(payload: Dict[str, Any]) -> bool:
    """Return True if trajectory payload has at least one message."""
    messages = payload.get("trajectory")
    return isinstance(messages, list) and len(messages) > 0


# ============================================================================
# HTML Routes
# ============================================================================

@app.get("/")
async def index(request: Request):
    """Main interface: Chat + Tab switching"""
    return templates.TemplateResponse("index.html", {"request": request})


# ============================================================================
# API Routes
# ============================================================================

@app.get("/api/agents", dependencies=[Depends(verify_api_key)])
async def api_list_agents() -> list[str]:
    """List all agents"""
    return agent_service.list_agents()


@app.get("/api/status", dependencies=[Depends(verify_api_key)])
async def api_status() -> Dict[str, Any]:
    """Get daemon and agent status"""
    return agent_service.get_status()


@app.post("/api/agents/{agent_name}/heartbeat", dependencies=[Depends(verify_api_key)])
async def api_trigger_heartbeat(agent_name: str, force: bool = False) -> JSONResponse:
    """Trigger heartbeat for an agent"""
    task_id = f"{agent_name}:{asyncio.get_event_loop().time()}"
    _tasks[task_id] = "scheduled"

    async def _run() -> None:
        try:
            _tasks[task_id] = "running"
            await agent_service.trigger_heartbeat(agent_name, force=force)
            _tasks[task_id] = "done"
        except Exception as e:
            _tasks[task_id] = f"error: {e}"

    asyncio.create_task(_run())
    return JSONResponse({"scheduled": True, "task_id": task_id})


@app.post("/api/agents/{agent_name}/sessions/reset", dependencies=[Depends(verify_api_key)])
async def reset_agent_session(agent_name: str, request: Request):
    """Hard reset agent environment: clear all sessions, cache, and temp files."""
    forwarded_for = request.headers.get("x-forwarded-for", "").strip()
    real_ip = request.headers.get("x-real-ip", "").strip()
    client_ip = request.client.host if request.client else ""
    source_ip = forwarded_for or real_ip or client_ip or "unknown"
    referer = request.headers.get("referer", "")
    user_agent = request.headers.get("user-agent", "")
    logger.warning(
        "Environment reset requested: agent=%s source_ip=%s referer=%s user_agent=%s",
        agent_name,
        source_ip,
        referer,
        user_agent,
    )

    removed_sessions = await chat_service.session_manager.reset_agent_sessions(agent_name)

    # A-4: Clean up agent temp directory to prevent leftover artifacts
    # from contaminating the next conversation.
    user_data = get_user_data_manager()
    agent_tmp = user_data.alfred_home / "agents" / agent_name / "tmp"
    if agent_tmp.is_dir():
        import shutil
        shutil.rmtree(agent_tmp, ignore_errors=True)
        agent_tmp.mkdir(parents=True, exist_ok=True)

    logger.info("Environment reset completed: agent=%s removed_sessions=%s", agent_name, removed_sessions)
    return {"status": "ok", "removed_sessions": removed_sessions}


@app.post("/api/agents/{agent_name}/sessions/{session_id}/clear-history", dependencies=[Depends(verify_api_key)])
async def clear_session_history(agent_name: str, session_id: str):
    """Clear conversation history for a single session while preserving session metadata."""
    found = await chat_service.session_manager.clear_session_history(session_id)
    if not found:
        return JSONResponse({"status": "not_found", "session_id": session_id}, status_code=404)
    logger.info("Session history cleared: agent=%s session=%s", agent_name, session_id)
    return {"status": "ok", "session_id": session_id}


@app.get("/api/agents/{agent_name}/sessions", dependencies=[Depends(verify_api_key)])
async def list_agent_sessions(agent_name: str, limit: int = 20) -> Dict[str, Any]:
    """List persisted sessions for one agent."""
    sessions = await chat_service.session_manager.list_agent_sessions(agent_name, limit=limit)
    primary_id = chat_service.session_manager.get_primary_session_id(agent_name)
    if not any(item.get("session_id") == primary_id for item in sessions):
        primary_stub = {
            "session_id": primary_id,
            "agent_name": agent_name,
            "created_at": None,
            "updated_at": None,
            "message_count": 0,
            "timeline_count": 0,
        }
        sessions.append(primary_stub)
    sessions.sort(key=lambda x: str(x.get("updated_at") or x.get("created_at") or ""), reverse=True)
    if limit > 0:
        sessions = sessions[:limit]
    return {"agent_name": agent_name, "sessions": sessions}


@app.post("/api/agents/{agent_name}/sessions", dependencies=[Depends(verify_api_key)])
async def create_agent_session(agent_name: str, request: Request) -> Dict[str, Any]:
    """Create a new chat session id for one agent."""
    forwarded_for = request.headers.get("x-forwarded-for", "").strip()
    real_ip = request.headers.get("x-real-ip", "").strip()
    client_ip = request.client.host if request.client else ""
    source_ip = forwarded_for or real_ip or client_ip or "unknown"
    trigger = request.headers.get("x-everbot-trigger", "unknown")
    trusted = request.headers.get("x-everbot-trusted", "unknown")
    active_element = request.headers.get("x-everbot-active-element", "unknown")
    callsite = request.headers.get("x-everbot-callsite", "unknown")
    referer = request.headers.get("referer", "")
    user_agent = request.headers.get("user-agent", "")
    logger.warning(
        "Session creation requested: agent=%s source_ip=%s trigger=%s trusted=%s active_element=%s callsite=%s referer=%s user_agent=%s",
        agent_name,
        source_ip,
        trigger,
        trusted,
        active_element,
        callsite,
        referer,
        user_agent,
    )
    session_id = chat_service.session_manager.create_chat_session_id(agent_name)
    now = datetime.now().isoformat()
    session_data = SessionData(
        session_id=session_id,
        agent_name=agent_name,
        model_name="gpt-4",
        history_messages=[],
        variables={},
        created_at=now,
        updated_at=now,
        timeline=[],
        context_trace={},
    )
    await chat_service.session_manager.persistence.save_data(session_data)
    return {"agent_name": agent_name, "session_id": session_id}


@app.get("/api/agents/{agent_name}/session/trace", dependencies=[Depends(verify_api_key)])
async def get_agent_session_trace(agent_name: str, session_id: Optional[str] = None) -> Dict[str, Any]:
    """Get persisted trace data for one agent session."""
    await chat_service.session_manager.migrate_legacy_sessions_for_agent(agent_name)
    if session_id and not chat_service.session_manager.is_valid_agent_session_id(agent_name, session_id):
        raise HTTPException(status_code=400, detail="Invalid session_id for agent")
    resolved_session_id = session_id or chat_service.session_manager.get_primary_session_id(agent_name)
    session_data = await chat_service.session_manager.load_session(resolved_session_id)

    # Read session-scoped trajectory first, then fallback to legacy shared path.
    trajectory: Dict[str, Any] = {}
    user_data = get_user_data_manager()
    trajectory_file = user_data.get_session_trajectory_path(agent_name, resolved_session_id)
    legacy_trajectory_file = user_data.get_agent_tmp_dir(agent_name) / "trajectory.json"
    for candidate in (trajectory_file, legacy_trajectory_file):
        if not candidate.is_file():
            continue
        try:
            parsed = json.loads(candidate.read_text(encoding="utf-8"))
            if not isinstance(parsed, dict):
                continue
            trajectory = parsed
            # Session-scoped trajectory files may exist but still be empty.
            # In that case, continue and try legacy shared trajectory as fallback.
            if candidate == legacy_trajectory_file or _has_trajectory_messages(parsed):
                break
        except Exception:
            trajectory = {}

    if session_data is None:
        context_trace: Dict[str, Any] = {}
        timeline: list = []
        return {
            "session_id": resolved_session_id,
            "agent_name": agent_name,
            "created_at": None,
            "updated_at": None,
            "history_messages": [],
            "context_trace": context_trace,
            "timeline": timeline,
            "trajectory": trajectory,
            "trace_source": "persisted",
        }

    context_trace = session_data.context_trace or {}
    timeline = session_data.timeline or []
    return {
        "session_id": session_data.session_id,
        "agent_name": session_data.agent_name or agent_name,
        "created_at": session_data.created_at,
        "updated_at": session_data.updated_at,
        "history_messages": session_data.history_messages or [],
        "context_trace": context_trace,
        "timeline": timeline,
        "trajectory": trajectory,
        "trace_source": "persisted",
    }


@app.get("/api/settings/telegram", dependencies=[Depends(verify_api_key)])
async def api_get_telegram_settings() -> Dict[str, Any]:
    """Get current Telegram channel configuration."""
    config = get_config()
    telegram = (config.get("everbot", {}).get("channels", {}).get("telegram", {}))

    bot_token_raw = telegram.get("bot_token", "")
    if isinstance(bot_token_raw, str) and bot_token_raw.startswith("${") and bot_token_raw.endswith("}"):
        bot_token_display = bot_token_raw
    elif isinstance(bot_token_raw, str) and len(bot_token_raw) > 10:
        bot_token_display = bot_token_raw[:5] + "..." + bot_token_raw[-4:]
    elif bot_token_raw:
        bot_token_display = "***"
    else:
        bot_token_display = ""

    agents = agent_service.list_agents()

    return {
        "enabled": telegram.get("enabled", False),
        "bot_token_display": bot_token_display,
        "default_agent": telegram.get("default_agent", ""),
        "allowed_chat_ids": telegram.get("allowed_chat_ids", []),
        "agents": agents,
    }


@app.post("/api/settings/telegram", dependencies=[Depends(verify_api_key)])
async def api_save_telegram_settings(request: Request) -> Dict[str, Any]:
    """Save Telegram channel configuration (bot_token is read-only)."""
    body = await request.json()
    config = load_config()  # mutable copy for save_config

    everbot = config.setdefault("everbot", {})
    channels = everbot.setdefault("channels", {})
    telegram = channels.setdefault("telegram", {})

    if "enabled" in body:
        telegram["enabled"] = bool(body["enabled"])
    if "default_agent" in body:
        telegram["default_agent"] = body["default_agent"]
    if "allowed_chat_ids" in body:
        raw = body["allowed_chat_ids"]
        if isinstance(raw, str):
            telegram["allowed_chat_ids"] = [s.strip() for s in raw.split(",") if s.strip()]
        elif isinstance(raw, list):
            telegram["allowed_chat_ids"] = [str(x).strip() for x in raw if str(x).strip()]

    save_config(config)
    return {"status": "ok", "message": "Telegram 配置已保存，需要重启 daemon 才能生效。"}


@app.get("/api/settings/skills", dependencies=[Depends(verify_api_key)])
async def api_get_skills_settings() -> Dict[str, Any]:
    """Get all installed skills with their enabled/disabled state."""
    import re
    from pathlib import Path

    skills_state_file = Path.home() / ".alfred" / "skills-state.json"
    disabled: list[str] = []
    if skills_state_file.exists():
        try:
            disabled = json.loads(skills_state_file.read_text(encoding="utf-8")).get("disabled", [])
        except Exception:
            pass

    # Scan skills directories
    skills_dirs = [
        Path.home() / ".alfred" / "skills",
        Path(__file__).resolve().parents[3] / "skills",
    ]

    skills = []
    seen: set[str] = set()
    for skills_dir in skills_dirs:
        if not skills_dir.is_dir():
            continue
        for item in sorted(skills_dir.iterdir()):
            if not item.is_dir() or item.name.startswith("."):
                continue
            if item.name in seen:
                continue
            skill_md = item / "SKILL.md"
            if not skill_md.exists():
                continue
            seen.add(item.name)
            # Parse title and description from SKILL.md
            title = item.name
            description = ""
            try:
                content = skill_md.read_text(encoding="utf-8")
                title_match = re.search(r'^#\s+(.+)$', content, re.MULTILINE)
                if title_match:
                    title = title_match.group(1)
                desc_match = re.search(r'^#\s+.+$\n\n(.+?)(?:\n\n|\n#|$)', content, re.MULTILINE | re.DOTALL)
                if desc_match:
                    description = desc_match.group(1).strip()
                    if len(description) > 80:
                        description = description[:77] + "..."
            except Exception:
                pass
            skills.append({
                "name": item.name,
                "title": title,
                "description": description,
                "enabled": item.name not in disabled,
            })

    return {"skills": skills}


@app.post("/api/settings/skills/{skill_name}/toggle", dependencies=[Depends(verify_api_key)])
async def api_toggle_skill(skill_name: str, request: Request) -> Dict[str, Any]:
    """Enable or disable a skill."""
    body = await request.json()
    enabled = bool(body.get("enabled", True))

    skills_state_file = Path.home() / ".alfred" / "skills-state.json"
    state = {"version": "1.0", "disabled": []}
    if skills_state_file.exists():
        try:
            state = json.loads(skills_state_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    disabled = state.get("disabled", [])
    if enabled and skill_name in disabled:
        disabled.remove(skill_name)
    elif not enabled and skill_name not in disabled:
        disabled.append(skill_name)

    state["disabled"] = disabled
    skills_state_file.parent.mkdir(parents=True, exist_ok=True)
    skills_state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

    action = "enabled" if enabled else "disabled"
    logger.info("Skill %s %s via web UI", skill_name, action)
    return {"status": "ok", "skill_name": skill_name, "enabled": enabled}


@app.get("/api/logs/heartbeat/stream", dependencies=[Depends(verify_api_key)])
async def api_stream_heartbeat_log() -> StreamingResponse:
    """Stream heartbeat logs (SSE)"""
    user_data = get_user_data_manager()
    log_path = user_data.heartbeat_log_file

    async def _events():
        log_path.parent.mkdir(parents=True, exist_ok=True)
        f = open(log_path, "a+", encoding="utf-8")
        try:
            f.seek(0, 2)
            while True:
                line = f.readline()
                if not line:
                    await asyncio.sleep(0.5)
                    continue
                yield f"data: {line.rstrip()}\n\n"
        finally:
            try:
                f.close()
            except Exception:
                pass

    return StreamingResponse(_events(), media_type="text/event-stream")


@app.get("/api/logs/heartbeat/events/stream", dependencies=[Depends(verify_api_key)])
async def api_stream_heartbeat_events() -> StreamingResponse:
    """Stream structured heartbeat events (SSE, JSONL source)"""
    user_data = get_user_data_manager()
    events_path = user_data.heartbeat_events_file

    async def _events():
        events_path.parent.mkdir(parents=True, exist_ok=True)
        f = open(events_path, "a+", encoding="utf-8")
        try:
            f.seek(0, 2)
            while True:
                line = f.readline()
                if not line:
                    await asyncio.sleep(0.5)
                    continue
                yield f"data: {line.rstrip()}\n\n"
        finally:
            try:
                f.close()
            except Exception:
                pass

    return StreamingResponse(_events(), media_type="text/event-stream")


# ============================================================================
# WebSocket Routes
# ============================================================================

@app.websocket("/ws/chat/{agent_name}")
async def websocket_chat(websocket: WebSocket, agent_name: str, session_id: Optional[str] = None):
    """
    WebSocket Chat interface

    Real-time conversation with streaming output
    """
    import sys

    # Authenticate before accepting the WebSocket connection
    if not await verify_ws_api_key(websocket):
        await websocket.close(code=4001, reason="Unauthorized")
        return

    print(f"[WebSocket Endpoint] Received connection request for agent: {agent_name}", flush=True)
    sys.stdout.flush()
    try:
        await chat_service.handle_chat_session(websocket, agent_name, requested_session_id=session_id)
        print(f"[WebSocket Endpoint] Session completed for agent: {agent_name}", flush=True)
    except Exception as e:
        print(f"[WebSocket Endpoint] Error in session: {e}", flush=True)
        import traceback
        traceback.print_exc()
        sys.stdout.flush()
