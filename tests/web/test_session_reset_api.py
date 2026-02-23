"""
E2E test for session reset API.
"""

from __future__ import annotations

import json
from types import SimpleNamespace


def test_session_reset_api_cleans_cache_disk_and_tmp(client, isolated_web_env):
    from src.everbot.web import app as web_app

    session_id = "web_session_reset_agent"
    agent_name = "reset_agent"

    isolated_web_env.session_manager.cache_agent(
        session_id=session_id,
        agent=SimpleNamespace(name=agent_name),
        agent_name=agent_name,
        model_name="gpt-4",
    )
    isolated_web_env.session_manager.append_timeline_event(session_id, {"type": "turn_start"})

    session_file = isolated_web_env.user_data.sessions_dir / f"{session_id}.json"
    session_file.write_text(
        json.dumps(
            {
                "session_id": session_id,
                "agent_name": agent_name,
                "model_name": "gpt-4",
                "history_messages": [{"role": "user", "content": "hello"}],
                "variables": {},
                "created_at": "2026-02-09T00:00:00",
                "updated_at": "2026-02-09T00:00:00",
                "timeline": [],
                "context_trace": {},
            }
        ),
        encoding="utf-8",
    )

    agent_tmp = web_app.get_user_data_manager().get_agent_dir(agent_name) / "tmp"
    agent_tmp.mkdir(parents=True, exist_ok=True)
    (agent_tmp / "stale.tmp").write_text("stale", encoding="utf-8")

    response = client.post(f"/api/agents/{agent_name}/sessions/reset")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["removed_sessions"] >= 1

    assert isolated_web_env.session_manager.get_cached_agent(session_id) is None
    assert isolated_web_env.session_manager.get_timeline(session_id) == []
    assert not session_file.exists()
    assert agent_tmp.exists()
    assert list(agent_tmp.iterdir()) == []


def test_session_reset_api_cleans_all_sessions_for_agent(client, isolated_web_env):
    agent_name = "reset_agent"
    session_a = "web_session_reset_agent"
    session_b = "web_session_reset_agent__20260209010101_x1"

    for session_id in (session_a, session_b):
        session_file = isolated_web_env.user_data.sessions_dir / f"{session_id}.json"
        session_file.write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "agent_name": agent_name,
                    "model_name": "gpt-4",
                    "history_messages": [{"role": "user", "content": session_id}],
                    "variables": {},
                    "created_at": "2026-02-09T00:00:00",
                    "updated_at": "2026-02-09T00:00:00",
                    "timeline": [],
                    "context_trace": {},
                }
            ),
            encoding="utf-8",
        )

    response = client.post(f"/api/agents/{agent_name}/sessions/reset")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["removed_sessions"] >= 2

    assert not (isolated_web_env.user_data.sessions_dir / f"{session_a}.json").exists()
    assert not (isolated_web_env.user_data.sessions_dir / f"{session_b}.json").exists()
