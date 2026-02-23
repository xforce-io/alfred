"""
Chat Service

Handles WebSocket chat sessions and agent message processing.
"""

from __future__ import annotations

import asyncio
import logging
import time
import traceback
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)
from fastapi import WebSocket
from dolphin.core.agent.agent_state import AgentState, PauseType

from .agent_service import AgentService
from ...core.channel.core_service import ChannelCoreService
from ...core.channel.models import OutboundMessage
from ...core.runtime.mailbox import compose_message_with_mailbox_updates
from ...core.runtime.turn_orchestrator import CHAT_POLICY
from ...core.session.session import SessionManager
from ...infra.user_data import get_user_data_manager


class ChatService:
    """Service for handling WebSocket chat sessions."""
    MAX_TOOL_ARGS_PREVIEW_CHARS = CHAT_POLICY.max_tool_args_preview_chars
    MAX_TOOL_OUTPUT_PREVIEW_CHARS = CHAT_POLICY.max_tool_output_preview_chars

    # Active WebSocket connections: session_id -> WebSocket
    _active_connections: Dict[str, WebSocket] = {}
    # Agent-scoped connections: agent_name -> set of (session_id, WebSocket)
    _connections_by_agent: Dict[str, set] = {}
    # Last activity time: session_id -> timestamp (time.time())
    _last_activity: Dict[str, float] = {}
    # Per-agent last broadcast: agent_name -> timestamp (for agent-scope idle gate)
    _last_agent_broadcast: Dict[str, float] = {}

    def __init__(self):
        self.agent_service = AgentService()
        self.user_data = get_user_data_manager()
        self.session_manager = SessionManager(self.user_data.sessions_dir)
        self._core = ChannelCoreService(self.session_manager, self.agent_service, self.user_data)
        self._setup_event_listener()

    def _setup_event_listener(self):
        """Setup global event listener for background-to-ui broadcasting."""
        from ...core.runtime.events import subscribe
        subscribe(self._on_background_event)

    def _register_connection(self, session_id: str, agent_name: str, websocket: WebSocket):
        """Register a WebSocket in both session and agent indices."""
        self._active_connections[session_id] = websocket
        if agent_name not in self._connections_by_agent:
            self._connections_by_agent[agent_name] = set()
        self._connections_by_agent[agent_name].add((session_id, websocket))

    def _unregister_connection(self, session_id: str, agent_name: str):
        """Remove a WebSocket from both session and agent indices."""
        ws = self._active_connections.pop(session_id, None)
        if agent_name in self._connections_by_agent:
            self._connections_by_agent[agent_name].discard((session_id, ws))
            if not self._connections_by_agent[agent_name]:
                del self._connections_by_agent[agent_name]

    def _mark_activity(self, session_id: str, agent_name: Optional[str] = None):
        """Mark current time as the latest activity for a session."""
        now = time.time()
        self._last_activity[session_id] = now

    async def _on_background_event(self, session_id: str, data: Dict[str, Any]):
        """Handle events from background tasks (like heartbeats).

        Routing by scope:
        - scope=agent: broadcast to all connections for the same agent_name
        - scope=session (default): only to the matching session_id

        Heartbeat suppress/deliver:
        - Events with "deliver": false are suppressed (not pushed to WebSocket)
        - Events with "deliver": true or without "deliver" key are pushed normally
        """
        # Suppress heartbeat messages marked as non-deliverable
        if data.get("deliver") is False:
            return

        scope = data.get("scope", "session")
        agent_name = data.get("agent_name")
        source_type = data.get("source_type")
        if source_type == "heartbeat":
            event_type = str(data.get("type") or "")
            # Heartbeat body content must be visible only via primary-session mailbox drain.
            if event_type in {"message", "delta", "skill"} or "_progress" in data:
                return
        bypass_idle_gate = source_type in {"time_reminder", "heartbeat", "heartbeat_delivery"}

        if scope == "agent" and agent_name:
            # Agent-scope idle gate: throttle by last broadcast time (not user activity)
            now = time.time()
            last_t = self._last_agent_broadcast.get(agent_name, 0)
            if (not bypass_idle_gate) and (now - last_t < 20):
                return

            targets = list(self._connections_by_agent.get(agent_name, set()))
            for sid, ws in targets:
                try:
                    await ws.send_json(data)
                except Exception:
                    self._active_connections.pop(sid, None)
                    self._connections_by_agent.get(agent_name, set()).discard((sid, ws))
            self._last_agent_broadcast[agent_name] = now
        else:
            # Session-scope (default)
            websocket = self._active_connections.get(session_id)
            if not websocket:
                return
            last_t = self._last_activity.get(session_id, 0)
            if (not bypass_idle_gate) and (time.time() - last_t < 20):
                return
            try:
                await websocket.send_json(data)
            except Exception:
                self._active_connections.pop(session_id, None)

    def _resolve_session_id_for_agent(self, agent_name: str, requested_session_id: Optional[str]) -> str:
        """Resolve session id and enforce agent-scoped id prefix."""
        if requested_session_id and self.session_manager.is_valid_agent_session_id(agent_name, requested_session_id):
            return requested_session_id
        return self.session_manager.get_primary_session_id(agent_name)

    async def handle_chat_session(self, websocket: WebSocket, agent_name: str, requested_session_id: Optional[str] = None):
        """
        Handle a complete WebSocket chat session.

        Args:
            websocket: WebSocket connection
            agent_name: Name of the agent to chat with
        """
        logger.debug("handle_chat_session called for: %s", agent_name)
        await websocket.accept()
        session_id = self._resolve_session_id_for_agent(agent_name, requested_session_id)
        self._register_connection(session_id, agent_name, websocket)
        self._mark_activity(session_id, agent_name)
        logger.debug("WebSocket accepted for %s", session_id)

        try:
            try:
                session_id = self._resolve_session_id_for_agent(agent_name, requested_session_id)
                await self.session_manager.migrate_legacy_sessions_for_agent(agent_name)
                history_sent = False

                agent = self.session_manager.get_cached_agent(session_id)
                logger.debug("Cached agent for %s: %s", session_id, agent is not None)

                if not agent:
                    logger.debug("Creating new agent instance for session: %s", session_id)
                    agent = await self.agent_service.create_agent_instance(agent_name)
                    ChannelCoreService._bind_session_id_to_context(agent, session_id)
                    self._core._init_session_trajectory(agent, agent_name, session_id, overwrite=False)

                    session_data = await self.session_manager.load_session(session_id)
                    if session_data:
                        self.session_manager.restore_timeline(session_id, session_data.timeline or [])
                        await self.session_manager.restore_to_agent(agent, session_data)
                        display_messages = session_data.history_messages or []
                        logger.debug("History restored: %d messages", len(display_messages))
                        await websocket.send_json({
                            "type": "history",
                            "session_id": session_id,
                            "messages": display_messages,
                        })
                        history_sent = True

                    self.session_manager.cache_agent(session_id, agent, agent_name, "auto")
                else:
                    logger.debug("Reusing existing agent instance for session: %s", session_id)
                    ChannelCoreService._bind_session_id_to_context(agent, session_id)
                    self._core._init_session_trajectory(agent, agent_name, session_id, overwrite=False)
                    session_data = await self.session_manager.load_session(session_id)
                    if session_data:
                        self.session_manager.restore_timeline(session_id, session_data.timeline or [])
                    if session_data and session_data.history_messages:
                        await self.session_manager.restore_to_agent(agent, session_data)
                        display_messages = session_data.history_messages or []
                        logger.debug("Restored %d messages to agent context", len(display_messages))
                        await websocket.send_json({
                            "type": "history",
                            "session_id": session_id,
                            "messages": display_messages,
                        })
                        history_sent = True

                logger.debug("Agent ready: %s", agent.name)

                try:
                    context = agent.executor.context
                    ws_instr = context.get_var_value("workspace_instructions")
                    model_var = context.get_var_value("model_name")
                    logger.debug("workspace_instructions length: %d, model_name: %s",
                                 len(ws_instr) if ws_instr else 0, model_var)
                except Exception as e:
                    logger.debug("Failed to check context: %s", e)

            except ValueError as e:
                logger.warning("Agent ValueError for %s: %s", agent_name, e, exc_info=True)
                await websocket.send_json({
                    "type": "error",
                    "content": f"Agent {agent_name} 初始化失败: {e}"
                })
                await websocket.close()
                return
            except Exception as e:
                error_detail = traceback.format_exc()
                logger.error("Agent initialization error:\n%s", error_detail)
                await websocket.send_json({
                    "type": "error",
                    "content": f"Agent 初始化失败: {str(e)}"
                })
                await websocket.close()
                return

            if not history_sent:
                await websocket.send_json({
                    "type": "message",
                    "role": "assistant",
                    "session_id": session_id,
                    "content": f"已连接到 {agent_name}，开始对话吧！"
                })

            # Drain mailbox: show unread background events on reconnect
            try:
                if session_data:
                    mailbox_events = getattr(session_data, "mailbox", []) or []
                    if mailbox_events:
                        _, ack_ids = compose_message_with_mailbox_updates("", mailbox_events)
                        # Collect displayable events (with detail or summary)
                        drain_events = []
                        acked_ids = set()
                        for evt in mailbox_events:
                            if not isinstance(evt, dict):
                                continue
                            eid = str(evt.get("event_id") or "").strip()
                            if eid:
                                acked_ids.add(eid)
                            detail = str(evt.get("detail") or "").strip()
                            summary = str(evt.get("summary") or "").strip()
                            content = detail or summary
                            if content:
                                drain_events.append(content)
                        if drain_events:
                            await websocket.send_json({
                                "type": "mailbox_drain",
                                "events": drain_events,
                            })
                        if ack_ids:
                            await self._core._ack_mailbox_events(session_id, ack_ids)
                            logger.debug("Drained %d mailbox events on connect", len(drain_events))
            except Exception as e:
                logger.warning("Failed to drain mailbox on connect: %s", e)

            current_task: Optional[asyncio.Task] = None
            mailbox_poll_task: Optional[asyncio.Task] = None

            async def _poll_mailbox_loop():
                """Periodically check session mailbox and push new events to WebSocket."""
                while True:
                    await asyncio.sleep(5)
                    try:
                        fresh = await self.session_manager.load_session(session_id)
                        if not fresh:
                            continue
                        events = getattr(fresh, "mailbox", []) or []
                        if not events:
                            continue
                        drain_events = []
                        ack_ids = []
                        for evt in events:
                            if not isinstance(evt, dict):
                                continue
                            eid = str(evt.get("event_id") or "").strip()
                            if eid:
                                ack_ids.append(eid)
                            detail = str(evt.get("detail") or "").strip()
                            summary = str(evt.get("summary") or "").strip()
                            content = detail or summary
                            if content:
                                drain_events.append(content)
                        if drain_events:
                            await websocket.send_json({
                                "type": "mailbox_drain",
                                "events": drain_events,
                            })
                        if ack_ids:
                            await self._core._ack_mailbox_events(session_id, ack_ids)
                            logger.debug("Mailbox poll drained %d events", len(drain_events))
                    except Exception:
                        pass  # WebSocket closed or session read failed

            mailbox_poll_task = asyncio.create_task(_poll_mailbox_loop())

            while True:
                try:
                    data = await websocket.receive_json()
                    self._mark_activity(session_id, agent_name)
                except Exception as e:
                    self._unregister_connection(session_id, agent_name)
                    logger.debug("Connection closed or error: %s", e)
                    break

                # Handle actions
                action = data.get("action")
                if action == "stop":
                    if current_task and not current_task.done():
                        logger.debug("User interrupt requested for %s", agent_name)
                        try:
                            await agent.interrupt()
                            try:
                                await asyncio.wait_for(asyncio.shield(current_task), timeout=2.0)
                            except asyncio.TimeoutError:
                                logger.debug("Task did not finish within 2s, forcing cancellation")
                                current_task.cancel()
                                try:
                                    await current_task
                                except asyncio.CancelledError:
                                    pass
                        except Exception as e:
                            logger.debug("Agent.interrupt() failed: %s, falling back to task.cancel()", e)
                            current_task.cancel()

                        await websocket.send_json({"type": "status", "content": "已停止"})
                    else:
                        await websocket.send_json({"type": "status", "content": "已停止"})
                    continue

                message = data.get("message", "").strip()
                if not message:
                    continue

                # Clear stale status at the beginning of a new turn.
                await websocket.send_json({"type": "status", "content": ""})

                # === User Intervention Handling ===
                # Call agent.interrupt() → wait for task to finish → context preserved

                # Case 1: Agent is already PAUSED due to USER_INTERRUPT
                if agent.state == AgentState.PAUSED and agent._pause_type == PauseType.USER_INTERRUPT:
                    logger.debug("Agent is paused due to user interrupt, using resume_with_input()")
                    try:
                        await agent.resume_with_input(message)
                    except Exception as e:
                        logger.debug("resume_with_input failed: %s, will start fresh", e)

                # Case 2: There's an ongoing task (agent is RUNNING)
                elif current_task and not current_task.done():
                    logger.debug("User intervention while agent is RUNNING, triggering interrupt()")
                    try:
                        await agent.interrupt()
                    except Exception as e:
                        logger.debug("interrupt() failed: %s", e)

                    try:
                        await asyncio.wait_for(asyncio.shield(current_task), timeout=5.0)
                    except asyncio.TimeoutError:
                        logger.debug("Task didn't finish in 5s, cancelling forcefully")
                        current_task.cancel()
                        try:
                            await current_task
                        except asyncio.CancelledError:
                            pass
                    except asyncio.CancelledError:
                        pass
                    except Exception as e:
                        logger.debug("Task finished with exception: %s", e)

                    if agent.state == AgentState.PAUSED and agent._pause_type == PauseType.USER_INTERRUPT:
                        try:
                            await agent.resume_with_input(message)
                        except Exception as e:
                            logger.debug("resume_with_input failed: %s", e)

                current_task = asyncio.create_task(
                    self._process_message(websocket, agent, agent_name, session_id, message)
                )

                def task_done_callback(task):
                    try:
                        task.result()
                    except Exception as e:
                        logger.error("Message processing task failed: %s", e, exc_info=True)

                current_task.add_done_callback(task_done_callback)

        except Exception as e:
            logger.error("WebSocket error:\n%s", traceback.format_exc())
        finally:
            self._unregister_connection(session_id, agent_name)
            if 'mailbox_poll_task' in locals() and mailbox_poll_task and not mailbox_poll_task.done():
                mailbox_poll_task.cancel()
            if 'current_task' in locals() and current_task and not current_task.done():
                current_task.cancel()
            try:
                await websocket.close()
            except Exception:
                pass

    def _ensure_core(self) -> ChannelCoreService:
        """Lazily create ChannelCoreService for test paths using __new__."""
        if not hasattr(self, "_core"):
            self._core = ChannelCoreService(
                self.session_manager,
                getattr(self, "agent_service", None),
                self.user_data,
            )
        return self._core

    async def _process_message(
        self,
        websocket: WebSocket,
        agent,
        agent_name: str,
        session_id: str,
        message: str
    ):
        """Thin adapter: convert OutboundMessage to WebSocket JSON and delegate to core."""
        core = self._ensure_core()

        async def on_event(out: OutboundMessage):
            payload = self._outbound_to_ws_payload(out)
            await websocket.send_json(payload)

        await core.process_message(agent, agent_name, session_id, message, on_event)

    @staticmethod
    def _outbound_to_ws_payload(msg: OutboundMessage) -> dict:
        """OutboundMessage → existing WebSocket JSON format (frontend-compatible)."""
        if msg.msg_type == "delta":
            return {"type": "delta", "content": msg.content}
        elif msg.msg_type == "status":
            return {"type": "status", "content": msg.content}
        elif msg.msg_type == "error":
            return {"type": "error", "content": msg.content}
        elif msg.msg_type == "end":
            return {"type": "end", **msg.metadata}
        elif msg.msg_type == "skill":
            return {"type": "skill", **msg.metadata}
        elif msg.msg_type == "text":
            return {"type": "message", "role": "assistant", "content": msg.content, **msg.metadata}
        return {"type": "message", "role": "assistant", "content": msg.content}
