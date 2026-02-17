import json
from pathlib import Path

import pytest
from unittest.mock import MagicMock

from src.everbot.core.session.session import SessionManager


def _build_mock_agent(trace_payload):
    mock_agent = MagicMock()
    mock_agent.name = "trace_agent"
    mock_agent.get_execution_trace.return_value = trace_payload

    mock_context = MagicMock()
    mock_context.get_history_messages.return_value = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    mock_context.get_var_value.side_effect = lambda key: {
        "workspace_instructions": "Be helpful.",
        "model_name": "gpt-4",
        "current_time": "2026-02-08T00:00:00",
        "session_created_at": "2026-02-08T00:00:00",
    }.get(key)
    mock_agent.executor.context = mock_context
    mock_agent.snapshot.export_portable_session.return_value = {
        "schema_version": "portable_session.v1",
        "session_id": None,
        "history_messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ],
        "variables": {
            "workspace_instructions": "Be helpful.",
            "model_name": "gpt-4",
            "current_time": "2026-02-08T00:00:00",
        },
    }
    return mock_agent


@pytest.mark.asyncio
async def test_save_session_persists_context_trace(tmp_path: Path):
    manager = SessionManager(tmp_path)
    session_id = "trace_dict_case"
    expected_trace = {"execution_summary": {"total_stages": 3}}
    agent = _build_mock_agent(expected_trace)

    await manager.save_session(session_id, agent)

    loaded = await manager.load_session(session_id)
    assert loaded is not None
    assert loaded.context_trace == expected_trace


@pytest.mark.asyncio
async def test_save_session_parses_context_trace_from_json_string(tmp_path: Path):
    manager = SessionManager(tmp_path)
    session_id = "trace_json_string_case"
    expected_trace = {"llm_summary": {"total_tokens": 123}}
    agent = _build_mock_agent(json.dumps(expected_trace))

    await manager.save_session(session_id, agent)

    loaded = await manager.load_session(session_id)
    assert loaded is not None
    assert loaded.context_trace == expected_trace


@pytest.mark.asyncio
async def test_save_session_falls_back_to_empty_context_trace_on_invalid_payload(tmp_path: Path):
    manager = SessionManager(tmp_path)
    session_id = "trace_invalid_case"
    agent = _build_mock_agent("not-json")

    await manager.save_session(session_id, agent)

    loaded = await manager.load_session(session_id)
    assert loaded is not None
    assert loaded.context_trace == {}
