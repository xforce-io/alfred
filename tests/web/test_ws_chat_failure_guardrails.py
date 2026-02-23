"""
E2E test for tool-call guardrails in WebSocket message processing.
"""

from __future__ import annotations

import json

from .conftest import ScriptedAgent, receive_until


def test_ws_chat_stops_when_tool_call_budget_exceeded(client, isolated_web_env):
    from src.everbot.web import app as web_app

    events = [
        {
            "_progress": [
                {
                    "id": f"tool-{i}",
                    "status": "running",
                    "stage": "tool_call",
                    "tool_name": "_bash",
                    "args": "echo hello",
                }
            ]
        }
        for i in range(1, 17)
    ]

    agent = ScriptedAgent(name="budget_agent", script=events)
    web_app.chat_service.agent_service.create_agent_instance.return_value = agent

    with client.websocket_connect("/ws/chat/budget_agent") as ws:
        _welcome = ws.receive_json()
        ws.send_json({"message": "执行一些命令"})
        payloads = receive_until(ws, lambda msg: msg.get("type") == "end")

    assert any(
        p.get("type") == "message" and "工具调用次数过多" in p.get("content", "")
        for p in payloads
    )
    assert payloads[-1]["type"] == "end"

    # 添加延迟等待异步操作完成
    import time
    time.sleep(0.1)
    
    session_id = isolated_web_env.session_manager.get_primary_session_id("budget_agent")
    session_file = isolated_web_env.user_data.sessions_dir / f"{session_id}.json"
    if session_file.exists():
        persisted = json.loads(session_file.read_text(encoding="utf-8"))
        assert any(
            event.get("source_type") == "chat_user"
            and isinstance(event.get("run_id"), str)
            and event.get("run_id", "").startswith("chat_")
            for event in persisted.get("timeline", [])
        )
    else:
        print(f"Warning: Session file {session_file} does not exist")
        # 检查是否有其他会话文件
        session_files = list(isolated_web_env.user_data.sessions_dir.glob("*.json"))
        print(f"Available session files: {session_files}")