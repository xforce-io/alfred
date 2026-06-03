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
