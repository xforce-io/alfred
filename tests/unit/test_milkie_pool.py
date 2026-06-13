import asyncio

import pytest

from src.everbot.core.agent.provider.milkie.pool import SidecarPool


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


# ---------------------------------------------------------------------------
# #43:技能变化按需重生 —— peek / fingerprint freshness / lease(在飞保护)
# ---------------------------------------------------------------------------


def _fp_pool(fingerprint):
    return SidecarPool(
        build=lambda name: (["node", "serve", name], {"K": "v"}),
        sidecar_factory=lambda cmd, env: _FakeSidecar(cmd, env),
        fingerprint=fingerprint,
    )


async def test_peek_returns_none_before_spawn_and_sidecar_after():
    pool = _pool()
    assert pool.peek("alice") is None
    s = await pool.get_or_spawn("alice")
    assert pool.peek("alice") is s


async def test_fingerprint_unchanged_reuses_sidecar():
    pool = _fp_pool(lambda name: "fp-v1")
    s1 = await pool.get_or_spawn("alice")
    s2 = await pool.get_or_spawn("alice")
    assert s1 is s2
    assert s1.closed == 0
    assert len(_FakeSidecar.instances) == 1


async def test_fingerprint_changed_respawns_and_closes_old():
    fp = {"v": "fp-v1"}
    pool = _fp_pool(lambda name: fp["v"])
    old = await pool.get_or_spawn("alice")
    fp["v"] = "fp-v2"
    new = await pool.get_or_spawn("alice")
    assert new is not old
    assert old.closed == 1
    assert new.started == 1
    assert pool.peek("alice") is new


async def test_respawn_records_new_fingerprint_no_repeated_respawn():
    fp = {"v": "fp-v1"}
    pool = _fp_pool(lambda name: fp["v"])
    await pool.get_or_spawn("alice")
    fp["v"] = "fp-v2"
    new = await pool.get_or_spawn("alice")
    again = await pool.get_or_spawn("alice")
    assert again is new
    assert len(_FakeSidecar.instances) == 2  # 仅初始 + 一次重生


async def test_fingerprint_none_skips_check():
    """指纹返回 None = 该 agent 不参与检查(reflector / 注入 loader)→ 永不重生。"""
    calls = {"n": 0}

    def fp(name):
        calls["n"] += 1
        return None

    pool = _fp_pool(fp)
    s1 = await pool.get_or_spawn("alice")
    s2 = await pool.get_or_spawn("alice")
    assert s1 is s2
    assert s1.closed == 0
    assert calls["n"] >= 1  # 检查跑了,但 None 视为跳过


async def test_fingerprint_error_treated_as_unchanged(caplog):
    """每轮检查是优化路径:指纹计算抛错 → WARNING + 不重生、不打断取用。"""
    state = {"first": True}

    def fp(name):
        if state["first"]:
            state["first"] = False
            return "fp-v1"
        raise OSError("workspace transiently unreadable")

    pool = _fp_pool(fp)
    s1 = await pool.get_or_spawn("alice")
    with caplog.at_level("WARNING"):
        s2 = await pool.get_or_spawn("alice")
    assert s1 is s2
    assert s1.closed == 0
    assert any("fingerprint" in r.message for r in caplog.records)


async def test_lease_yields_sidecar_and_tracks_inflight():
    pool = _fp_pool(lambda name: "fp-v1")
    async with pool.lease("alice") as sidecar:
        assert sidecar is pool.peek("alice")
        assert pool._inflight["alice"] == 1
    assert pool._inflight["alice"] == 0


async def test_lease_decrements_on_exception():
    pool = _fp_pool(lambda name: "fp-v1")
    with pytest.raises(RuntimeError):
        async with pool.lease("alice"):
            raise RuntimeError("turn blew up")
    assert pool._inflight["alice"] == 0


async def test_respawn_deferred_while_lease_held_then_applies():
    """在飞 turn 持 lease 时指纹变化 → 本轮跳过重生(不腰斩);释放后下一次取用才重生。"""
    fp = {"v": "fp-v1"}
    pool = _fp_pool(lambda name: fp["v"])
    async with pool.lease("alice") as old:
        fp["v"] = "fp-v2"
        deferred = await pool.get_or_spawn("alice")
        assert deferred is old      # 有在飞 → 跳过
        assert old.closed == 0
    new = await pool.get_or_spawn("alice")
    assert new is not old
    assert old.closed == 1


async def test_concurrent_gets_during_change_respawn_once():
    """指纹变化时并发取用:per-agent 锁内串行,只重生一次,全部拿到新 sidecar。"""
    fp = {"v": "fp-v1"}
    pool = _fp_pool(lambda name: fp["v"])
    old = await pool.get_or_spawn("alice")
    fp["v"] = "fp-v2"
    results = await asyncio.gather(*[pool.get_or_spawn("alice") for _ in range(10)])
    assert all(r is results[0] for r in results)
    assert results[0] is not old
    assert len(_FakeSidecar.instances) == 2


async def test_shutdown_then_respawn_fresh_fingerprint_no_loop():
    """shutdown 后重新 spawn:指纹重新记录,后续无变化不重生。"""
    pool = _fp_pool(lambda name: "fp-v1")
    await pool.get_or_spawn("alice")
    await pool.shutdown_all()
    s = await pool.get_or_spawn("alice")
    s2 = await pool.get_or_spawn("alice")
    assert s is s2
    assert len(_FakeSidecar.instances) == 2  # shutdown 前后各一,无额外重生


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
