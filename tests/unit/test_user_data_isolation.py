"""#70:守卫测试 —— 测试进程中的 user_data 单例必须与真实 ~/.alfred 隔离。

历史教训:单测经 cron._write_event → 全局 get_user_data_manager() 直写生产
~/.alfred/logs/heartbeat_events.jsonl(475+ 条 test_agent 假事件),并可触发
真实日志轮转。隔离由 tests/conftest.py 的 autouse fixture 提供,本文件守卫其生效。
"""
from pathlib import Path


def test_user_data_singleton_isolated_from_real_home():
    from src.everbot.infra.user_data import get_user_data_manager
    real_home = Path("~/.alfred").expanduser()
    assert get_user_data_manager().alfred_home != real_home


def test_heartbeat_events_file_not_under_real_home():
    from src.everbot.infra.user_data import get_user_data_manager
    real_home = Path("~/.alfred").expanduser()
    events = get_user_data_manager().heartbeat_events_file
    assert not str(events).startswith(str(real_home))


def test_cron_write_event_lands_in_isolated_home(tmp_path):
    """行为级守卫:CronExecutor._write_event 落盘隔离目录,而非真实 ~/.alfred。"""
    import json
    from src.everbot.infra.user_data import get_user_data_manager

    from src.everbot.core.runtime.cron import CronExecutor

    real_events = Path("~/.alfred/logs/heartbeat_events.jsonl").expanduser()
    real_size_before = real_events.stat().st_size if real_events.exists() else 0

    executor = CronExecutor.__new__(CronExecutor)
    executor.agent_name = "isolation-probe"
    executor._write_event("job_skipped", skill="isolation-probe-skill", reason="guard")

    events_file = get_user_data_manager().heartbeat_events_file
    assert events_file != real_events
    assert events_file.exists()
    lines = [json.loads(l) for l in events_file.read_text().splitlines()]
    assert any(e.get("agent") == "isolation-probe" for e in lines)
    # 生产日志在本测例期间必须零增长
    real_size_after = real_events.stat().st_size if real_events.exists() else 0
    assert real_size_after == real_size_before
