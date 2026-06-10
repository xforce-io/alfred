"""Shared test configuration."""

import sys
from pathlib import Path

import pytest

# Ensure project root is on sys.path so that `from src.everbot...` works
# regardless of how pytest is invoked (run_tests.sh, bare pytest, CI, etc.).
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


@pytest.fixture(autouse=True)
def _isolate_alfred_home(tmp_path, monkeypatch):
    """#70:把 ALFRED_HOME 隔离到 tmp_path 并重置 user_data 单例。

    任何测例经全局 get_user_data_manager() 的写入(如 cron._write_event)都不得
    落到真实 ~/.alfred —— 曾把数百条 test_agent 假事件写进生产
    heartbeat_events.jsonl,且可触发真实日志轮转。守卫见 test_user_data_isolation.py。
    """
    from src.everbot.infra.user_data import reset_user_data_manager

    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "alfred-home"))
    reset_user_data_manager()
    yield
    reset_user_data_manager()
