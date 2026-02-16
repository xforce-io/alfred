"""
Shared fixtures and fakes for E2E tests.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from dolphin.core.common.constants import KEY_HISTORY
from src.everbot.core.session.session import SessionManager
from src.everbot.infra.user_data import UserDataManager
from src.everbot.web import app as web_app


class FakeContext:
    """Minimal context implementation required by ChatService and SessionManager."""

    def __init__(self):
        self._vars: dict[str, Any] = {
            "workspace_instructions": "Test workspace instructions.",
            "model_name": "gpt-4",
        }
        self._history: list[dict[str, Any]] = []

    def get_var_value(self, name: str) -> Any:
        return self._vars.get(name)

    def set_variable(self, name: str, value: Any) -> None:
        self._vars[name] = value
        if name == KEY_HISTORY and isinstance(value, list):
            self._history = list(value)

    def get_history_messages(self, normalize: bool = False) -> list[dict[str, Any]]:  # noqa: ARG002
        return list(self._history)

    def set_history_bucket(self, messages: Any) -> None:
        if hasattr(messages, "get_messages_as_dict"):
            self._history = messages.get_messages_as_dict()
        elif isinstance(messages, list):
            self._history = list(messages)

    def init_trajectory(self, path: str, overwrite: bool = True) -> None:
        trajectory_path = Path(path)
        trajectory_path.parent.mkdir(parents=True, exist_ok=True)
        if overwrite or not trajectory_path.exists():
            trajectory_path.write_text(json.dumps({"trajectory": [], "stages": []}), encoding="utf-8")

    def set_session_id(self, session_id: str) -> None:
        self._vars["session_id"] = session_id


class ScriptedAgent:
    """Fake agent that streams scripted progress events."""

    def __init__(self, name: str, script: list[Any]):
        self.name = name
        self.executor = SimpleNamespace(context=FakeContext())
        self._script = script
        self._interrupted = asyncio.Event()
        self.state = None
        self._pause_type = None

    async def interrupt(self) -> None:
        self._interrupted.set()

    async def resume_with_input(self, _message: str) -> None:
        self._interrupted.clear()

    def get_execution_trace(self) -> dict[str, Any]:
        return {"execution_summary": {"total_stages": len(self._script)}}

    async def continue_chat(self, **kwargs):
        message = kwargs.get("message", "")
        answer_chunks: list[str] = []

        try:
            for step in self._script:
                if self._interrupted.is_set():
                    break

                if isinstance(step, (int, float)):
                    remaining = float(step)
                    while remaining > 0:
                        if self._interrupted.is_set():
                            break
                        wait = min(0.05, remaining)
                        await asyncio.sleep(wait)
                        remaining -= wait
                    if self._interrupted.is_set():
                        break
                    continue

                event = step
                if isinstance(event, dict) and "_progress" in event:
                    for progress in event["_progress"]:
                        if progress.get("stage") == "llm":
                            delta = progress.get("delta")
                            if delta:
                                answer_chunks.append(str(delta))
                yield event
        finally:
            if message:
                self.executor.context._history.append({"role": "user", "content": message})
            if answer_chunks:
                self.executor.context._history.append(
                    {"role": "assistant", "content": "".join(answer_chunks)}
                )


def receive_until(ws, stop_predicate, max_messages: int = 50) -> list[dict[str, Any]]:
    """Receive websocket payloads until predicate matches."""
    messages: list[dict[str, Any]] = []
    for _ in range(max_messages):
        payload = ws.receive_json()
        messages.append(payload)
        if stop_predicate(payload):
            break
    return messages


@pytest.fixture
def isolated_web_env(monkeypatch, tmp_path):
    """Isolate global web service singletons to a temporary .alfred home."""
    alfred_home = tmp_path / ".alfred"
    user_data = UserDataManager(alfred_home=alfred_home)
    user_data.ensure_directories()

    session_manager = SessionManager(user_data.sessions_dir)
    web_app.chat_service.session_manager = session_manager
    web_app.chat_service.user_data = user_data
    web_app.chat_service.agent_service = SimpleNamespace(
        create_agent_instance=AsyncMock()
    )

    monkeypatch.setattr(web_app, "UserDataManager", lambda: UserDataManager(alfred_home=alfred_home))
    return SimpleNamespace(
        alfred_home=alfred_home,
        user_data=user_data,
        session_manager=session_manager,
    )


@pytest.fixture
def client():
    """FastAPI test client."""
    with TestClient(web_app.app) as test_client:
        yield test_client
