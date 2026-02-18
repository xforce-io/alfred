"""Transport-agnostic message processing core.

Extracts the turn execution logic from ChatService so that any channel
(WebSocket, Telegram, Discord, …) can reuse the same lock → load →
compose → run_turn → persist pipeline.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import time
import traceback
import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from dolphin.core.agent.agent_state import AgentState
from dolphin.core.common.constants import KEY_HISTORY

from .models import OutboundMessage
from ...core.runtime.context_strategy import PrimaryContextStrategy, RuntimeDeps
from ...core.runtime.mailbox import compose_message_with_mailbox_updates
from ...core.runtime.turn_orchestrator import (
    CHAT_POLICY,
    TurnEventType,
    TurnOrchestrator,
)
from ...core.session.session import SessionManager
from ...infra.user_data import UserDataManager
from ...infra.dolphin_compat import ensure_continue_chat_compatibility

logger = logging.getLogger(__name__)


class ChannelCoreService:
    """Transport-agnostic 消息处理核心。

    Turn 执行过程中产生的事件通过 *on_event* 回调投递，
    transport 层负责将 :class:`OutboundMessage` 转为具体格式。
    """

    MAX_TOOL_ARGS_PREVIEW_CHARS = CHAT_POLICY.max_tool_args_preview_chars
    MAX_TOOL_OUTPUT_PREVIEW_CHARS = CHAT_POLICY.max_tool_output_preview_chars

    def __init__(
        self,
        session_manager: SessionManager,
        agent_service: Any,
        user_data: UserDataManager,
    ) -> None:
        self.session_manager = session_manager
        self.agent_service = agent_service
        self.user_data = user_data

        self._primary_context_strategy = PrimaryContextStrategy()
        self._runtime_deps = RuntimeDeps(
            load_workspace_instructions=self._runtime_load_workspace_instructions,
        )
        self._runtime_workspace_instructions_by_agent: Dict[str, str] = {}
        self._orchestrator = TurnOrchestrator(CHAT_POLICY)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def process_message(
        self,
        agent: Any,
        agent_name: str,
        session_id: str,
        message: str,
        on_event: Callable[[OutboundMessage], Awaitable[None]],
    ) -> None:
        """处理一条用户消息（加锁 → 加载 → compose → run_turn → 持久化 → 释放锁）。

        Turn 执行过程中产生的事件通过 *on_event* 回调投递。
        """
        logger.debug("Processing message for agent=%s, message='%s'", agent_name, message[:80])

        response = ""
        tool_call_count = 0
        failed_tool_outputs = 0
        tool_execution_count = 0
        tool_names_executed: list = []
        run_id = f"chat_{uuid.uuid4().hex[:12]}"
        event_meta = {"source_type": "chat_user", "run_id": run_id}
        _inproc_acquired = False
        _flock_fd = None
        _restore_ok = False
        mailbox_ack_ids: list[str] = []
        effective_message = message

        try:
            # Layer 1: in-process lock (same web process)
            _inproc_acquired = await self.session_manager.acquire_session(session_id, timeout=30.0)
            if not _inproc_acquired:
                await on_event(OutboundMessage(session_id, "当前会话繁忙，请稍后重试", msg_type="status"))
                await on_event(OutboundMessage(session_id, "", msg_type="end"))
                return

            # Layer 2: cross-process lock (web vs daemon). Use async polling to avoid blocking event loop.
            lock_path = self.session_manager.persistence._get_lock_path(session_id)
            _flock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
            deadline = time.monotonic() + 30.0
            flock_acquired = False
            while time.monotonic() < deadline:
                try:
                    fcntl.flock(_flock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    flock_acquired = True
                    break
                except (OSError, BlockingIOError):
                    await asyncio.sleep(0.05)

            if not flock_acquired:
                os.close(_flock_fd)
                _flock_fd = None
                await on_event(OutboundMessage(session_id, "当前会话繁忙，请稍后重试", msg_type="status"))
                await on_event(OutboundMessage(session_id, "", msg_type="end"))
                return

            # Reload latest disk session under lock before running this turn.
            session_data = await self.session_manager.load_session(session_id)
            if session_data:
                self.session_manager.restore_timeline(session_id, session_data.timeline or [])
                await self.session_manager.restore_to_agent(agent, session_data)
            _restore_ok = True
            self._inject_skill_updates_if_needed(agent, session_id, session_data)
            effective_message, mailbox_ack_ids = self._compose_turn_message(
                message,
                session_data,
                agent_name,
            )

            logger.debug("Agent=%s, Message=%s", agent_name, message[:50])

            message_preview, _, _ = self._truncate_preview(message, 200)
            turn_start_time = datetime.now()
            self._record_timeline_event(
                session_id,
                "turn_start",
                user_message_preview=message_preview,
                **event_meta,
            )

            # --- Turn execution via TurnOrchestrator ---
            ctx = agent.executor.context
            self._bind_session_id_to_context(agent, session_id)
            self._init_session_trajectory(agent, agent_name, session_id, overwrite=False)
            if agent.state != AgentState.PAUSED:
                ctx.set_variable("query", effective_message)
            self._cache_runtime_workspace_instructions(agent_name, ctx)
            system_prompt_override = self._build_turn_system_prompt(session_data, agent_name)
            ensure_continue_chat_compatibility()

            history_messages = ctx.get_var_value(KEY_HISTORY)
            is_first_turn = (
                (not isinstance(history_messages, list) or len(history_messages) == 0)
                and agent.state != AgentState.PAUSED
            )
            has_streamed = False
            response = ""
            llm_started = False

            async def _on_before_retry(attempt: int, exc: Exception):
                if agent.state == AgentState.ERROR:
                    try:
                        await agent.initialize()
                    except Exception:
                        raise exc
                msg = f"检测到网络异常 ({str(exc)[:50]}...)，正在重试 ({attempt + 1}/{self._orchestrator.policy.max_attempts})..."
                await on_event(OutboundMessage(session_id, msg, msg_type="status"))
                await on_event(OutboundMessage(session_id, "", msg_type="end"))

            async for te in self._orchestrator.run_turn(
                agent,
                effective_message,
                system_prompt=system_prompt_override,
                is_first_turn=is_first_turn,
                on_before_retry=_on_before_retry,
            ):
                if te.type == TurnEventType.LLM_DELTA:
                    if not llm_started:
                        llm_started = True
                        self._record_timeline_event(session_id, "llm_start", **event_meta)
                    await on_event(OutboundMessage(session_id, te.content, msg_type="delta"))
                    response += te.content
                    has_streamed = True

                elif te.type == TurnEventType.SKILL:
                    self._record_timeline_event(session_id, "skill", skill_name=te.skill_name, status=te.status, **event_meta)
                    norm_status = (te.status or "").lower()
                    if norm_status in {"processing", "running", "in_progress", "start", "started"}:
                        self._mark_activity(session_id)
                        self._record_timeline_event(
                            session_id, "tool_call", tool_name=te.skill_name,
                            args_preview=te.skill_args, source="skill_fallback", **event_meta,
                        )
                    elif norm_status in {"completed", "failed", "error"}:
                        self._record_timeline_event(
                            session_id, "tool_output", tool_name=te.skill_name,
                            output_preview=te.skill_output,
                            status="failed" if norm_status in {"failed", "error"} else "success",
                            source="skill_fallback", **event_meta,
                        )
                    await on_event(OutboundMessage(session_id, "", msg_type="skill", metadata={
                        "id": te.pid or "noid-skill",
                        "status": te.status, "skill_name": te.skill_name,
                        "skill_args": te.skill_args, "skill_output": te.skill_output,
                    }))
                    has_streamed = True

                elif te.type == TurnEventType.TOOL_CALL:
                    tool_call_count += 1
                    tool_execution_count += 1
                    tool_names_executed.append(te.tool_name)
                    self._record_timeline_event(
                        session_id, "tool_call", tool_name=te.tool_name,
                        args_preview=te.tool_args, args_truncated=te.args_truncated,
                        args_total_chars=te.args_total_chars, **event_meta,
                    )
                    await on_event(OutboundMessage(session_id, "", msg_type="skill", metadata={
                        "id": te.pid or "toolcall",
                        "status": "processing", "skill_name": te.tool_name,
                        "skill_args": te.tool_args,
                        "skill_args_truncated": te.args_truncated,
                        "skill_args_total_chars": te.args_total_chars,
                        "skill_output": None,
                    }))
                    has_streamed = True

                elif te.type == TurnEventType.TOOL_OUTPUT:
                    if te.status == "failed":
                        failed_tool_outputs += 1
                    self._record_timeline_event(
                        session_id, "tool_output", tool_name=te.tool_name,
                        output_preview=te.tool_output, status=te.status,
                        output_truncated=te.output_truncated,
                        output_total_chars=te.output_total_chars, **event_meta,
                    )
                    await on_event(OutboundMessage(session_id, "", msg_type="skill", metadata={
                        "id": te.pid or "toolout",
                        "status": "completed", "skill_name": te.tool_name,
                        "skill_args": "", "skill_output": te.tool_output,
                        "skill_output_truncated": te.output_truncated,
                        "skill_output_total_chars": te.output_total_chars,
                        "tool_reference_id": te.reference_id,
                    }))
                    has_streamed = True

                elif te.type == TurnEventType.STATUS:
                    await on_event(OutboundMessage(session_id, te.content, msg_type="status"))

                elif te.type == TurnEventType.TURN_ERROR:
                    raise RuntimeError(te.error)

                elif te.type == TurnEventType.TURN_COMPLETE:
                    response = te.answer or response
                    tool_call_count = te.tool_call_count
                    tool_execution_count = te.tool_execution_count
                    tool_names_executed = list(te.tool_names_executed)
                    failed_tool_outputs = te.failed_tool_outputs

            turn_end_time = datetime.now()
            total_duration_ms = int((turn_end_time - turn_start_time).total_seconds() * 1000)
            self._record_timeline_event(
                session_id, "turn_end",
                tool_call_count=tool_call_count,
                tool_execution_count=tool_execution_count,
                tool_names_executed=tool_names_executed,
                failed_tool_outputs=failed_tool_outputs,
                response_length=len(response or ""),
                status="completed", total_duration_ms=total_duration_ms,
                **event_meta,
            )

            # Send final response
            if not has_streamed and response:
                response = self._strip_heartbeat_token_for_chat(response)
                if response:
                    await on_event(OutboundMessage(session_id, response, msg_type="text"))
                else:
                    await on_event(OutboundMessage(session_id, "（无响应）", msg_type="text"))
            elif not response:
                await on_event(OutboundMessage(session_id, "（无响应）", msg_type="text"))

            await on_event(OutboundMessage(session_id, "", msg_type="end"))

            await self.session_manager.save_session(
                session_id,
                agent,
                lock_already_held=True,
            )
            await self._ack_mailbox_events(session_id, mailbox_ack_ids)
            logger.debug("Session persisted: %s", session_id)

        except asyncio.CancelledError:
            logger.debug("Task for %s was cancelled (likely due to user intervention)", agent_name)
            try:
                self._record_timeline_event(
                    session_id,
                    "turn_end",
                    tool_call_count=tool_call_count,
                    tool_execution_count=tool_execution_count,
                    tool_names_executed=tool_names_executed,
                    failed_tool_outputs=failed_tool_outputs,
                    response_length=len(response or ""),
                    status="cancelled",
                    source_type="chat_user",
                    run_id=run_id,
                )
                await self.session_manager.save_session(
                    session_id,
                    agent,
                    lock_already_held=True,
                )
                await on_event(OutboundMessage(session_id, "", msg_type="end", metadata={"status": "cancelled"}))
            except Exception as e:
                logger.warning("Failed to save session on cancellation: %s", e)
            raise
        except Exception as e:
            logger.error("Agent execution error:\n%s", traceback.format_exc())
            should_send_end = True
            try:
                self._record_timeline_event(
                    session_id,
                    "turn_end",
                    tool_call_count=tool_call_count,
                    tool_execution_count=tool_execution_count,
                    tool_names_executed=tool_names_executed,
                    failed_tool_outputs=failed_tool_outputs,
                    response_length=len(response or ""),
                    status="error",
                    error=str(e),
                    source_type="chat_user",
                    run_id=run_id,
                )
                err_msg = str(e)
                if err_msg.startswith("TOOL_CALL_BUDGET_EXCEEDED"):
                    await on_event(OutboundMessage(session_id, (
                        "我已停止本轮自动尝试：工具调用次数过多，继续重试很可能是无效循环。"
                        "建议你指定一个替代路径（例如换信息源、缩小目标或提供可用入口）。"
                    ), msg_type="text"))
                elif err_msg.startswith("REPEATED_TOOL_FAILURES"):
                    await on_event(OutboundMessage(session_id, (
                        "我已停止本轮自动重试：检测到重复失败（同类错误连续出现）。"
                        "请确认是否切换策略：1) 更换站点/接口 2) 你先人工完成验证后我继续。"
                    ), msg_type="text"))
                else:
                    await on_event(OutboundMessage(session_id, (
                        "本轮执行遇到错误，未能完成处理。"
                        "我已停止本轮并保留上下文，你可以直接重试或给我一个更具体的指令。"
                    ), msg_type="text"))
                    tb_text = traceback.format_exc()
                    await on_event(OutboundMessage(session_id, f"执行失败: {str(e)}\n\n```\n{tb_text[-1000:]}\n```", msg_type="error"))
            except Exception as send_error:
                should_send_end = False
                logger.warning("Failed to send error payload: %s", send_error)
            finally:
                if should_send_end:
                    try:
                        await on_event(OutboundMessage(session_id, "", msg_type="end"))
                    except Exception as end_error:
                        logger.warning("Failed to send end payload: %s", end_error)
                if _restore_ok:
                    try:
                        # Build failed turn context for session history
                        _trailing: list[dict] = []

                        # Check if Dolphin already committed the user message to
                        # history (e.g. via _update_history_and_cleanup in finally).
                        # Search the last few messages — Dolphin may have appended
                        # both user + assistant, so the user msg is not necessarily
                        # the very last entry.  Compare against both the raw
                        # ``message`` and ``effective_message`` (which may carry a
                        # mailbox prefix).
                        _dolphin_has_msg = False
                        try:
                            _exported = agent.snapshot.export_portable_session()
                            _hist = _exported.get("history_messages", [])
                            _candidates = {message, effective_message}
                            for _m in reversed(_hist[-4:]):
                                if _m.get("role") == "user" and _m.get("content") in _candidates:
                                    _dolphin_has_msg = True
                                    break
                        except Exception:
                            pass

                        if not _dolphin_has_msg and message:
                            _trailing.append({"role": "user", "content": message})

                        # Build failure summary as assistant message
                        _fail_parts = [f"（本轮执行遇到错误：{str(e)[:100]}）"]
                        _tb_text = traceback.format_exc()
                        if _tb_text and _tb_text.strip() != "NoneType: None":
                            _fail_parts.append(f"错误堆栈：\n{_tb_text[-500:]}")
                        if tool_names_executed:
                            _fail_parts.append(f"已调用工具：{', '.join(tool_names_executed)}")
                        if response:
                            _fail_parts.append(f"部分响应：{response[:300]}")
                        _trailing.append({
                            "role": "assistant",
                            "content": "\n".join(_fail_parts),
                        })

                        await self.session_manager.save_session(
                            session_id,
                            agent,
                            lock_already_held=True,
                            trailing_messages=_trailing,
                        )
                    except Exception as save_error:
                        logger.warning("Failed to persist session after error: %s", save_error)
                else:
                    logger.warning(
                        "Skipping session save after error: restore was not completed for %s",
                        session_id,
                    )
        finally:
            # Release cross-process file lock
            if _flock_fd is not None:
                try:
                    fcntl.flock(_flock_fd, fcntl.LOCK_UN)
                except Exception:
                    pass
                try:
                    os.close(_flock_fd)
                except Exception:
                    pass
                _flock_fd = None
            if _inproc_acquired:
                self.session_manager.release_session(session_id)

    async def load_history(self, session_id: str) -> list[dict]:
        """Load session history messages for the given session."""
        session_data = await self.session_manager.load_session(session_id)
        if session_data and session_data.history_messages:
            return session_data.history_messages
        return []

    async def drain_mailbox(self, session_id: str) -> tuple[list[str], list[str]]:
        """Drain mailbox events, returning (display_contents, ack_ids)."""
        session_data = await self.session_manager.load_session(session_id)
        if not session_data:
            return [], []
        mailbox_events = getattr(session_data, "mailbox", []) or []
        if not mailbox_events:
            return [], []

        _, ack_ids = compose_message_with_mailbox_updates("", mailbox_events)
        drain_events = []
        for evt in mailbox_events:
            if not isinstance(evt, dict):
                continue
            eid = str(evt.get("event_id") or "").strip()
            if eid and eid not in ack_ids:
                ack_ids.append(eid)
            detail = str(evt.get("detail") or "").strip()
            summary = str(evt.get("summary") or "").strip()
            content = detail or summary
            if content:
                drain_events.append(content)
        return drain_events, ack_ids

    # ------------------------------------------------------------------
    # Helpers (moved from ChatService)
    # ------------------------------------------------------------------

    def _init_session_trajectory(self, agent: Any, agent_name: str, session_id: str, overwrite: bool = False) -> None:
        """Initialize trajectory file isolated by session."""
        trajectory_path = self.user_data.get_session_trajectory_path(agent_name, session_id)
        trajectory_path.parent.mkdir(parents=True, exist_ok=True)
        agent.executor.context.init_trajectory(str(trajectory_path), overwrite=overwrite)

    @staticmethod
    def _bind_session_id_to_context(agent: Any, session_id: str) -> None:
        """Bind session_id into context variable and native context session when available."""
        context = agent.executor.context
        context.set_variable("session_id", session_id)
        if hasattr(context, "set_session_id"):
            context.set_session_id(session_id)

    def _truncate_preview(self, text: str, max_chars: int) -> tuple[str, bool, int]:
        """Build a bounded preview text to keep UI payload and history readable."""
        if text is None:
            return "", False, 0
        raw = str(text)
        total_chars = len(raw)
        if total_chars <= max_chars:
            return raw, False, total_chars

        if max_chars < 100:
            omitted = total_chars - max_chars
            preview = raw[:max_chars] + f"... [truncated {omitted} chars]"
            return preview, True, total_chars

        head_chars = int(max_chars * 0.6)
        tail_chars = max_chars - head_chars - 50
        omitted = total_chars - head_chars - tail_chars

        preview = (
            raw[:head_chars] +
            f"\n\n... [truncated {omitted} chars] ...\n\n" +
            raw[-tail_chars:]
        )
        return preview, True, total_chars

    @staticmethod
    def _strip_heartbeat_token_for_chat(text: str) -> str:
        """Strip stray HEARTBEAT_OK token at message edges for normal chat replies."""
        if not isinstance(text, str):
            return ""
        token = "HEARTBEAT_OK"
        cleaned = text.strip()
        changed = True
        while changed:
            changed = False
            if cleaned.startswith(token):
                cleaned = cleaned[len(token):].lstrip()
                changed = True
                continue
            if cleaned.endswith(token):
                cleaned = cleaned[:-len(token)].rstrip()
                changed = True
        return cleaned

    @staticmethod
    def _session_context_view(
        session_data: Any,
        agent_name: str,
    ) -> Any:
        """Create a lightweight session-like object for runtime strategy calls."""
        if session_data is not None:
            if not getattr(session_data, "agent_name", None):
                try:
                    setattr(session_data, "agent_name", agent_name)
                except Exception:
                    mailbox = getattr(session_data, "mailbox", []) or []
                    session_type = getattr(session_data, "session_type", "primary") or "primary"
                    return SimpleNamespace(
                        agent_name=agent_name,
                        mailbox=mailbox,
                        session_type=session_type,
                    )
            if not getattr(session_data, "session_type", None):
                try:
                    setattr(session_data, "session_type", "primary")
                except Exception:
                    pass
            return session_data
        return SimpleNamespace(agent_name=agent_name, mailbox=[], session_type="primary")

    def _ensure_runtime_context_strategy(self) -> None:
        """Lazily initialize runtime strategy deps for test paths using __new__."""
        if not hasattr(self, "_primary_context_strategy"):
            self._primary_context_strategy = PrimaryContextStrategy()
        if not hasattr(self, "_runtime_deps"):
            self._runtime_deps = RuntimeDeps(
                load_workspace_instructions=self._runtime_load_workspace_instructions,
            )
        if not hasattr(self, "_runtime_workspace_instructions_by_agent"):
            self._runtime_workspace_instructions_by_agent = {}
        if not hasattr(self, "_orchestrator"):
            self._orchestrator = TurnOrchestrator(CHAT_POLICY)

    def _runtime_load_workspace_instructions(self, agent_name: str) -> str:
        """Load cached workspace instructions for runtime context strategy."""
        self._ensure_runtime_context_strategy()
        return str(self._runtime_workspace_instructions_by_agent.get(agent_name) or "")

    def _cache_runtime_workspace_instructions(self, agent_name: str, context: Any) -> None:
        """Cache workspace instructions from agent context for strategy lookup."""
        self._ensure_runtime_context_strategy()
        get_var = getattr(context, "get_var_value", None)
        if not callable(get_var):
            return
        value = get_var("workspace_instructions")
        if isinstance(value, str):
            self._runtime_workspace_instructions_by_agent[agent_name] = value

    def _compose_turn_message(
        self,
        user_message: str,
        session_data: Any,
        agent_name: str,
    ) -> tuple[str, list[str]]:
        """Compose primary turn message via runtime context strategy."""
        self._ensure_runtime_context_strategy()
        session_view = self._session_context_view(session_data, agent_name)
        built = self._primary_context_strategy.build_message(
            session_view,
            user_message,
            self._runtime_deps,
        )
        return built.message, built.mailbox_ack_ids

    def _build_turn_system_prompt(
        self,
        session_data: Any,
        agent_name: str,
    ) -> str:
        """Build primary turn system prompt via runtime context strategy."""
        self._ensure_runtime_context_strategy()
        session_view = self._session_context_view(session_data, agent_name)
        return self._primary_context_strategy.build_system_prompt(session_view, self._runtime_deps)

    async def _ack_mailbox_events(self, session_id: str, event_ids: list[str]) -> None:
        """Acknowledge consumed mailbox events after successful turn."""
        if not event_ids:
            return
        if hasattr(self.session_manager, "ack_mailbox_events"):
            await self.session_manager.ack_mailbox_events(session_id, event_ids)
            return

        if hasattr(self.session_manager, "update_atomic"):
            ids = {str(eid).strip() for eid in event_ids if str(eid).strip()}
            if not ids:
                return

            def _mutator(session_data):
                mailbox = getattr(session_data, "mailbox", []) or []
                session_data.mailbox = [
                    e for e in mailbox
                    if not isinstance(e, dict) or str(e.get("event_id") or "").strip() not in ids
                ]

            await self.session_manager.update_atomic(session_id, _mutator, timeout=5.0, blocking=True)

    def _record_timeline_event(self, session_id: str, event_type: str, **payload) -> None:
        """Record one timeline event with an ISO timestamp."""
        event = {"type": event_type, "timestamp": datetime.now().isoformat()}
        event.update(payload)
        self.session_manager.append_timeline_event(session_id, event)

    # ------------------------------------------------------------------
    # Skill update detection
    # ------------------------------------------------------------------

    _SESSION_VAR_KNOWN_SKILLS = "_known_resource_skills"

    @staticmethod
    def _get_current_resource_skills(agent: Any) -> Dict[str, str]:
        """Extract current resource skill names and descriptions from the agent's ResourceSkillkit.

        Returns:
            Mapping of skill_name → description (may be empty if ResourceSkillkit not loaded).
        """
        global_skills = getattr(agent, "global_skills", None)
        if global_skills is None:
            return {}

        installed = getattr(global_skills, "installedSkillset", None)
        if installed is None:
            return {}

        rsk = None
        for skill in installed.getSkills():
            owner = getattr(skill, "owner_skillkit", None)
            if owner is not None and getattr(owner, "getName", lambda: "")() == "resource_skillkit":
                rsk = owner
                break

        if rsk is None:
            return {}

        result: Dict[str, str] = {}
        for name in rsk.get_available_skills():
            meta = rsk.get_skill_meta(name)
            desc = (getattr(meta, "description", "") or "") if meta else ""
            result[name] = desc
        return result

    def _inject_skill_updates_if_needed(
        self,
        agent: Any,
        session_id: str,
        session_data: Any,
    ) -> None:
        """Compare current resource skills with what the session last saw.

        If skills were added or removed, inject a system message into history
        so the LLM learns about the change without modifying the system prompt
        (preserving prefix cache).
        """
        current_skills = self._get_current_resource_skills(agent)
        if not current_skills:
            return

        current_names = set(current_skills.keys())

        # Read previously known skill set from session variables
        prev_names: Set[str] = set()
        has_prev = False
        if session_data and session_data.variables:
            stored = session_data.variables.get(self._SESSION_VAR_KNOWN_SKILLS)
            if isinstance(stored, list):
                prev_names = set(stored)
                has_prev = True

        if current_names == prev_names and has_prev:
            return

        # First turn (no previous record): just persist the baseline, no notification needed
        if not has_prev:
            ctx = agent.executor.context
            ctx.set_variable(self._SESSION_VAR_KNOWN_SKILLS, sorted(current_names))
            return

        added = sorted(current_names - prev_names)
        removed = sorted(prev_names - current_names)

        if not added and not removed:
            return

        # Build notification message
        parts: List[str] = ["[系统通知] 可用 Resource Skills 已更新。"]
        if added:
            lines = [f"  - **{n}**: {current_skills[n][:80]}" for n in added]
            parts.append("新增技能:\n" + "\n".join(lines))
        if removed:
            parts.append("已移除技能: " + ", ".join(f"`{n}`" for n in removed))
        parts.append(
            "当前完整列表: " + ", ".join(f"`{n}`" for n in sorted(current_names))
        )
        notification = "\n".join(parts)

        # Inject into agent history
        ctx = agent.executor.context
        history = ctx.get_var_value(KEY_HISTORY)
        if not isinstance(history, list):
            history = []
        history.append({"role": "user", "content": notification})
        ctx.set_variable(KEY_HISTORY, history)

        # Persist updated skill set for next comparison
        ctx.set_variable(self._SESSION_VAR_KNOWN_SKILLS, sorted(current_names))

        logger.info(
            "Skill update injected for session %s: +%s -%s",
            session_id,
            added or "none",
            removed or "none",
        )

    def _mark_activity(self, session_id: str) -> None:
        """No-op in core service; transports override for idle-gate."""
        pass
