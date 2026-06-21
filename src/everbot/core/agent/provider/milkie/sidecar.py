"""Manage a ``milkie serve`` child process (#86, D4).

生命周期绑定父进程(alfred):spawn → 读 stdout 的 ``MILKIE_SERVE_READY <port>``
就绪信号 → 暴露 ``base_url`` → ``close()`` 用 SIGTERM 优雅终止(超时再 SIGKILL)。

命令由调用方注入(``cmd``)而非硬编码,便于测试喂 fake 子进程、e2e 喂真
``milkie serve``。

#91 件1:启动失败(就绪前退出 / 就绪超时)必须可诊断 —— stderr 单独捕获,异常
``SidecarStartError`` 携带 command、exit code、stdout/stderr 末 N 行。ABI mismatch、
``node_modules`` 缺失、native addon load failure 都打在 stderr;不捕获 → 排查时只能
手动复现。
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from typing import Deque, List, Optional

logger = logging.getLogger(__name__)

_READY_RE = re.compile(r"^MILKIE_SERVE_READY\s+(\d+)$")

# 诊断尾部保留的行数(stdout / stderr 各自末 N 行)。
SIDECAR_DIAG_TAIL_LINES = 20


def parse_ready_signal(line: str) -> Optional[int]:
    """Extract the port from a ``MILKIE_SERVE_READY <port>`` line, else ``None``."""
    m = _READY_RE.match(line.strip())
    return int(m.group(1)) if m else None


class SidecarStartError(RuntimeError):
    """milkie serve 启动失败(就绪前退出 / 就绪超时),携带可执行诊断。

    ``str(self)`` 含 command、exit code、stderr/stdout 末 N 行 —— 直接进日志即可定位
    ABI mismatch / 依赖缺失,无需手动复现。注意:目前该 message 也会经
    telegram_channel 透传给用户(#92 负责做用户侧降级映射)。
    """

    def __init__(
        self,
        reason: str,
        *,
        cmd: List[str],
        returncode: Optional[int],
        stdout_tail: List[str],
        stderr_tail: List[str],
    ) -> None:
        self.reason = reason
        self.cmd = list(cmd)
        self.returncode = returncode
        self.stdout_tail = list(stdout_tail)
        self.stderr_tail = list(stderr_tail)
        super().__init__(self._format())

    def _format(self) -> str:
        parts = [
            self.reason,
            f"  command: {' '.join(self.cmd)}",
            f"  exit code: {self.returncode}",
        ]
        if self.stderr_tail:
            parts.append(f"  stderr (last {len(self.stderr_tail)} lines):")
            parts.extend(f"    {ln}" for ln in self.stderr_tail)
        if self.stdout_tail:
            parts.append(f"  stdout (last {len(self.stdout_tail)} lines):")
            parts.extend(f"    {ln}" for ln in self.stdout_tail)
        return "\n".join(parts)


class MilkieSidecar:
    """A spawned ``milkie serve`` process whose lifecycle is bound to ours."""

    def __init__(
        self,
        cmd: List[str],
        *,
        env: Optional[dict] = None,
        ready_timeout: float = 10.0,
    ) -> None:
        self._cmd = cmd
        self._env = env
        self._ready_timeout = ready_timeout
        self._proc: Optional[asyncio.subprocess.Process] = None
        self.port: Optional[int] = None
        # 就绪前读到的 stdout 行(非 ready 信号)保留末 N 行用于失败诊断。
        self._stdout_tail: Deque[str] = deque(maxlen=SIDECAR_DIAG_TAIL_LINES)
        # 就绪后持续排空 stdout/stderr 的后台任务:milkie serve 就绪后仍往管道写日志,
        # 不排空 → OS pipe 缓冲(~64KB)填满 → 子进程 write 阻塞 → /chat 挂死。
        self._drain_task: Optional[asyncio.Task] = None
        self._drain_stderr_task: Optional[asyncio.Task] = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def returncode(self) -> Optional[int]:
        return self._proc.returncode if self._proc is not None else None

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self._cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,  # #91:单独捕获,失败时进诊断
            env=self._env,
        )
        try:
            self.port = await asyncio.wait_for(self._await_ready(), self._ready_timeout)
        except (asyncio.TimeoutError, TimeoutError):
            # 就绪信号超时:进程多半仍存活,kill 后采集诊断(returncode/stderr)。
            proc = self._proc
            if proc is not None and proc.returncode is None:
                proc.kill()
            raise await self._build_start_error(
                f"milkie serve did not emit ready signal within {self._ready_timeout}s"
            )
        # 就绪后:持续排空 stdout/stderr 到 EOF,防 pipe 缓冲填满阻塞子进程。
        self._drain_task = asyncio.create_task(self._drain(self._proc.stdout, "stdout"))
        stderr = getattr(self._proc, "stderr", None)
        if stderr is not None:
            self._drain_stderr_task = asyncio.create_task(self._drain(stderr, "stderr"))

    async def _drain(self, stream, label: str) -> None:
        """就绪后持续读 stream 至 EOF(丢弃,debug 留痕)。

        子进程退出 → pipe EOF → readline 返回 b"" → 自然结束;close() 也会主动
        cancel。CancelledError 静默吞(正常关停路径)。"""
        if stream is None:
            return
        try:
            while True:
                line = await stream.readline()
                if not line:  # EOF — 子进程已退出
                    break
                logger.debug(
                    "milkie serve %s: %s", label, line.decode("utf-8", "replace").rstrip()
                )
        except asyncio.CancelledError:
            pass

    async def _await_ready(self) -> int:
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            line = await self._proc.stdout.readline()
            if not line:  # EOF — 进程在发出就绪信号前退出
                raise await self._build_start_error(
                    "milkie serve exited before emitting ready signal"
                )
            decoded = line.decode("utf-8", "replace")
            port = parse_ready_signal(decoded)
            if port is not None:
                return port
            self._stdout_tail.append(decoded.rstrip("\n"))  # 非 ready 行 → 诊断尾部

    async def _build_start_error(self, reason: str) -> SidecarStartError:
        """采集 returncode + stderr 末 N 行,构造可诊断异常。"""
        proc = self._proc
        rc: Optional[int] = None
        if proc is not None:
            try:
                rc = await asyncio.wait_for(proc.wait(), 2.0)
            except (asyncio.TimeoutError, TimeoutError):
                rc = proc.returncode
        return SidecarStartError(
            reason,
            cmd=self._cmd,
            returncode=rc,
            stdout_tail=list(self._stdout_tail),
            stderr_tail=await self._read_stderr_tail(),
        )

    async def _read_stderr_tail(self) -> List[str]:
        proc = self._proc
        stream = getattr(proc, "stderr", None) if proc is not None else None
        if stream is None:
            return []
        try:
            data = await asyncio.wait_for(stream.read(), 2.0)  # 进程已退出 → EOF,不阻塞
        except (asyncio.TimeoutError, TimeoutError):
            data = b""
        lines = data.decode("utf-8", "replace").splitlines()
        return lines[-SIDECAR_DIAG_TAIL_LINES:]

    async def close(self) -> None:
        # 先停排空任务(robust:可能从未 start 过 → task is None)。
        for attr in ("_drain_task", "_drain_stderr_task"):
            task = getattr(self, attr)
            setattr(self, attr, None)
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()  # SIGTERM — serve binds shutdown to this
        try:
            await asyncio.wait_for(proc.wait(), 5.0)
        except (asyncio.TimeoutError, TimeoutError):
            proc.kill()
            await proc.wait()
