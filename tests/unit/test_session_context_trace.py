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


# ===========================================================================
# context_trace multi-turn token accumulation
# ===========================================================================


@pytest.mark.asyncio
async def test_context_trace_records_output_tokens_for_tool_call_turns(tmp_path: Path):
    """Production bug: when LLM produces thinking-only output in the first turn
    (output_tokens=0 in visible text) followed by tool calls and a final text
    response, context_trace records estimated_output_tokens=0 because it only
    captures the first LLM turn's token count.

    Example from production context_trace:
        id=0369263f, estimated_input_tokens=15607, estimated_output_tokens=0,
        answer="", status=completed
    But the agent actually produced 4 tool calls and a text reply.

    _extract_context_trace should accumulate tokens across ALL LLM turns in a
    single agent execution, not just snapshot the first turn.
    """
    manager = SessionManager(tmp_path)
    session_id = "trace_multi_turn"

    # Simulate agent that had multiple LLM turns:
    # Turn 1: thinking only (0 visible output tokens) + tool_call
    # Turn 2: tool_result → more thinking + tool_call
    # Turn 3: tool_result → final text response (200 output tokens)
    # But get_execution_trace() only returns the first turn's snapshot
    trace_with_zero_output = {
        "id": "0369263f",
        "estimated_input_tokens": 15607,
        "estimated_output_tokens": 0,  # Only first turn recorded!
        "answer": "",
        "status": "completed",
        "think": "Let me analyze this request...",
    }
    agent = _build_mock_agent(trace_with_zero_output)

    # The agent actually produced visible output (tool calls + text)
    agent.snapshot.export_portable_session.return_value = {
        "schema_version": "portable_session.v1",
        "session_id": None,
        "history_messages": [
            {"role": "user", "content": "帮我注册一个定时任务"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "tc1", "function": {"name": "_load_resource_skill", "arguments": "{}"}}
            ]},
            {"role": "tool", "content": "skill loaded", "tool_call_id": "tc1"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "tc2", "function": {"name": "_bash", "arguments": "{\"command\": \"routine add\"}"}}
            ]},
            {"role": "tool", "content": "routine added", "tool_call_id": "tc2"},
            {"role": "assistant", "content": "好的，我已经帮你注册了定时任务：每两分钟检测一次会话。"},
        ],
        "variables": {},
    }

    await manager.save_session(session_id, agent)

    loaded = await manager.load_session(session_id)
    assert loaded is not None
    trace = loaded.context_trace

    # The trace should reflect actual output, not just the first turn
    assert trace.get("estimated_output_tokens", 0) > 0, (
        f"context_trace.estimated_output_tokens={trace.get('estimated_output_tokens')} "
        "but agent produced tool calls and a text response. "
        "_extract_context_trace only captures the first LLM turn's token count, "
        "missing subsequent tool-call and response turns. "
        "Token counts should be accumulated across all turns in the execution."
    )
