"""GrokCliProvider — host-spawned grok CLI behind AgentProvider.run_turn."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from ..base import SkillObservationBatch
from .....infra.dolphin_compat import KEY_HISTORY
from .invoke import DEFAULT_TIMEOUT_S, grok_json_to_progress, invoke_grok

_HISTORY_MAX_MESSAGES = 16
_HISTORY_MAX_CHARS = 800


def _format_history(messages: list) -> str:
    """Render recent chat turns for the grok prompt (bounded)."""
    if not messages:
        return ""
    recent = messages[-_HISTORY_MAX_MESSAGES:]
    lines = ["# Conversation so far"]
    for msg in recent:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user")
        content = msg.get("content") or ""
        if not isinstance(content, str):
            content = str(content)
        content = content.strip()
        if not content:
            continue
        if len(content) > _HISTORY_MAX_CHARS:
            content = content[:_HISTORY_MAX_CHARS] + "…"
        lines.append(f"{role}: {content}")
    if len(lines) == 1:
        return ""
    return "\n".join(lines)

logger = logging.getLogger(__name__)

REFLECTOR_AGENT = "_reflector"


@dataclass
class GrokCliAgentHandle:
    """In-process handle: grok CLI has no milkie sidecar / contextId."""

    name: str
    workspace: Path
    runtime: str = "grok-cli"
    persona: str = ""
    model: str = ""
    variables: dict[str, Any] = field(default_factory=dict)
    session_id: str = ""


class GrokCliProvider:
    """Drive one turn via ``grok --prompt-file`` and yield llm ``_progress``."""

    def __init__(self, *, runner=None, model: str = "") -> None:
        self._runner = runner
        self._model = model

    async def create_agent(
        self,
        agent_name: str,
        workspace_path: Path,
        model_name: Optional[str] = None,
        extra_variables: Optional[dict] = None,
        tools_override: Optional[list[str]] = None,
    ) -> GrokCliAgentHandle:
        workspace = Path(workspace_path)
        persona = self._persona_for(agent_name, workspace)
        model = model_name or self._model or self._resolve_model(agent_name)
        handle = GrokCliAgentHandle(
            name=agent_name,
            workspace=workspace,
            persona=persona,
            model=model,
            variables=dict(extra_variables or {}),
        )
        return handle

    @staticmethod
    def _persona_for(agent_name: str, workspace: Path) -> str:
        if agent_name == REFLECTOR_AGENT:
            from ....runtime.inspector import _REFLECT_SYSTEM_PROMPT

            return _REFLECT_SYSTEM_PROMPT
        from .....infra.workspace import WorkspaceLoader

        if not workspace.exists():
            raise FileNotFoundError(
                f"agent workspace not found for '{agent_name}': {workspace}"
            )
        return WorkspaceLoader(workspace).build_system_prompt()

    @staticmethod
    def _resolve_model(agent_name: str) -> str:
        try:
            from ...agent_config import resolve_agent_model

            return resolve_agent_model(agent_name) or ""
        except Exception:
            return ""

    def is_paused(self, agent: Any) -> bool:
        return False

    def is_error(self, agent: Any) -> bool:
        return False

    def capture_trace(self, agent: Any) -> Optional[Any]:
        return None

    def is_user_interrupt_paused(self, agent: Any) -> bool:
        return False

    async def call_llm(
        self,
        context: Any,
        prompt: str,
        temperature: float = 0.3,
        fast: bool = False,
        raise_on_error: bool = True,
    ) -> str:
        cwd = Path(getattr(context, "workspace", None) or ".")
        model = getattr(context, "model", None) or self._model
        try:
            data = await asyncio.to_thread(
                invoke_grok,
                prompt,
                cwd=cwd,
                model=model,
                runner=self._runner,
            )
            items = grok_json_to_progress(data)
            return (items[0].get("answer") or items[0].get("delta") or "") if items else ""
        except Exception as exc:
            if raise_on_error:
                raise
            return f"grok-cli call_llm error: {exc}"

    async def run_turn(
        self,
        agent: Any,
        message: Any,
        *,
        system_prompt: str = "",
        is_first_turn: bool = False,
        stream_mode: str = "delta",
    ) -> AsyncIterator[dict]:
        handle: GrokCliAgentHandle = agent
        text = message if isinstance(message, str) else str(message)
        history_block = _format_history(self._history(handle))
        parts = [p for p in (handle.persona, system_prompt, history_block, text) if p]
        prompt = "\n\n---\n\n".join(parts)
        cwd = handle.workspace if handle.workspace.exists() else Path(".")
        data = await asyncio.to_thread(
            invoke_grok,
            prompt,
            cwd=cwd,
            model=handle.model or self._model,
            runner=self._runner,
            timeout=DEFAULT_TIMEOUT_S,
        )
        reply = ""
        for item in grok_json_to_progress(data):
            reply = item.get("delta") or item.get("answer") or reply
            yield {"_progress": [item]}
        self._append_turn(handle, text, reply)

    @staticmethod
    def _history(handle: GrokCliAgentHandle) -> list:
        raw = handle.variables.get("history_messages") or handle.variables.get(KEY_HISTORY) or []
        return list(raw) if isinstance(raw, list) else []

    @staticmethod
    def _append_turn(handle: GrokCliAgentHandle, user_text: str, assistant_text: str) -> None:
        hist = GrokCliProvider._history(handle)
        hist.append({"role": "user", "content": user_text})
        if assistant_text:
            hist.append({"role": "assistant", "content": assistant_text})
        handle.variables["history_messages"] = hist
        handle.variables[KEY_HISTORY] = hist

    def set_variable(self, agent: Any, key: str, value: Any) -> None:
        agent.variables[key] = value

    def get_variable(self, agent: Any, key: str) -> Any:
        return agent.variables.get(key)

    def init_trajectory(self, agent: Any, path: str, overwrite: bool = False) -> None:
        return None

    def get_skill_observations(self, agent: Any) -> SkillObservationBatch:
        return SkillObservationBatch(skill_names=(), complete=True, reason="")

    def set_session_id(self, agent: Any, session_id: str) -> None:
        if session_id:
            agent.session_id = session_id

    def finalize_trajectory_on_error(self, agent: Any) -> None:
        return None

    def has_skill(self, agent: Any, name: str) -> bool:
        return False

    def register_skillkit(self, agent: Any, skillkit: Any) -> None:
        name = getattr(skillkit, "getName", lambda: type(skillkit).__name__)()
        logger.debug("GrokCliProvider.register_skillkit no-op for '%s'", name)

    def export_session(self, agent: Any) -> dict:
        history = self._history(agent)
        return {"history_messages": history, "variables": dict(agent.variables)}

    def import_session(self, agent: Any, portable_state: dict) -> None:
        if not isinstance(portable_state, dict):
            raise TypeError("portable_state must be a dict")
        history = portable_state.get("history_messages")
        if isinstance(history, list):
            agent.variables["history_messages"] = history
            agent.variables[KEY_HISTORY] = history
        variables = portable_state.get("variables")
        if isinstance(variables, dict):
            agent.variables.update(variables)

    def needs_history_restore(self) -> bool:
        # Same as milkie: no dolphin executor. Persona is baked at create_agent;
        # restore_to_agent must not touch agent.executor (heartbeat/chat crash).
        return False

    async def interrupt(self, agent: Any) -> None:
        return None

    async def resume(self, agent: Any, message: str) -> None:
        return None

    async def shutdown_sidecars(self) -> None:
        return None
