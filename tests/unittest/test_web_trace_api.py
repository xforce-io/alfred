import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from src.everbot.core.session.session import SessionData, SessionManager
from src.everbot.web import app as web_app


class _StrictSessionManager:
    def __init__(self, session_data):
        self._session_data = session_data

    async def load_session(self, _session_id: str):
        return self._session_data

    async def migrate_legacy_sessions_for_agent(self, _agent_name: str):
        return False

    def get_primary_session_id(self, agent_name: str):
        return f"web_session_{agent_name}"

    def get_cached_agent(self, _session_id: str):
        raise AssertionError("cached agent should not be used")

    def is_valid_agent_session_id(self, agent_name: str, session_id: str):
        return SessionManager.is_valid_agent_session_id(agent_name, session_id)


class _FakeUserDataManager:
    def __init__(self, base_dir: Path):
        self._base_dir = base_dir

    def get_agent_dir(self, agent_name: str) -> Path:
        return self._base_dir / "agents" / agent_name

    def get_agent_tmp_dir(self, agent_name: str) -> Path:
        return self.get_agent_dir(agent_name) / "tmp"

    def get_session_trajectory_path(self, agent_name: str, session_id: str) -> Path:
        return self.get_agent_tmp_dir(agent_name) / f"trajectory_{session_id}.json"


@pytest.mark.asyncio
async def test_trace_api_uses_persisted_session_data_only(monkeypatch, tmp_path: Path):
    agent_name = "demo_agent"
    session_id = f"web_session_{agent_name}"
    persisted_context_trace = {"execution_summary": {"total_stages": 2}}
    persisted_timeline = [{"type": "turn_end", "total_duration_ms": 42}]
    persisted_trajectory = {"trajectory": [{"role": "user", "content": "hi"}], "stages": []}

    trajectory_file = tmp_path / "agents" / agent_name / "tmp" / f"trajectory_{session_id}.json"
    trajectory_file.parent.mkdir(parents=True, exist_ok=True)
    trajectory_file.write_text(json.dumps(persisted_trajectory), encoding="utf-8")

    session_data = SessionData(
        session_id=session_id,
        agent_name=agent_name,
        model_name="gpt-4",
        history_messages=[{"role": "user", "content": "hello"}],
        variables={},
        created_at="2026-02-08T00:00:00",
        updated_at="2026-02-08T00:01:00",
        timeline=persisted_timeline,
        context_trace=persisted_context_trace,
    )

    web_app.chat_service.session_manager = _StrictSessionManager(session_data)
    monkeypatch.setattr(web_app, "UserDataManager", lambda: _FakeUserDataManager(tmp_path))

    result = await web_app.get_agent_session_trace(agent_name)

    assert result["trace_source"] == "persisted"
    assert result["context_trace"] == persisted_context_trace
    assert result["timeline"] == persisted_timeline
    assert result["trajectory"] == persisted_trajectory


@pytest.mark.asyncio
async def test_trace_api_returns_empty_structures_when_no_persisted_session(monkeypatch, tmp_path: Path):
    agent_name = "empty_agent"
    web_app.chat_service.session_manager = _StrictSessionManager(None)
    monkeypatch.setattr(web_app, "UserDataManager", lambda: _FakeUserDataManager(tmp_path))

    result = await web_app.get_agent_session_trace(agent_name)

    assert result["trace_source"] == "persisted"
    assert result["context_trace"] == {}
    assert result["timeline"] == []
    assert result["trajectory"] == {}


@pytest.mark.asyncio
async def test_trace_api_is_stable_across_refresh_requests(monkeypatch, tmp_path: Path):
    agent_name = "refresh_agent"
    session_id = f"web_session_{agent_name}"
    persisted_context_trace = {"execution_summary": {"total_stages": 1}}
    persisted_timeline = [{"type": "turn_end", "total_duration_ms": 11}]
    persisted_trajectory = {"trajectory": [{"role": "assistant", "content": "ok"}], "stages": []}

    trajectory_file = tmp_path / "agents" / agent_name / "tmp" / f"trajectory_{session_id}.json"
    trajectory_file.parent.mkdir(parents=True, exist_ok=True)
    trajectory_file.write_text(json.dumps(persisted_trajectory), encoding="utf-8")

    session_data = SessionData(
        session_id=session_id,
        agent_name=agent_name,
        model_name="gpt-4",
        history_messages=[],
        variables={},
        created_at="2026-02-08T00:00:00",
        updated_at="2026-02-08T00:00:01",
        timeline=persisted_timeline,
        context_trace=persisted_context_trace,
    )
    web_app.chat_service.session_manager = _StrictSessionManager(session_data)
    monkeypatch.setattr(web_app, "UserDataManager", lambda: _FakeUserDataManager(tmp_path))

    first = await web_app.get_agent_session_trace(agent_name)
    second = await web_app.get_agent_session_trace(agent_name)
    assert first == second


@pytest.mark.asyncio
async def test_trace_api_uses_requested_session_id_for_trajectory(monkeypatch, tmp_path: Path):
    agent_name = "demo_agent"
    requested_session_id = "web_session_demo_agent_alt"
    persisted_trajectory = {"trajectory": [{"role": "assistant", "content": "alt"}], "stages": []}

    trajectory_file = tmp_path / "agents" / agent_name / "tmp" / f"trajectory_{requested_session_id}.json"
    trajectory_file.parent.mkdir(parents=True, exist_ok=True)
    trajectory_file.write_text(json.dumps(persisted_trajectory), encoding="utf-8")

    web_app.chat_service.session_manager = _StrictSessionManager(None)
    monkeypatch.setattr(web_app, "UserDataManager", lambda: _FakeUserDataManager(tmp_path))

    result = await web_app.get_agent_session_trace(agent_name, session_id=requested_session_id)
    assert result["session_id"] == requested_session_id
    assert result["trajectory"] == persisted_trajectory


@pytest.mark.asyncio
async def test_trace_api_rejects_invalid_session_id(monkeypatch, tmp_path: Path):
    agent_name = "demo_agent"
    invalid_session_id = "web_session_demo_agent/../../etc/passwd"
    web_app.chat_service.session_manager = _StrictSessionManager(None)
    monkeypatch.setattr(web_app, "UserDataManager", lambda: _FakeUserDataManager(tmp_path))

    with pytest.raises(HTTPException) as exc_info:
        await web_app.get_agent_session_trace(agent_name, session_id=invalid_session_id)
    assert exc_info.value.status_code == 400
