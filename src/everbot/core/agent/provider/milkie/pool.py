"""按 agent_name 维度管理 milkie serve 子进程:惰性 spawn + 常驻复用 + 统一关闭。

并发同 agent 经 per-agent 锁串行化,只 spawn 一次。spawn 失败不入池(下次重试)。

#43 freshness:构造可注入 ``fingerprint(agent_name) -> Optional[str]``(技能集指纹)。
注入后,每次 ``get_or_spawn`` 命中缓存时重算指纹比对 spawn 时记录值 —— 变化且该 agent
无在飞请求(见 :meth:`lease`)→ 锁内 close 老 sidecar 并重生,使技能变更免重启 daemon
即生效;有在飞 → 本轮跳过、下一轮再试(最终一致,绝不腰斩在飞流)。指纹返回 None
表示该 agent 不参与检查(reflector / 注入式 loader);计算抛错按"未变化"处理并
WARNING(检查是优化路径,可用性优先;spawn 路径维持 fail-loud)。
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Callable, Dict, Optional, Tuple

from .sidecar import MilkieSidecar

logger = logging.getLogger(__name__)


def _default_factory(cmd, env):
    return MilkieSidecar(cmd, env=env)


class SidecarPool:
    def __init__(
        self,
        *,
        build: Callable[[str], Tuple[list, dict]],
        sidecar_factory: Callable[[list, dict], Any] = _default_factory,
        fingerprint: Optional[Callable[[str], Optional[str]]] = None,
    ) -> None:
        self._build = build
        self._factory = sidecar_factory
        self._fingerprint = fingerprint
        self._sidecars: Dict[str, Any] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._fingerprints: Dict[str, Optional[str]] = {}
        self._inflight: Dict[str, int] = {}

    def _lock(self, agent_name: str) -> asyncio.Lock:
        lock = self._locks.get(agent_name)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[agent_name] = lock
        return lock

    def peek(self, agent_name: str) -> Any:
        """同步取已存活的 sidecar(无则 None)—— 供 provider 的 sync 方法按
        agent 名解析当前 base_url(#43:handle 不再冻结端口)。不触发 spawn/检查。"""
        return self._sidecars.get(agent_name)

    async def get_or_spawn(self, agent_name: str) -> Any:
        # 注入 fingerprint 后命中路径也要做 freshness 检查/可能重生,必须全程在
        # per-agent 锁内(与 lease 登记互斥,消除"取到老 sidecar 后才登记"竞态)。
        # 未注入时保留无锁快路径(行为同旧版,常态零开销)。
        if self._fingerprint is None:
            existing = self._sidecars.get(agent_name)
            if existing is not None:
                return existing
        async with self._lock(agent_name):
            return await self._get_or_spawn_locked(agent_name)

    @asynccontextmanager
    async def lease(self, agent_name: str):
        """取 sidecar 并登记在飞 —— 包住整个流式 turn 的消费。

        登记与 freshness 检查同在 per-agent 锁内;lease 存续期间该 agent 的重生被
        推迟(本轮跳过、后续轮再试),绝不 close 正在服务的 sidecar。"""
        async with self._lock(agent_name):
            sidecar = await self._get_or_spawn_locked(agent_name)
            self._inflight[agent_name] = self._inflight.get(agent_name, 0) + 1
        try:
            yield sidecar
        finally:
            self._inflight[agent_name] -= 1

    async def _get_or_spawn_locked(self, agent_name: str) -> Any:
        existing = self._sidecars.get(agent_name)
        if existing is None:
            return await self._spawn_locked(agent_name)
        if self._fingerprint is None:
            return existing
        current = await self._current_fingerprint(agent_name)
        if current is None or current == self._fingerprints.get(agent_name):
            return existing
        if self._inflight.get(agent_name, 0) > 0:
            logger.info(
                "sidecar pool: '%s' 技能指纹已变化但有 %d 个在飞 turn,本轮跳过重生(下一轮再试)",
                agent_name, self._inflight[agent_name],
            )
            return existing
        logger.info("sidecar pool: '%s' 技能指纹变化,重生 sidecar 以刷新技能提示", agent_name)
        self._sidecars.pop(agent_name, None)
        self._fingerprints.pop(agent_name, None)
        try:
            await existing.close()
        except Exception:
            logger.warning("sidecar pool: 关闭 '%s' 旧 sidecar 失败(忽略,继续重生)", agent_name, exc_info=True)
        return await self._spawn_locked(agent_name, fingerprint=current)

    async def _spawn_locked(self, agent_name: str, fingerprint: Optional[str] = None) -> Any:
        # 指纹在 _build 之前取(_build 自己会再跑 discover):若两者之间技能恰好又变,
        # 指纹偏旧 → 下一轮多一次重生(冗余但安全);反向(指纹偏新)会漏刷新,不可取。
        if fingerprint is None and self._fingerprint is not None:
            fingerprint = await self._current_fingerprint(agent_name)
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
        self._fingerprints[agent_name] = fingerprint
        return sidecar

    async def _current_fingerprint(self, agent_name: str) -> Optional[str]:
        """线程池里算指纹(discover_skills 读盘 + 缩水重试含同步 sleep,不能阻塞事件循环)。
        抛错 → WARNING + None(调用方按"未变化/跳过"处理)。"""
        try:
            return await asyncio.to_thread(self._fingerprint, agent_name)
        except Exception:
            logger.warning(
                "sidecar pool: '%s' 技能 fingerprint 计算失败,本轮按未变化处理", agent_name, exc_info=True,
            )
            return None

    async def shutdown_all(self) -> None:
        sidecars = list(self._sidecars.values())
        self._sidecars.clear()
        self._fingerprints.clear()
        if sidecars:
            await asyncio.gather(
                *(s.close() for s in sidecars), return_exceptions=True
            )
