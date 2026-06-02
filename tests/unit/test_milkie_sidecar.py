"""TDD: milkie serve 子进程管理(spawn / 等就绪信号 / 超时 / 优雅关闭)。

用 fake 子进程脚本(python -c)替代真 milkie,聚焦验证进程编排本身:
- 从 stdout 的 ``MILKIE_SERVE_READY <port>`` 解析端口;
- 就绪信号迟迟不来 → 超时;
- 进程在就绪前退出 → 明确报错(不是静默挂死);
- close 用 SIGTERM 让进程退出(生命周期绑定父进程,无僵尸)。
"""
import asyncio
import sys

import pytest

from everbot.core.agent.provider.milkie.sidecar import MilkieSidecar, parse_ready_signal


def _py(script: str) -> list[str]:
    return [sys.executable, "-c", script]


_READY_THEN_SLEEP = (
    "import sys,time;"
    "sys.stdout.write('MILKIE_SERVE_READY {port}\\n');sys.stdout.flush();"
    "time.sleep(30)"
)


def test_parse_ready_signal_extracts_port():
    assert parse_ready_signal("MILKIE_SERVE_READY 8723") == 8723


def test_parse_ready_signal_tolerates_surrounding_whitespace():
    assert parse_ready_signal("  MILKIE_SERVE_READY 8723  ") == 8723


def test_parse_ready_signal_ignores_noise():
    assert parse_ready_signal("some unrelated log") is None
    assert parse_ready_signal("") is None
    assert parse_ready_signal("MILKIE_SERVE_READY") is None  # 缺端口
    assert parse_ready_signal("MILKIE_SERVE_READY abc") is None  # 非数字


async def test_start_reads_port_from_ready_signal():
    sc = MilkieSidecar(_py(_READY_THEN_SLEEP.format(port=12345)))
    try:
        await sc.start()
        assert sc.port == 12345
        assert sc.base_url == "http://127.0.0.1:12345"
    finally:
        await sc.close()


async def test_start_times_out_when_no_ready_signal():
    sc = MilkieSidecar(_py("import time;time.sleep(30)"), ready_timeout=0.5)
    try:
        with pytest.raises((asyncio.TimeoutError, TimeoutError)):
            await sc.start()
    finally:
        await sc.close()


async def test_start_raises_if_process_exits_before_ready():
    sc = MilkieSidecar(_py("import sys;sys.exit(0)"), ready_timeout=5)
    try:
        with pytest.raises(RuntimeError):
            await sc.start()
    finally:
        await sc.close()


async def test_close_terminates_process():
    sc = MilkieSidecar(_py(_READY_THEN_SLEEP.format(port=1)))
    await sc.start()
    assert sc.returncode is None  # 仍在运行
    await sc.close()
    assert sc.returncode is not None  # 已被 SIGTERM 终止


async def test_close_is_idempotent():
    sc = MilkieSidecar(_py(_READY_THEN_SLEEP.format(port=1)))
    await sc.start()
    await sc.close()
    await sc.close()  # 二次调用不应抛错
