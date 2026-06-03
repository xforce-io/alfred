"""Manage a ``milkie serve`` child process (#86, D4).

生命周期绑定父进程(alfred):spawn → 读 stdout 的 ``MILKIE_SERVE_READY <port>``
就绪信号 → 暴露 ``base_url`` → ``close()`` 用 SIGTERM 优雅终止(超时再 SIGKILL)。

命令由调用方注入(``cmd``)而非硬编码,便于测试喂 fake 子进程、e2e 喂真
``milkie serve``。
"""
from __future__ import annotations

import asyncio
import re
from typing import List, Optional

_READY_RE = re.compile(r"^MILKIE_SERVE_READY\s+(\d+)$")


def parse_ready_signal(line: str) -> Optional[int]:
    """Extract the port from a ``MILKIE_SERVE_READY <port>`` line, else ``None``."""
    m = _READY_RE.match(line.strip())
    return int(m.group(1)) if m else None


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
            env=self._env,
        )
        self.port = await asyncio.wait_for(self._await_ready(), self._ready_timeout)

    async def _await_ready(self) -> int:
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            line = await self._proc.stdout.readline()
            if not line:  # EOF — process exited without ever signalling ready
                raise RuntimeError("milkie serve exited before emitting ready signal")
            port = parse_ready_signal(line.decode("utf-8", "replace"))
            if port is not None:
                return port

    async def close(self) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()  # SIGTERM — serve binds shutdown to this
        try:
            await asyncio.wait_for(proc.wait(), 5.0)
        except (asyncio.TimeoutError, TimeoutError):
            proc.kill()
            await proc.wait()
