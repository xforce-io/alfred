"""按 agent_name 维度管理 milkie serve 子进程:惰性 spawn + 常驻复用 + 统一关闭。

并发同 agent 经 per-agent 锁串行化,只 spawn 一次。spawn 失败不入池(下次重试)。
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, Tuple

from .sidecar import MilkieSidecar


def _default_factory(cmd, env):
    return MilkieSidecar(cmd, env=env)


class SidecarPool:
    def __init__(
        self,
        *,
        build: Callable[[str], Tuple[list, dict]],
        sidecar_factory: Callable[[list, dict], Any] = _default_factory,
    ) -> None:
        self._build = build
        self._factory = sidecar_factory
        self._sidecars: Dict[str, Any] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    def _lock(self, agent_name: str) -> asyncio.Lock:
        lock = self._locks.get(agent_name)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[agent_name] = lock
        return lock

    async def get_or_spawn(self, agent_name: str) -> Any:
        existing = self._sidecars.get(agent_name)
        if existing is not None:
            return existing
        async with self._lock(agent_name):
            existing = self._sidecars.get(agent_name)
            if existing is not None:
                return existing
            cmd, env = self._build(agent_name)
            sidecar = self._factory(cmd, env)
            try:
                await sidecar.start()
            except BaseException:
                # start() 已 spawn 子进程后才失败(如 ready 超时)→ 子进程已存活
                # 但未入池。必须 close() 回收,否则 orphan milkie serve 泄漏。
                # close 错误吞掉(best-effort),re-raise 原始异常。
                try:
                    await sidecar.close()
                except BaseException:
                    pass
                raise
            self._sidecars[agent_name] = sidecar
            return sidecar

    async def shutdown_all(self) -> None:
        sidecars = list(self._sidecars.values())
        self._sidecars.clear()
        if sidecars:
            await asyncio.gather(
                *(s.close() for s in sidecars), return_exceptions=True
            )
