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


class MailboxAdapter:
    """Adapts HeartbeatSessionPort.deposit_mailbox_event() to MailboxPort."""

    def __init__(self, session_manager: Any, primary_session_id: str, agent_name: str = ""):
        self._session_manager = session_manager
        self._primary_session_id = primary_session_id
        self._agent_name = agent_name

    async def deposit(self, summary: str, detail: str) -> bool:
        """Deposit a skill notification to user's primary session mailbox."""
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
            return ok
        except Exception as e:
            logger.warning("Mailbox deposit failed: %s", e)
            return False
