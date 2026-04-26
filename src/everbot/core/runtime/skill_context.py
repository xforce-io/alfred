"""Skill runtime context for reflection skills."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

from ..memory.manager import MemoryManager
from ..scanners.base import ScanResult

logger = logging.getLogger(__name__)


@runtime_checkable
class LLMClient(Protocol):
    """Minimal LLM client interface for skill usage."""

    async def complete(self, prompt: str, system: str = "") -> str:
        """Single-turn LLM completion. Returns response text."""
        ...


@runtime_checkable
class MailboxPort(Protocol):
    """Minimal mailbox interface for skill usage."""

    async def deposit(self, summary: str, detail: str) -> bool:
        """Deposit a message to user's mailbox."""
        ...


@dataclass
class SkillContext:
    """Runtime context for reflection skills.

    Built by HeartbeatRunner._build_skill_context() and passed to skill run().
    """

    sessions_dir: Path  # UserDataManager.sessions_dir
    workspace_path: Path  # Agent workspace path (~/.alfred/agents/{agent_name}/)
    agent_name: str  # Current agent name
    memory_manager: MemoryManager  # Memory management (with store)
    mailbox: MailboxPort  # Message delivery to user
    llm: LLMClient  # LLM client (fast model)
    scan_result: Optional[ScanResult] = None  # Scanner gate pre-check result
    skill_logs_dir: Optional[Path] = None  # Per-agent skill usage logs
    skill_eval_dir: Optional[Path] = None  # Per-agent eval data (version pointers + reports)


class MailboxAdapter:
    """Adapts HeartbeatSessionPort.deposit_mailbox_event() to MailboxPort."""

    def __init__(self, session_manager: Any, primary_session_id: str, agent_name: str = ""):
        self._session_manager = session_manager
        self._primary_session_id = primary_session_id
        self._agent_name = agent_name

    async def deposit(self, summary: str, detail: str) -> bool:
        """Deposit a skill notification to user's primary session mailbox.

        Two-stage delivery, matching how Inspector emits push notifications:
        1. Persist to primary session mailbox (durable, survives restart;
           consumed via Background Updates on next primary chat turn).
        2. Emit a realtime push event so user-facing channels (Telegram,
           web SSE) deliver immediately AND mirror to their per-channel
           session mailbox so the user sees it on their next turn there.

        Without step 2, skill notifications would land in the web session
        mailbox but the user chatting via Telegram would never see them —
        the gap that hid SLM aborts for months.
        """
        from ..models.system_event import build_system_event

        event = build_system_event(
            event_type="skill_notification",
            source_session_id="",
            summary=summary[:500],
            detail=detail,
            artifacts=[],
            priority=0,
            suppress_if_stale=False,
            dedupe_key=f"skill_notification:{self._agent_name}:{summary[:50]}",
        )
        try:
            ok = await self._session_manager.deposit_mailbox_event(
                self._primary_session_id,
                event,
                timeout=5.0,
                blocking=True,
            )
            if not ok:
                logger.warning("Failed to deposit skill notification to mailbox")
        except Exception as e:
            logger.warning("Mailbox deposit failed: %s", e)
            ok = False

        # Realtime emit — best-effort, does not affect deposit success.
        try:
            from .events import emit
            await emit(
                self._primary_session_id,
                {
                    "type": "message",
                    "role": "assistant",
                    "content": summary,
                    "summary": summary[:300],
                    "detail": detail or summary,
                    "source_type": "skill_notification",
                    "deliver": True,
                },
                agent_name=self._agent_name,
                scope="agent",
                source_type="skill_notification",
            )
        except Exception as e:
            logger.warning("Skill notification realtime emit failed: %s", e)

        return ok
