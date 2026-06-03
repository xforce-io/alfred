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


class _FakeStdout:
    """可控的 async readline:先吐 queued 行,耗尽后保持 pending(模拟子进程仍在跑、
    随时可能再写 stdout —— 真实 pipe 不会 EOF,直到进程退出)。"""

    def __init__(self, lines: list[bytes]):
        self._queue = asyncio.Queue()
        for ln in lines:
            self._queue.put_nowait(ln)
        self.read_count = 0

    async def readline(self) -> bytes:
        self.read_count += 1
        # 队列空 → await 永久挂起(直到任务被 cancel),绝不返回 EOF。
        return await self._queue.get()


class _FakeProc:
    def __init__(self, stdout):
        self.stdout = stdout
        self.returncode = None
        self.terminated = 0

    def terminate(self):
        self.terminated += 1
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


async def test_drain_consumes_stdout_after_ready_and_close_cancels_it(monkeypatch):
    """就绪后 start() 返回(即便 stdout 后续还有更多行),且后台 drain 任务持续消费这些
    额外行(防 pipe 阻塞);close() 干净取消 drain 任务,无挂死。"""
    ready = b"MILKIE_SERVE_READY 7777\n"
    extra = [b"request log 1\n", b"request log 2\n", b"request log 3\n"]
    stdout = _FakeStdout([ready, *extra])
    proc = _FakeProc(stdout)

    async def fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    sc = MilkieSidecar(["node", "x"], ready_timeout=2.0)
    await sc.start()
    # (a) 就绪解析成功,即便 stdout 仍有更多行排队
    assert sc.port == 7777

    # (b) 额外行被 drain 任务消费:让出事件循环直到队列被排空
    for _ in range(50):
        if stdout._queue.empty():
            break
        await asyncio.sleep(0)
    assert stdout._queue.empty(), "drain 任务应消费完所有额外 stdout 行"
    # readline 调用数 = ready(1) + 3 额外行 + 1 次正挂起的下一行
    assert stdout.read_count >= 1 + len(extra)
    assert sc._drain_task is not None and not sc._drain_task.done()

    # (c) close() 干净取消 drain 任务,不挂死
    await asyncio.wait_for(sc.close(), 2.0)
    assert sc._drain_task is None
    assert proc.terminated == 1


async def test_close_robust_when_never_started():
    """从未 start(无 _drain_task / 无 proc)→ close() no-op 不抛。"""
    sc = MilkieSidecar(["node", "x"])
    assert sc._drain_task is None
    await sc.close()  # must NOT raise


async def test_drain_ends_naturally_on_stdout_eof(monkeypatch):
    """子进程退出 → stdout EOF(readline 返回 b"")→ drain 任务自然结束(非靠 cancel)。"""
    class _EofStdout:
        def __init__(self):
            self._q = asyncio.Queue()
            self._q.put_nowait(b"MILKIE_SERVE_READY 5555\n")
            self._q.put_nowait(b"one more line\n")
            self._q.put_nowait(b"")  # EOF
        async def readline(self):
            return await self._q.get()

    proc = _FakeProc(_EofStdout())

    async def fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    sc = MilkieSidecar(["node", "x"], ready_timeout=2.0)
    await sc.start()
    assert sc.port == 5555
    await asyncio.wait_for(sc._drain_task, 2.0)  # EOF → 自然结束
    assert sc._drain_task.done()
    await sc.close()


async def test_sidecar_passes_env(monkeypatch):
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["env"] = kwargs.get("env")
        raise RuntimeError("stop here")

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    sc = MilkieSidecar(["node", "x"], env={"OPENAI_API_KEY": "sk"})
    with pytest.raises(RuntimeError):
        await sc.start()
    assert captured["env"] == {"OPENAI_API_KEY": "sk"}
