import asyncio

import pytest

from everbot.core.agent.provider.milkie.pool import SidecarPool


class _FakeSidecar:
    instances = []

    def __init__(self, cmd, env=None, ready_timeout=10.0):
        self.cmd = cmd
        self.started = 0
        self.closed = 0
        self.port = 18000 + len(_FakeSidecar.instances)
        _FakeSidecar.instances.append(self)

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"

    async def start(self):
        self.started += 1
        await asyncio.sleep(0)

    async def close(self):
        self.closed += 1


@pytest.fixture(autouse=True)
def _reset():
    _FakeSidecar.instances = []
    yield


def _pool():
    return SidecarPool(
        build=lambda name: (["node", "serve", name], {"K": "v"}),
        sidecar_factory=lambda cmd, env: _FakeSidecar(cmd, env),
    )


async def test_lazy_spawn_then_reuse():
    pool = _pool()
    s1 = await pool.get_or_spawn("alice")
    s2 = await pool.get_or_spawn("alice")
    assert s1 is s2
    assert s1.started == 1
    assert len(_FakeSidecar.instances) == 1


async def test_concurrent_same_agent_spawns_once():
    pool = _pool()
    results = await asyncio.gather(*[pool.get_or_spawn("alice") for _ in range(10)])
    assert all(r is results[0] for r in results)
    assert results[0].started == 1
    assert len(_FakeSidecar.instances) == 1


async def test_distinct_agents_distinct_sidecars():
    pool = _pool()
    a = await pool.get_or_spawn("alice")
    b = await pool.get_or_spawn("bob")
    assert a is not b
    assert {a.cmd[-1], b.cmd[-1]} == {"alice", "bob"}


async def test_shutdown_all_closes_and_clears():
    pool = _pool()
    a = await pool.get_or_spawn("alice")
    b = await pool.get_or_spawn("bob")
    await pool.shutdown_all()
    assert a.closed == 1 and b.closed == 1
    await pool.shutdown_all()
    assert a.closed == 1
    c = await pool.get_or_spawn("alice")
    assert c is not a


async def test_spawn_failure_not_cached():
    calls = {"n": 0}

    def factory(cmd, env):
        calls["n"] += 1
        s = _FakeSidecar(cmd, env)
        if calls["n"] == 1:
            async def boom():
                raise RuntimeError("ready timeout")
            s.start = boom
        return s

    pool = SidecarPool(build=lambda name: (["c"], {}), sidecar_factory=factory)
    with pytest.raises(RuntimeError):
        await pool.get_or_spawn("alice")
    s = await pool.get_or_spawn("alice")
    assert s.started == 1


async def test_start_failure_after_spawn_closes_child_and_not_cached():
    """start() 已 spawn 子进程后才失败(如 ready 超时)→ 子进程已存活但 start 抛错。
    get_or_spawn 必须 close() 回收(防 orphan 泄漏),不入池,且支持后续重试。"""
    spawned = []

    class _SpawnThenFailSidecar:
        def __init__(self, cmd, env=None, ready_timeout=10.0):
            self.cmd = cmd
            self.spawned = False
            self.closed = 0
            self.started = 0
            self.port = 18500

        @property
        def base_url(self):
            return f"http://127.0.0.1:{self.port}"

        async def start(self):
            self.spawned = True   # 子进程已 spawn(模拟 create_subprocess 成功)
            raise RuntimeError("ready timeout after spawn")

        async def close(self):
            self.closed += 1

    calls = {"n": 0}

    def factory(cmd, env):
        calls["n"] += 1
        if calls["n"] == 1:
            s = _SpawnThenFailSidecar(cmd, env)
            spawned.append(s)
            return s
        # 第二次重试:成功的 fake
        return _FakeSidecar(cmd, env)

    pool = SidecarPool(build=lambda name: (["c"], {}), sidecar_factory=factory)

    with pytest.raises(RuntimeError, match="ready timeout after spawn"):
        await pool.get_or_spawn("alice")

    failed = spawned[0]
    assert failed.spawned is True
    assert failed.closed == 1            # close() 被调一次(回收 orphan 子进程)
    assert "alice" not in pool._sidecars  # 失败不入池

    # 后续重试可成功(失败的 sidecar 不残留)
    retry = await pool.get_or_spawn("alice")
    assert retry is not failed
    assert retry.started == 1
    assert pool._sidecars["alice"] is retry


async def test_start_failure_swallows_close_errors_and_reraises_original():
    """close() 自身抛错时(best-effort)不掩盖原始 start 异常。"""
    class _BadCloseSidecar:
        def __init__(self, cmd, env=None, ready_timeout=10.0):
            self.closed = 0

        @property
        def base_url(self):
            return "http://127.0.0.1:18600"

        async def start(self):
            raise RuntimeError("original start failure")

        async def close(self):
            self.closed += 1
            raise OSError("close blew up")

    instances = []

    def factory(cmd, env):
        s = _BadCloseSidecar(cmd, env)
        instances.append(s)
        return s

    pool = SidecarPool(build=lambda name: (["c"], {}), sidecar_factory=factory)
    with pytest.raises(RuntimeError, match="original start failure"):
        await pool.get_or_spawn("alice")
    assert instances[0].closed == 1
    assert "alice" not in pool._sidecars
