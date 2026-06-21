"""#92 件A:sidecar/agent 不可用时,内部诊断不得透传给终端用户。

#91 让 SidecarStartError 带 command/stderr/ABI 富诊断(且进日志);本测试钉住:
telegram 与 web 两个 channel 边界对用户**只发友好文案**,富诊断只进日志。
"""
import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.everbot.core.agent.provider.milkie.sidecar import SidecarStartError
from src.everbot.channels.user_messages import AGENT_UNAVAILABLE

_SECRET = "NODE_MODULE_VERSION 127 vs 131 :: /opt/homebrew/bin/node :: stderr-secret"


def _boom() -> SidecarStartError:
    return SidecarStartError(
        "milkie serve exited before emitting ready signal",
        cmd=["/opt/homebrew/bin/node", "/x/milkie/dist/cli/index.js"],
        returncode=3,
        stdout_tail=["booting milkie"],
        stderr_tail=[_SECRET],
    )


# --- telegram 边界 ----------------------------------------------------------

@pytest.mark.asyncio
async def test_telegram_create_failure_shows_friendly_msg_not_internal(tmp_path, caplog):
    from src.everbot.channels.telegram_channel import TelegramChannel

    sm = MagicMock()
    sm.get_cached_agent.return_value = None
    ch = TelegramChannel(bot_token="123:FAKE", session_manager=sm, default_agent="demo_agent")
    ch._bindings = {"111": "demo_agent"}
    ch._agent_service.create_agent_instance = AsyncMock(side_effect=_boom())
    ch._send_message = AsyncMock(return_value=True)

    with caplog.at_level(logging.ERROR):
        await ch._handle_message("111", "hi", {})

    # 用户侧:只发友好文案,绝不含内部诊断
    ch._send_message.assert_awaited_once()
    sent_text = ch._send_message.await_args.args[1]
    assert sent_text == AGENT_UNAVAILABLE
    assert _SECRET not in sent_text
    assert "NODE_MODULE_VERSION" not in sent_text
    # 富诊断仍进日志
    assert _SECRET in caplog.text


# --- web 边界 ---------------------------------------------------------------

class _ScriptedWS:
    def __init__(self):
        self.sent: list[dict] = []
        self.closed = False
        self._messages = [{"message": "hello"}]

    async def accept(self):
        pass

    async def send_json(self, payload: dict):
        self.sent.append(payload)

    async def receive_json(self):
        await asyncio.sleep(0.01)
        if self._messages:
            return self._messages.pop(0)
        raise RuntimeError("disconnect")

    async def close(self):
        self.closed = True


def _make_service_raising(tmp_path, exc):
    from src.everbot.web.services.chat_service import ChatService
    from src.everbot.core.channel.core_service import ChannelCoreService
    from src.everbot.core.session.session import SessionManager

    service = ChatService.__new__(ChatService)
    service._active_connections = {}
    service._connections_by_agent = {}
    service._last_activity = {}
    service._last_agent_broadcast = {}
    service._bootstrap_locks = {}
    service.user_data = SimpleNamespace(
        sessions_dir=tmp_path,
        get_session_trajectory_path=lambda a, s: tmp_path / f"{a}_{s}.jsonl",
    )
    service.session_manager = SessionManager(tmp_path)

    async def create_agent_instance(agent_name):  # noqa: ARG001
        raise exc

    service.agent_service = SimpleNamespace(create_agent_instance=create_agent_instance)
    service._core = ChannelCoreService(service.session_manager, service.agent_service, service.user_data)
    return service


@pytest.mark.asyncio
async def test_web_create_failure_shows_friendly_msg_not_internal(tmp_path, caplog):
    service = _make_service_raising(tmp_path, _boom())
    ws = _ScriptedWS()

    with caplog.at_level(logging.ERROR):
        await service.handle_chat_session(ws, "demo_agent", requested_session_id="web_x")

    blob = json.dumps(ws.sent, ensure_ascii=False)
    # 用户侧:任何下发 payload 都不得含内部诊断
    assert _SECRET not in blob
    assert "NODE_MODULE_VERSION" not in blob
    # 仍发了 error 帧 + 友好文案
    err_frames = [p for p in ws.sent if p.get("type") == "error"]
    assert err_frames and any(AGENT_UNAVAILABLE in (p.get("content") or "") for p in err_frames)
    # 富诊断仍进日志
    assert _SECRET in caplog.text
