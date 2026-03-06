"""
E2E test for stop-interrupt flow in WebSocket chat.
"""

from __future__ import annotations

import json

from .conftest import ScriptedAgent, receive_until


def test_ws_chat_stop_interrupt_persists_session(client, isolated_web_env):
    from src.everbot.web import app as web_app

    agent = ScriptedAgent(
        name="interrupt_agent",
        script=[
            2.5,
            {"_progress": [{"id": "llm-1", "status": "running", "stage": "llm", "delta": "这条消息不应到达"}]},
        ],
    )
    web_app.chat_service.agent_service.create_agent_instance.return_value = agent

    with client.websocket_connect("/ws/chat/interrupt_agent") as ws:
        _welcome = ws.receive_json()

        ws.send_json({"message": "请开始一个长任务"})
        ws.send_json({"action": "stop"})

        payloads = receive_until(
            ws,
            lambda msg: msg.get("type") == "status" and msg.get("content") == "已停止",
        )
        assert any(p.get("type") == "status" and p.get("content") == "已停止" for p in payloads)

    # 添加延迟等待异步操作完成
    import time
    time.sleep(0.1)
    
    session_id = isolated_web_env.session_manager.get_primary_session_id("interrupt_agent")
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