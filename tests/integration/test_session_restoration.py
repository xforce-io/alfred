"""
Session Restoration Integration Test

#43 调查重写:原测试断言 dolphin 时代的 save/restore 语义 —— alfred 从进程内 agent
经 ``snapshot.export_portable_session`` 抽取历史、restore 时经 ``import_portable_session``
灌回。#38 去 dolphin、收敛到 milkie 后这条路径已死:

- save 经 ``provider.export_session(agent)`` 取历史(milkie 下来自 serve 的 sqlite);
- restore 在 ``provider.needs_history_restore()`` 为 False(milkie 即如此)时**故意
  早返回、不灌回**(serve 用 sqlite/jsonl 自持久化跨重启恢复,见 milkie#130)。

故本测试改为校验**当前**契约:save/load 元数据往返一致;restore 对自持久化
provider 是 no-op(绝不触碰 agent.snapshot)。"历史跨重启恢复"的真实覆盖在
e2e ``test_milkie_serve_smoke.py::test_session_history_persists_across_serve_restart``。
"""

import pytest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from src.everbot.infra.user_data import UserDataManager
from src.everbot.core.session.session import SessionManager


@pytest.mark.asyncio
async def test_save_load_roundtrip_and_restore_is_noop_for_self_persisting_provider():
    """milkie 契约:save/load 往返保留元数据;restore 对自持久化 provider 不灌回历史。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        user_data = UserDataManager(alfred_home=tmp_path)
        user_data.ensure_directories()

        session_manager = SessionManager(user_data.sessions_dir)
        session_id = "test_integration_session"
        agent_name = "test_agent"
        user_data.init_agent_workspace(agent_name)

        # 假 agent:仅需 name(历史/变量经内存 provider 的 export_session,见 conftest)。
        mock_agent = MagicMock()
        mock_agent.name = agent_name

        # 1. 保存 + 落盘
        await session_manager.save_session(session_id, mock_agent, "gpt-4")
        assert (user_data.sessions_dir / f"{session_id}.json").exists()

        # 2. 读回:元数据往返一致
        loaded_data = await session_manager.load_session(session_id)
        assert loaded_data is not None
        assert loaded_data.agent_name == agent_name
        assert loaded_data.model_name == "gpt-4"
        # milkie 集成层无 serve,export_session 无历史可取 → 空(符合自持久化语义)
        assert loaded_data.history_messages == []

        # 3. restore 到新 agent:provider.needs_history_restore() 为 False(milkie)→
        #    persistence.restore_to_agent 早返回,绝不触碰 agent.snapshot(灌回是 serve 的事)。
        new_mock_agent = MagicMock()
        await session_manager.restore_to_agent(new_mock_agent, loaded_data)
        new_mock_agent.snapshot.import_portable_session.assert_not_called()
