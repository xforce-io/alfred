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
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Dict, Optional, Union

from dolphin.core.agent.agent_state import AgentState
from ...infra.dolphin_compat import KEY_HISTORY

from .models import OutboundMessage
from .session_resolver import ChannelSessionResolver
from . import skill_change_detector as _skill_detect
from ...core.runtime.context_strategy import PrimaryContextStrategy, RuntimeDeps
from ...core.runtime.mailbox import compose_message_with_mailbox_updates
from ...core.runtime.turn_orchestrator import (
    CHAT_POLICY,
    TurnEventType,
    TurnOrchestrator,
    build_chat_policy,
)
from ...core.runtime import events
from ...core.session.session import SessionManager
from ...infra.user_data import UserDataManager
from ...infra.workspace import WorkspaceLoader
from ...infra.dolphin_compat import ensure_continue_chat_compatibility

# SLM: imported at module level to avoid per-event attribute lookup overhead.
# handle_skill_event is called in the SKILL-completed hot path.
try:
    from ...core.slm.skill_log_recorder import handle_skill_event as _slm_handle_skill_event
except Exception:  # pragma: no cover
    _slm_handle_skill_event = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def _build_circuit_break_summary(exc: Exception, kind: str) -> str:
    """Build a user-friendly summary for circuit-breaker stops.

    Extracts structured stats attached by the TURN_ERROR handler and
    produces a concise Chinese message describing what happened.
    """
    n_calls = getattr(exc, "turn_tool_call_count", 0)
    n_failed = getattr(exc, "turn_failed_count", 0)
    tool_names: list = getattr(exc, "turn_tool_names", [])

    # Deduplicate tool names while preserving order
    seen: set = set()
    unique_tools: list = []
    for t in tool_names:
        if t not in seen:
            seen.add(t)
            unique_tools.append(t)

    # Build stats line
    parts: list = []
    if n_calls:
        parts.append(f"执行了 {n_calls} 次工具调用")
    if n_failed:
        parts.append(f"其中 {n_failed} 次失败")
    stats = "，".join(parts) if parts else "多次尝试后"

    tools_desc = ""
    if unique_tools:
        tools_desc = f"（使用了 {', '.join(unique_tools[:5])}{'等' if len(unique_tools) > 5 else ''}）"

    if kind == "budget":
        reason = "工具调用次数已达上限，继续重试很可能是无效循环"
    elif kind == "loop":
        reason = "检测到重复操作模式，已自动停止"
    else:
        reason = "同类错误连续出现，当前策略未能取得进展"

    if kind == "loop":
        return (
            f"已停止：{stats}{tools_desc}，{reason}。"
            "你可以给我一个更具体的指令，或换一种做法。"
        )

    return (
        f"已停止本轮自动重试：{stats}{tools_desc}，{reason}。"
        "请换一种做法，或告诉我下一步方向。"
    )


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
        *,
        skill_log_recorder: Optional[Any] = None,
        skill_log_recorder_factory: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self.session_manager = session_manager
        self.agent_service = agent_service
        self.user_data = user_data

        self._primary_context_strategy = PrimaryContextStrategy()
        self._runtime_deps = RuntimeDeps(
            load_workspace_instructions=self._runtime_load_workspace_instructions,
        )
        self._runtime_workspace_instructions_by_agent: Dict[str, str] = {}
        self._default_orchestrator = TurnOrchestrator(CHAT_POLICY)
        # Cross-turn failure memory: maps session_id → {failure_sig: count}.
        # Allows circuit breakers to fire earlier when the same tool keeps
        # failing across consecutive user messages.
        self._session_failure_memory: Dict[str, Dict[str, int]] = {}
        # SLM skill log recorder — per-agent, lazily created via factory.
        # Legacy: if skill_log_recorder is provided directly, use it as fallback.
        self._skill_log_recorder_factory = skill_log_recorder_factory
        self._skill_log_recorders: Dict[str, Any] = {}
        self._skill_log_recorder_fallback = skill_log_recorder

    def _get_recorder(self, agent_name: str) -> Optional[Any]:
        """Get per-agent SkillLogRecorder, lazily created."""
        recorders = getattr(self, "_skill_log_recorders", None)
        if recorders is None:
            return getattr(self, "_skill_log_recorder_fallback", None)
        if not agent_name:
            return getattr(self, "_skill_log_recorder_fallback", None)
        if agent_name not in self._skill_log_recorders:
            if self._skill_log_recorder_factory:
                try:
                    self._skill_log_recorders[agent_name] = self._skill_log_recorder_factory(agent_name)
                except Exception:
                    self._skill_log_recorders[agent_name] = None
            else:
                self._skill_log_recorders[agent_name] = self._skill_log_recorder_fallback
        return self._skill_log_recorders[agent_name]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_text_from_message(message: Union[str, list]) -> str:
        """Extract the text portion from a message (str or multimodal list)."""
        if isinstance(message, str):
            return message
        if isinstance(message, list):
            parts = []
            for item in message:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(item.get("text", ""))
            return " ".join(parts)
        return str(message)

    async def process_message(
        self,
        agent: Any,
        agent_name: str,
        session_id: str,
        message: Union[str, list],
        on_event: Callable[[OutboundMessage], Awaitable[None]],
    ) -> None:
        """处理一条用户消息（加锁 → 加载 → compose → run_turn → 持久化 → 释放锁）。

        Turn 执行过程中产生的事件通过 *on_event* 回调投递。
        """
        message_text = self._extract_text_from_message(message)
        logger.debug("Processing message for agent=%s, message='%s'", agent_name, message_text[:80])

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
                # Restore cross-turn failure memory from persisted session.
                _persisted_fm = getattr(session_data, "failure_memory", None)
                if _persisted_fm:
                    self._session_failure_memory[session_id] = dict(_persisted_fm)
            _restore_ok = True
            self._inject_skill_updates_if_needed(agent, session_id, session_data)
            if isinstance(message, list):
                # Multimodal message: skip mailbox composition, pass as-is.
                # Still drain mailbox so events don't accumulate across turns
                # and leak into the next text message (intent-hijack bug).
                effective_message = message
                _, mailbox_ack_ids = self._compose_turn_message(
                    "", session_data, agent_name,
                )
            else:
                effective_message, mailbox_ack_ids = self._compose_turn_message(
                    message,
                    session_data,
                    agent_name,
                )


            logger.debug("Agent=%s, Message=%s", agent_name, message_text[:50])

            # SLM: backfill the previous turn's context_after with this user message.
            # Must run before new skills execute in this turn.
            _recorder = self._get_recorder(agent_name)
            if _recorder is not None and message_text and hasattr(_recorder, "backfill_context_after"):
                _recorder.backfill_context_after(session_id, message_text)

            message_preview, _, _ = self._truncate_preview(message_text, 200)
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
            self._reload_workspace_instructions_if_missing(agent, agent_name)
            self._cache_runtime_workspace_instructions(agent_name, ctx)
            # Refresh current_time so the LLM always knows the actual time
            ctx.set_variable("current_time", datetime.now().strftime("%Y-%m-%d %H:%M"))
            system_prompt_override = self._build_turn_system_prompt(session_data, agent_name)
            ensure_continue_chat_compatibility()

            history_messages = ctx.get_var_value(KEY_HISTORY)
            is_first_turn = (
                (not isinstance(history_messages, list) or len(history_messages) == 0)
                and agent.state != AgentState.PAUSED
            )
            response = ""
            llm_started = False

            async def _on_before_retry(attempt: int, exc: Exception):
                if agent.state == AgentState.ERROR:
                    try:
                        await agent.initialize()
                    except Exception as e:
                        raise exc from e
                msg = f"检测到网络异常 ({str(exc)[:50]}...)，正在重试 ({attempt + 1}/{CHAT_POLICY.max_attempts})..."
                await on_event(OutboundMessage(session_id, msg, msg_type="status"))
                await on_event(OutboundMessage(session_id, "", msg_type="end"))

            async def _deliver_deferred_result(result_text: str) -> None:
                """Deliver deferred result from a timed-out turn via history + events.

                History injection persists the result for LLM context;
                events.emit pushes it to connected channels in real-time.
                Mailbox deposit is intentionally omitted — the result is
                already in history_messages so prepending it again as a
                "Background Updates" prefix would be redundant.
                """
                try:
                    msg = {
                        "role": "assistant",
                        "content": (
                            "[此消息由超时后台任务完成后自动生成]\n\n"
                            + result_text
                        ),
                        "metadata": {
                            "source": "deferred_result",
                            "run_id": run_id,
                            "injected_at": datetime.now(timezone.utc).isoformat(),
                        },
                    }
                    if hasattr(self.session_manager, "inject_history_message"):
                        await self.session_manager.inject_history_message(
                            session_id, msg, timeout=5.0, blocking=False,
                        )
                    await events.emit(
                        session_id,
                        {
                            "detail": result_text,
                            "source_type": "deferred_result",
                            "run_id": run_id,
                            "agent_name": agent_name,
                            "deliver": True,
                        },
                        agent_name=agent_name,
                        scope="session",
                        target_session_id=session_id,
                        target_channel=ChannelSessionResolver.extract_channel_type(session_id),
                        source_type="deferred_result",
                        run_id=run_id,
                    )
                except Exception as exc:
                    logger.warning("Failed to deliver deferred result: %s", exc)

            # Build per-turn policy with config overrides (agent > global > default)
            from ...infra.config import get_config
            _turn_policy = build_chat_policy(get_config(), agent_name=agent_name)
            _prior_failures = self._session_failure_memory.get(session_id, {})
            _turn_orchestrator = TurnOrchestrator(_turn_policy, prior_failures=_prior_failures)

            async for te in _turn_orchestrator.run_turn(
                agent,
                effective_message,
                system_prompt=system_prompt_override,
                is_first_turn=is_first_turn,
                on_before_retry=_on_before_retry,
                on_deferred_result=_deliver_deferred_result,
            ):
                if te.type == TurnEventType.LLM_DELTA:
                    if not llm_started:
                        llm_started = True
                        self._record_timeline_event(session_id, "llm_start", **event_meta)
                    await on_event(OutboundMessage(session_id, te.content, msg_type="delta"))
                    response += te.content

                elif te.type == TurnEventType.LLM_ROUND_RESET:
                    # New agentic round starting — discard intermediate text
                    await on_event(OutboundMessage(session_id, "", msg_type="round_reset"))
                    response = ""

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
                        # SLM: record successful skill invocations for evaluation
                        _recorder = self._get_recorder(agent_name)
                        if norm_status == "completed" and _recorder is not None and _slm_handle_skill_event is not None:
                            _slm_handle_skill_event(
                                te, _recorder,
                                session_id=session_id,
                                context_before=message_text or "",
                            )
                    await on_event(OutboundMessage(session_id, "", msg_type="skill", metadata={
                        "id": te.pid or "noid-skill",
                        "status": te.status, "skill_name": te.skill_name,
                        "skill_args": te.skill_args, "skill_output": te.skill_output,
                    }))

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

                elif te.type == TurnEventType.STATUS:
                    await on_event(OutboundMessage(session_id, te.content, msg_type="status"))

                elif te.type == TurnEventType.TURN_ERROR:
                    # Preserve structured stats from the orchestrator
                    err = RuntimeError(te.error)
                    err.turn_tool_call_count = te.tool_call_count or tool_call_count
                    err.turn_tool_names = list(te.tool_names_executed or tool_names_executed)
                    err.turn_failed_count = te.failed_tool_outputs or failed_tool_outputs
                    raise err

                elif te.type == TurnEventType.TURN_COMPLETE:
                    response = te.answer or response
                    tool_call_count = te.tool_call_count
                    tool_execution_count = te.tool_execution_count
                    tool_names_executed = list(te.tool_names_executed)
                    failed_tool_outputs = te.failed_tool_outputs

            # Persist cross-turn failure memory.  If this turn had no
            # failures, clear the memory so successful turns reset the
            # circuit breaker (avoids penalising a turn long after the
            # network recovered).
            if _turn_orchestrator.accumulated_failures:
                if failed_tool_outputs > 0:
                    self._session_failure_memory[session_id] = _turn_orchestrator.accumulated_failures
                else:
                    self._session_failure_memory.pop(session_id, None)
            else:
                self._session_failure_memory.pop(session_id, None)

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

            # Send final response.
            # When LLM streams deltas, the text is already sent via "delta" events
            # and `response` just accumulates a copy — no need to re-send.
            # But in tool-use mode, the final answer may arrive only in
            # TURN_COMPLETE.answer without any prior LLM_DELTA, so we must
            # check whether deltas were actually streamed (llm_started).
            if not llm_started and response:
                response = self._strip_heartbeat_token_for_chat(response)
                if response:
                    await on_event(OutboundMessage(session_id, response, msg_type="text"))
                else:
                    await on_event(OutboundMessage(session_id, "（无响应）", msg_type="text"))
            elif not response:
                await on_event(OutboundMessage(session_id, "（无响应）", msg_type="text"))

            await on_event(OutboundMessage(session_id, "", msg_type="end"))

            # Persist failure memory into the session so it survives restarts.
            _fm = self._session_failure_memory.get(session_id)
            await self.session_manager.save_session(
                session_id,
                agent,
                lock_already_held=True,
                failure_memory=_fm if _fm else None,
            )
            await self._ack_mailbox_events(session_id, mailbox_ack_ids, lock_already_held=True)
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
                if _restore_ok:
                    await self.session_manager.save_session(
                        session_id,
                        agent,
                        lock_already_held=True,
                    )
                    await self._ack_mailbox_events(session_id, mailbox_ack_ids, lock_already_held=True)
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
                if err_msg.startswith(("TOOL_CALL_BUDGET_EXCEEDED", "REPEATED_TOOL_INTENT", "THINK_ONLY_LOOP", "EMPTY_OUTPUT_LOOP")):
                    summary = _build_circuit_break_summary(e, "budget")
                    await on_event(OutboundMessage(session_id, summary, msg_type="text"))
                elif err_msg.startswith("REPEATED_TEXT_LOOP"):
                    # Graceful stop: send partial answer if available, then ask user to intervene
                    if response and response.strip():
                        response = self._strip_heartbeat_token_for_chat(response) or response
                        await on_event(OutboundMessage(session_id, response, msg_type="text"))
                    summary = _build_circuit_break_summary(e, "loop")
                    await on_event(OutboundMessage(session_id, summary, msg_type="text"))
                elif err_msg.startswith("REPEATED_TOOL_FAILURES"):
                    summary = _build_circuit_break_summary(e, "failure")
                    await on_event(OutboundMessage(session_id, summary, msg_type="text"))
                elif "timeout" in err_msg.lower():
                    await on_event(OutboundMessage(session_id, (
                        "本轮执行超时，但后台任务仍在继续。如果任务完成，我会自动推送结果给你。"
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
                            _candidates = {message_text, effective_message if isinstance(effective_message, str) else message_text}
                            for _m in reversed(_hist[-4:]):
                                if _m.get("role") == "user" and _m.get("content") in _candidates:
                                    _dolphin_has_msg = True
                                    break
                        except Exception:
                            pass

                        if not _dolphin_has_msg and message_text:
                            _trailing.append({"role": "user", "content": message_text})

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
                        await self._ack_mailbox_events(session_id, mailbox_ack_ids, lock_already_held=True)
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
        if not hasattr(self, "_default_orchestrator"):
            self._default_orchestrator = TurnOrchestrator(CHAT_POLICY)

    def _runtime_load_workspace_instructions(self, agent_name: str) -> str:
        """Load cached workspace instructions for runtime context strategy."""
        self._ensure_runtime_context_strategy()
        return str(self._runtime_workspace_instructions_by_agent.get(agent_name) or "")

    def _reload_workspace_instructions_if_missing(self, agent: Any, agent_name: str) -> None:
        """Reload workspace_instructions from disk when restore cleared it.

        After session restore, ``_NON_RESTORABLE_VARS`` intentionally strips
        ``workspace_instructions`` to avoid stale content.  This method detects
        the missing variable and rebuilds fresh instructions from the workspace
        files on disk (AGENTS.md, HEARTBEAT.md, MEMORY.md, etc.).
        """
        ctx = agent.executor.context
        get_var = getattr(ctx, "get_var_value", None)
        if not callable(get_var):
            return
        current = get_var("workspace_instructions")
        if current:
            return  # still present — nothing to do

        try:
            workspace_path = self.user_data.get_agent_dir(agent_name)
            fresh = WorkspaceLoader(workspace_path).build_system_prompt()
            if fresh:
                from ...core.agent.factory import AgentFactory
                fresh = AgentFactory._append_runtime_paths(
                    None,
                    workspace_instructions=fresh,
                    workspace_path=workspace_path,
                )
            if fresh:
                ctx.set_variable("workspace_instructions", fresh)
                logger.info(
                    "Reloaded workspace_instructions from disk for agent=%s (%d chars)",
                    agent_name,
                    len(fresh),
                )
        except Exception:
            logger.warning(
                "Failed to reload workspace_instructions for agent=%s",
                agent_name,
                exc_info=True,
            )

    def _cache_runtime_workspace_instructions(self, agent_name: str, context: Any) -> None:
        """Cache workspace instructions from agent context for strategy lookup."""
        self._ensure_runtime_context_strategy()
        get_var = getattr(context, "get_var_value", None)
        if not callable(get_var):
            return
        value = get_var("workspace_instructions")
        if isinstance(value, str):
            self._runtime_workspace_instructions_by_agent[agent_name] = value

    async def _load_heartbeat_context(
        self,
        session_data: Any,
        agent_name: str,
    ) -> Optional[list]:
        """Load recent heartbeat messages from primary session for channel sessions.

        Returns None for primary/heartbeat sessions (they don't need cross-session
        context). For channel sessions (e.g. Telegram), loads the primary session
        and extracts recent heartbeat results so the LLM can reference them.
        """
        from ..session.session_ids import infer_session_type, get_primary_session_id
        from ..session.history_utils import extract_recent_heartbeat

        sid = getattr(session_data, "session_id", None) or ""
        session_type = infer_session_type(sid) if sid else ""
        if session_type != "channel":
            return None
        primary_id = get_primary_session_id(agent_name)
        primary = await self.session_manager.load_session(primary_id)
        if not primary or not primary.history_messages:
            return None
        heartbeats = extract_recent_heartbeat(primary.history_messages)
        return heartbeats or None

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

    async def _ack_mailbox_events(self, session_id: str, event_ids: list[str], *, lock_already_held: bool = False) -> None:
        """Acknowledge consumed mailbox events after successful turn."""
        if not event_ids:
            return
        await self.session_manager.ack_mailbox_events(session_id, event_ids, lock_already_held=lock_already_held)

    def _record_timeline_event(self, session_id: str, event_type: str, **payload) -> None:
        """Record one timeline event with an ISO timestamp."""
        event = {"type": event_type, "timestamp": datetime.now().isoformat()}
        event.update(payload)
        self.session_manager.append_timeline_event(session_id, event)

    # ------------------------------------------------------------------
    # Skill update detection
    # ------------------------------------------------------------------

    _SESSION_VAR_KNOWN_SKILLS = _skill_detect.SESSION_VAR_KNOWN_SKILLS

    @staticmethod
    def _get_current_resource_skills(agent: Any) -> Dict[str, str]:
        return _skill_detect.get_current_resource_skills(agent)

    def _inject_skill_updates_if_needed(
        self, agent: Any, session_id: str, session_data: Any,
    ) -> None:
        _skill_detect.inject_skill_updates_if_needed(agent, session_id, session_data)

    def _mark_activity(self, session_id: str) -> None:
        """No-op in core service; transports override for idle-gate."""
        pass
