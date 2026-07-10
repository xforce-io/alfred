"""TDD C2: MilkieProvider 走 AgentProvider 契约。

run_turn(handle, message, ...) 产 dolphin ``_progress`` 流(统一中立契约),
turn_orchestrator 在其上套 policy。create_agent 返回 MilkieAgentHandle(base_url
+ context_id)。用 httpx MockTransport 注入预设 SSE 验证编排。
"""
import json

import httpx
import pytest

from src.everbot.core.agent.provider.milkie import provider as provider_mod
from src.everbot.core.agent.provider.milkie.provider import (
    MilkieAgentError,
    MilkieAgentHandle,
    MilkieProvider,
)


def _sse(*frames: tuple[str, dict]) -> str:
    return "".join(f"event: {ev}\ndata: {json.dumps(d)}\n\n" for ev, d in frames)


def _provider(sse_text: str, capture: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture["payload"] = json.loads(request.content)
            capture["url"] = str(request.url)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse_text.encode("utf-8")
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return MilkieProvider("http://sidecar", client=client), client


async def test_create_agent_returns_handle():
    # create_agent 现经 pool spawn/复用 per-agent serve,handle 携带该 serve 的
    # base_url(动态端口),而非固定 config base_url。注入 fake pool 验证此契约。
    class _Sidecar:
        base_url = "http://sidecar"

    class _Pool:
        async def get_or_spawn(self, name):
            return _Sidecar()

    p = MilkieProvider("http://config-base", pool=_Pool())
    h = await p.create_agent("smoke", "/ws")
    assert isinstance(h, MilkieAgentHandle)
    assert h.base_url == "http://sidecar"
    assert h.context_id  # 非空


async def test_create_agent_handle_carries_agent_name():
    """handle.name 必须 = agent_name:trunk(web chat_service / session persistence)
    以 agent.name 取值,milkie handle 缺它会 AttributeError 崩溃(web 连接/会话保存)。"""
    class _Sidecar:
        base_url = "http://sidecar"

    class _Pool:
        async def get_or_spawn(self, name):
            return _Sidecar()

    p = MilkieProvider("http://config-base", pool=_Pool())
    h = await p.create_agent("alice", "/ws")
    assert h.name == "alice"
    assert h.base_url == "http://sidecar"
    assert h.context_id.startswith("alice-")


def test_handle_accepts_name_field():
    """MilkieAgentHandle 直接以 name 关键字构造可用;name 默认 "" 不破既有 2-arg 位置构造。"""
    h = MilkieAgentHandle(name="bob", base_url="http://x", context_id="ctx")
    assert h.name == "bob"
    assert h.base_url == "http://x"
    assert h.context_id == "ctx"
    # 既有位置构造(base_url, context_id)仍合法,name 默认 ""
    h2 = MilkieAgentHandle("http://y", "c2")
    assert h2.name == ""
    assert (h2.base_url, h2.context_id) == ("http://y", "c2")


async def test_run_turn_yields_llm_progress_deltas():
    sse = _sse(
        ("agent.run.started", {"contextId": "c"}),
        ("message_delta", {"text": "Hello, "}),
        ("message_delta", {"text": "world!"}),
        ("agent.run.completed", {"status": "completed", "output": "Hello, world!"}),
    )
    p, client = _provider(sse)
    try:
        events = [e async for e in p.run_turn(MilkieAgentHandle("http://sidecar", "c"), "hi")]
    finally:
        await client.aclose()
    assert events == [
        {"_progress": [{"stage": "llm", "delta": "Hello, ", "answer": "", "id": "llm"}]},
        {"_progress": [{"stage": "llm", "delta": "world!", "answer": "", "id": "llm"}]},
    ]


async def test_run_turn_raises_on_error_terminal():
    """An ``agent.run.completed`` with status=error must raise (not be swallowed as
    an empty turn) — e.g. an LLM-endpoint connection error. The message carries the
    cause so retryable markers can drive a retry."""
    sse = _sse(
        ("agent.run.started", {"contextId": "c"}),
        ("agent.run.completed", {"status": "error", "output": "Connection error."}),
    )
    p, client = _provider(sse)
    try:
        with pytest.raises(RuntimeError, match="Connection error"):
            _ = [e async for e in p.run_turn(MilkieAgentHandle("http://sidecar", "c"), "hi")]
    finally:
        await client.aclose()


async def test_run_turn_raises_on_error_frame():
    """A milkie ``error`` SSE frame (thrown-exception path) must also surface."""
    sse = _sse(("error", {"message": "model overloaded"}))
    p, client = _provider(sse)
    try:
        with pytest.raises(RuntimeError, match="model overloaded"):
            _ = [e async for e in p.run_turn(MilkieAgentHandle("http://sidecar", "c"), "hi")]
    finally:
        await client.aclose()


async def test_run_turn_preserves_structured_error_and_waits_for_terminal_runid():
    envelope = {
        "code": "MODEL_CONNECTION_ERROR",
        "message": "Model provider connection failed.",
        "phase": "stream_open",
        "provider": "volcengine",
        "model": "glm-5.2",
        "retryable": True,
    }
    sse = _sse(
        ("error", {"message": envelope["message"], "error": envelope}),
        ("agent.run.completed", {
            "status": "error", "message": envelope["message"],
            "error": envelope, "runId": "run-structured",
        }),
    )
    p, client = _provider(sse)
    handle = MilkieAgentHandle("http://sidecar", "c")
    try:
        with pytest.raises(MilkieAgentError) as raised:
            _ = [e async for e in p.run_turn(handle, "hi")]
    finally:
        await client.aclose()

    assert raised.value.code == "MODEL_CONNECTION_ERROR"
    assert raised.value.retryable is True
    assert raised.value.phase == "stream_open"
    assert raised.value.provider == "volcengine"
    assert raised.value.model == "glm-5.2"
    assert raised.value.run_id == "run-structured"
    assert handle.last_run_id == "run-structured"


# ── #47: capture milkie's per-run id off the terminal frame (milkie#140) ──
# runId is milkie-private and must NOT enter the neutral _progress contract; it
# is stashed on the provider-internal handle so the Provider can later locate the
# recorded trace (`milkie trace <runId>`). Never surfaced to turn_orchestrator.

async def test_run_turn_captures_runid_onto_handle():
    """The completed terminal frame's runId is stashed on the handle (not _progress)."""
    sse = _sse(
        ("agent.run.started", {"contextId": "c"}),
        ("message_delta", {"text": "hi"}),
        ("agent.run.completed", {"status": "completed", "output": "hi", "runId": "run-abc"}),
    )
    p, client = _provider(sse)
    handle = MilkieAgentHandle("http://sidecar", "c")
    try:
        events = [e async for e in p.run_turn(handle, "hi")]
    finally:
        await client.aclose()
    assert handle.last_run_id == "run-abc"
    # runId must not leak into the neutral _progress contract
    assert "run-abc" not in json.dumps(events)


async def test_run_turn_captures_runid_even_on_error_terminal():
    """失败 run 也要留得到 runId(供 cron 失败分支留证):error 终止帧的 runId
    必须在抛 RuntimeError 前捕获到 handle。"""
    sse = _sse(
        ("agent.run.started", {"contextId": "c"}),
        ("agent.run.completed", {"status": "error", "output": "boom", "runId": "run-err"}),
    )
    p, client = _provider(sse)
    handle = MilkieAgentHandle("http://sidecar", "c")
    try:
        with pytest.raises(RuntimeError):
            _ = [e async for e in p.run_turn(handle, "hi")]
    finally:
        await client.aclose()
    assert handle.last_run_id == "run-err"


# ── #47: capture_trace — 中立能力,内部从 handle.last_run_id 取 runId,调带外 chokepoint ──

def test_capture_trace_returns_none_without_run_id(monkeypatch):
    """无 runId(从未跑过 / 非 milkie 路径)→ 返回 None,绝不调 chokepoint。"""
    called: list = []
    monkeypatch.setattr(provider_mod, "capture_trace_report", lambda *a, **k: called.append(1))
    p = MilkieProvider("http://x")
    h = MilkieAgentHandle("http://s", "c", name="alice")  # last_run_id 默认 None
    assert p.capture_trace(h) is None
    assert called == []


def test_capture_trace_invokes_chokepoint_with_runid_and_agent_data_dir(monkeypatch, tmp_path):
    """有 runId → 用 handle.last_run_id + 按 agent.name 解析的 sidecar data_dir 调 chokepoint。
    runId 不出 Provider —— 由 capture_trace 内部取用。"""
    captured: dict = {}

    def fake_report(run_id, *, traces_dir, data_dir, **kw):
        captured.update(run_id=run_id, traces_dir=traces_dir, data_dir=data_dir)
        return tmp_path / f"{run_id}.html"

    monkeypatch.setattr(provider_mod, "capture_trace_report", fake_report)
    monkeypatch.setattr(provider_mod, "_milkie_data_dir", lambda name: f"/data/{name}")
    monkeypatch.setattr(provider_mod, "_traces_dir", lambda: tmp_path)

    p = MilkieProvider("http://x")
    h = MilkieAgentHandle("http://s", "c", name="alice")
    h.last_run_id = "run-77"

    out = p.capture_trace(h)
    assert out == tmp_path / "run-77.html"
    assert captured["run_id"] == "run-77"
    assert captured["data_dir"] == "/data/alice"   # data_dir 由 agent.name 推出
    assert captured["traces_dir"] == tmp_path


async def test_run_turn_sends_contextid_and_input_to_chat():
    cap: dict = {}
    p, client = _provider(_sse(("agent.run.completed", {"status": "completed", "output": ""})), cap)
    try:
        _ = [e async for e in p.run_turn(MilkieAgentHandle("http://sidecar", "ctx-9"), "say hi")]
    finally:
        await client.aclose()
    assert cap["payload"]["contextId"] == "ctx-9"
    assert cap["payload"]["input"] == "say hi"
    assert cap["url"].endswith("/chat")


async def test_run_turn_yields_skill_progress_for_tools():
    sse = _sse(
        ("tool.requested", {"toolName": "t", "input": {"x": 1}, "toolCallId": "tc"}),
        ("tool.responded", {"toolName": "t", "toolCallId": "tc", "status": "ok", "output": "done"}),
        ("agent.run.completed", {"status": "completed", "output": "done"}),
    )
    p, client = _provider(sse)
    try:
        events = [e async for e in p.run_turn(MilkieAgentHandle("http://sidecar", "c"), "go")]
    finally:
        await client.aclose()
    items = [e["_progress"][0] for e in events]
    assert [it["stage"] for it in items] == ["skill", "skill"]
    assert items[0]["status"] == "running"
    assert items[1]["status"] == "completed"
    assert items[0]["id"] == items[1]["id"] == "tc"


# ---------------------------------------------------------------------------
# #43:handle 不再冻结 base_url —— 每次调用经 pool 按 agent 名解析;run_turn 持 lease
# ---------------------------------------------------------------------------


class _FreshPool:
    """fake pool:peek/lease 都解析到"新"sidecar(模拟重生后端口已变)。"""

    def __init__(self, base_url: str = "http://fresh-sidecar"):
        self._sidecar = type("_S", (), {"base_url": base_url})()
        self.lease_active = 0
        self.lease_names: list = []
        self.peek_names: list = []

    def peek(self, name):
        self.peek_names.append(name)
        return self._sidecar

    def lease(self, name):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _cm():
            self.lease_names.append(name)
            self.lease_active += 1
            try:
                yield self._sidecar
            finally:
                self.lease_active -= 1

        return _cm()


def _provider_with_pool(sse_text: str, pool, capture: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture["url"] = str(request.url)
            capture["payload"] = json.loads(request.content)
            capture["lease_active_during_request"] = pool.lease_active
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse_text.encode("utf-8")
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sync_client = httpx.Client(transport=httpx.MockTransport(handler))
    p = MilkieProvider("http://cfg", client=client, sync_client=sync_client, pool=pool)
    return p, client, sync_client


async def test_run_turn_resolves_base_url_via_pool_and_holds_lease():
    """重生后 handle.base_url 已僵尸 → run_turn 必须经 pool 解析新地址,且流式期间持 lease。"""
    pool = _FreshPool()
    cap: dict = {}
    p, client, sc = _provider_with_pool(
        _sse(("agent.run.completed", {"status": "completed", "output": "ok"})), pool, cap
    )
    try:
        h = MilkieAgentHandle("http://stale-dead-port", "ctx", name="alice")
        _ = [e async for e in p.run_turn(h, "hi")]
    finally:
        await client.aclose()
        sc.close()
    assert cap["url"].startswith("http://fresh-sidecar")     # 不打僵尸端口
    assert pool.lease_names == ["alice"]
    assert cap["lease_active_during_request"] == 1           # 流式期间在飞登记中
    assert pool.lease_active == 0                            # 结束后释放


async def test_run_turn_releases_lease_on_error():
    pool = _FreshPool()
    p, client, sc = _provider_with_pool(
        _sse(("error", {"message": "boom"})), pool
    )
    try:
        h = MilkieAgentHandle("http://stale", "ctx", name="alice")
        with pytest.raises(RuntimeError, match="boom"):
            _ = [e async for e in p.run_turn(h, "hi")]
    finally:
        await client.aclose()
        sc.close()
    assert pool.lease_active == 0


async def test_run_turn_without_pool_falls_back_to_handle_base_url():
    """注入式构造(测试/无 pool)→ 沿用 handle.base_url,行为同旧版。"""
    cap: dict = {}
    p, client = _provider(
        _sse(("agent.run.completed", {"status": "completed", "output": "ok"})), cap
    )
    try:
        _ = [e async for e in p.run_turn(MilkieAgentHandle("http://sidecar", "c"), "hi")]
    finally:
        await client.aclose()
    assert cap["url"].startswith("http://sidecar")


async def test_resume_resolves_base_url_via_pool_and_holds_lease():
    """resume 同样是流式 turn(中断续跑)—— 须经 pool 解析地址并全程持 lease,
    防止续跑中途被技能重生腰斩。"""
    pool = _FreshPool()
    cap: dict = {}
    p, client, sc = _provider_with_pool(
        _sse(("agent.run.completed", {"status": "completed", "output": "ok"})), pool, cap
    )
    try:
        h = MilkieAgentHandle("http://stale-dead-port", "ctx", name="alice")
        await p.resume(h, "continue")
    finally:
        await client.aclose()
        sc.close()
    assert cap["url"] == "http://fresh-sidecar/resume"
    assert cap["lease_active_during_request"] == 1
    assert pool.lease_active == 0


def test_sync_methods_resolve_base_url_via_pool_peek():
    """set/get_variable 等 sync 方法经 pool.peek 解析当前 sidecar 地址。"""
    pool = _FreshPool()
    cap: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cap["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True, "value": "v", "paused": False})

    sc = httpx.Client(transport=httpx.MockTransport(handler))
    p = MilkieProvider("http://cfg", sync_client=sc, pool=pool)
    h = MilkieAgentHandle("http://stale-dead-port", "c1", name="alice")
    try:
        p.set_variable(h, "k", "v")
        assert cap["url"] == "http://fresh-sidecar/context/set"
        p.get_variable(h, "k")
        assert cap["url"] == "http://fresh-sidecar/context/get"
        p.is_user_interrupt_paused(h)
        assert cap["url"] == "http://fresh-sidecar/context/state"
        p.export_session(h)
        assert cap["url"] == "http://fresh-sidecar/session/history"
    finally:
        sc.close()


async def test_async_methods_resolve_base_url_via_pool_peek():
    """interrupt / attach_projection 同样经 pool 解析,不打僵尸端口。"""
    pool = _FreshPool()
    urls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    p = MilkieProvider("http://cfg", client=client, pool=pool)
    h = MilkieAgentHandle("http://stale", "c1", name="alice")
    try:
        await p.interrupt(h)
        await p.attach_projection(h, source_run_id="r1", display_text="t")
    finally:
        await client.aclose()
    assert urls == [
        "http://fresh-sidecar/interrupt",
        "http://fresh-sidecar/projection/attach",
    ]


def test_base_url_falls_back_when_pool_peek_misses():
    """pool 里没有该 agent(尚未 spawn / 名字为空)→ 回落 handle.base_url。"""

    class _EmptyPool:
        def peek(self, name):
            return None

    cap: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cap["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True})

    sc = httpx.Client(transport=httpx.MockTransport(handler))
    p = MilkieProvider("http://cfg", sync_client=sc, pool=_EmptyPool())
    try:
        p.set_variable(MilkieAgentHandle("http://handle-url", "c", name="alice"), "k", "v")
        assert cap["url"] == "http://handle-url/context/set"
        # name 为空(旧式 2-arg 构造)→ 同样回落
        p.set_variable(MilkieAgentHandle("http://handle-url2", "c"), "k", "v")
        assert cap["url"] == "http://handle-url2/context/set"
    finally:
        sc.close()


# ---------------------------------------------------------------------------
# #43:技能指纹 —— pool freshness 检查的输入(指纹变 ⇔ prompt 技能段/manifest 会变)
# ---------------------------------------------------------------------------


def _make_skill(root, name: str, desc: str, body: str = ""):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"# {name}\n\n{desc}\n{body}", encoding="utf-8")
    return d


def _fingerprint_env(tmp_path, monkeypatch):
    """隔离的 workspace + 技能目录,返回技能根目录。"""
    from src.everbot.core.agent.provider.milkie import skills as msk

    ws = tmp_path / "ws"
    skills_root = ws / "skills"
    skills_root.mkdir(parents=True)
    monkeypatch.setattr(provider_mod, "_resolve_agent_workspace", lambda name: ws)
    monkeypatch.setattr(msk, "resolve_skill_dirs", lambda _ws: [skills_root])
    monkeypatch.setattr(provider_mod, "_agent_skill_filter", lambda name: (None, None))
    return skills_root


def test_skills_fingerprint_reflector_returns_none():
    assert provider_mod._skills_fingerprint(provider_mod.REFLECTOR_AGENT) is None


def test_skills_fingerprint_deterministic(tmp_path, monkeypatch):
    root = _fingerprint_env(tmp_path, monkeypatch)
    _make_skill(root, "alpha", "does a thing")
    fp1 = provider_mod._skills_fingerprint("alice")
    fp2 = provider_mod._skills_fingerprint("alice")
    assert isinstance(fp1, str) and len(fp1) == 64  # sha256 hex
    assert fp1 == fp2


def test_skills_fingerprint_changes_on_skill_added(tmp_path, monkeypatch):
    root = _fingerprint_env(tmp_path, monkeypatch)
    _make_skill(root, "alpha", "does a thing")
    fp1 = provider_mod._skills_fingerprint("alice")
    _make_skill(root, "beta", "another thing")
    fp2 = provider_mod._skills_fingerprint("alice")
    assert fp1 != fp2


def test_skills_fingerprint_changes_on_description_change(tmp_path, monkeypatch):
    root = _fingerprint_env(tmp_path, monkeypatch)
    _make_skill(root, "alpha", "does a thing")
    fp1 = provider_mod._skills_fingerprint("alice")
    _make_skill(root, "alpha", "does a BETTER thing")
    fp2 = provider_mod._skills_fingerprint("alice")
    assert fp1 != fp2


def test_skills_fingerprint_stable_on_body_only_change(tmp_path, monkeypatch):
    """prompt 技能段只用 name/title/description/abs_path;改正文(首段之外)不应触发重生
    —— agent 运行时本就经 run_command 从磁盘读全文。"""
    root = _fingerprint_env(tmp_path, monkeypatch)
    _make_skill(root, "alpha", "does a thing", body="\n## usage\nold steps\n")
    fp1 = provider_mod._skills_fingerprint("alice")
    _make_skill(root, "alpha", "does a thing", body="\n## usage\nNEW steps entirely\n")
    fp2 = provider_mod._skills_fingerprint("alice")
    assert fp1 == fp2


def test_skills_fingerprint_respects_include_filter(tmp_path, monkeypatch):
    """include/exclude 过滤后的列表才是 prompt 的来源 → 指纹必须基于过滤后结果。"""
    root = _fingerprint_env(tmp_path, monkeypatch)
    _make_skill(root, "alpha", "a")
    _make_skill(root, "beta", "b")
    fp_all = provider_mod._skills_fingerprint("alice")
    monkeypatch.setattr(provider_mod, "_agent_skill_filter", lambda name: (["alpha"], None))
    fp_filtered = provider_mod._skills_fingerprint("alice")
    assert fp_all != fp_filtered


def test_pool_fingerprint_gate_default_loader_vs_injected():
    """默认 loader → pool 接技能指纹;注入式 loader(测试 seam,prompt 不来自
    discover_skills)→ None,不参与 freshness 检查。"""
    p_default = MilkieProvider("http://x")
    assert p_default._pool_fingerprint() is provider_mod._skills_fingerprint

    p_injected = MilkieProvider("http://x", system_prompt_loader=lambda name: "static")
    assert p_injected._pool_fingerprint() is None


async def test_self_built_client_disables_env_proxy():
    client = MilkieProvider("http://x")._new_client()
    try:
        assert client.trust_env is False
    finally:
        await client.aclose()


def test_set_session_id_aligns_context_id_for_cross_restart_continuity():
    """#34:milkie 会话历史按 contextId 存于 serve sqlite;contextId 必须绑定到稳定的
    session_id,否则每次 daemon 重启生成新随机 contextId → 历史接不上。set_session_id
    把 handle.context_id 对齐到 session_id(同会话跨重启续历史)。"""
    p = MilkieProvider("http://x")
    h = MilkieAgentHandle("http://sidecar", "demo_agent-rand1234", name="demo_agent")
    p.set_session_id(h, "demo_agent__primary__chat")
    assert h.context_id == "demo_agent__primary__chat"  # 绑定稳定 session_id


def test_safe_noop_methods_do_not_crash():
    """milkie 自带机制的接口:no-op,不崩(turn 层可用的前提)。"""
    p = MilkieProvider("http://x")
    h = MilkieAgentHandle("http://x", "c")
    p.init_trajectory(h, "/t", overwrite=True)
    p.finalize_trajectory_on_error(h)
    assert p.is_paused(h) is False
    assert p.is_error(h) is False
    assert p.has_skill(h, "x") is False


def test_set_variable_posts_to_context_set_endpoint():
    cap: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cap["url"] = str(request.url)
        cap["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        p = MilkieProvider("http://x", sync_client=client)
        p.set_variable(MilkieAgentHandle("http://sidecar", "c1"), "model_name", "claude")
    finally:
        client.close()
    assert cap["url"].endswith("/context/set")
    assert cap["body"] == {"contextId": "c1", "name": "model_name", "value": "claude"}


def test_get_variable_reads_from_context_get_endpoint():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": "claude"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        p = MilkieProvider("http://x", sync_client=client)
        val = p.get_variable(MilkieAgentHandle("http://sidecar", "c1"), "model_name")
    finally:
        client.close()
    assert val == "claude"


def test_is_user_interrupt_paused_queries_context_state():
    """milkie#137:is_user_interrupt_paused 经 serve /context/state 查 paused。"""
    cap: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cap["url"] = str(request.url)
        cap["body"] = json.loads(request.content)
        return httpx.Response(200, json={"contextId": "c1", "exists": True, "paused": True, "resumable": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        p = MilkieProvider("http://x", sync_client=client)
        paused = p.is_user_interrupt_paused(MilkieAgentHandle("http://sidecar", "c1"))
    finally:
        client.close()
    assert paused is True
    assert cap["url"].endswith("/context/state")
    assert cap["body"] == {"contextId": "c1"}


def test_is_user_interrupt_paused_false_when_not_paused():
    """completed/running 的 context → paused False(resume gate 不触发)。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"contextId": "c1", "exists": True, "paused": False, "resumable": False})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        p = MilkieProvider("http://x", sync_client=client)
        paused = p.is_user_interrupt_paused(MilkieAgentHandle("http://sidecar", "c1"))
    finally:
        client.close()
    assert paused is False


async def test_register_skillkit_is_graceful_noop():
    """#38 telegram 原生化:register_skillkit 不再 NotImplementedError 阻断 telegram agent;
    文件发送改由 channel 输出约定提供,故此处为优雅 no-op(不崩、不抛)。合并 #34:
    register_skillkit 在 milkie 下不再抛 NotImplementedError。"""
    p = MilkieProvider("http://x")
    h = MilkieAgentHandle("http://x", "c")

    class _Kit:
        def getName(self):
            return "telegram_channel"

    # 不抛异常即通过
    p.register_skillkit(h, _Kit())


def _llm_provider(handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return MilkieProvider("http://x", client=client), client


async def test_call_llm_posts_canonical_request_and_returns_output():
    """prompt → POST /llm,canonical Message[] + 默认 tier/temperature;返回 strip 后 output。"""
    cap: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cap["url"] = str(request.url)
        cap["body"] = json.loads(request.content)
        return httpx.Response(200, json={"output": "  summary text  "})

    p, client = _llm_provider(handler)
    try:
        out = await p.call_llm(None, "compress this")
    finally:
        await client.aclose()
    assert cap["url"].endswith("/llm")
    assert cap["body"]["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "compress this"}]}
    ]
    assert cap["body"]["tier"] == "default"
    assert cap["body"]["temperature"] == 0.3
    assert out == "summary text"


async def test_call_llm_fast_selects_fast_tier_and_temperature_passes():
    """fast=True → tier='fast'(命中 serve 的便宜快档);temperature 透传。"""
    cap: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cap["body"] = json.loads(request.content)
        return httpx.Response(200, json={"output": "ok"})

    p, client = _llm_provider(handler)
    try:
        await p.call_llm(None, "p", temperature=0.1, fast=True)
    finally:
        await client.aclose()
    assert cap["body"]["tier"] == "fast"
    assert cap["body"]["temperature"] == 0.1


async def test_call_llm_raises_on_error_when_serve_errors():
    """raise_on_error=True(默认):serve 非200 → RuntimeError(含错误信息),不静默吞。"""
    import pytest

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "gateway boom"})

    p, client = _llm_provider(handler)
    try:
        with pytest.raises(RuntimeError, match="gateway boom"):
            await p.call_llm(None, "p")
    finally:
        await client.aclose()


async def test_call_llm_returns_error_text_when_raise_disabled():
    """raise_on_error=False(compressor 语义):serve 非200 → 返回错误串当结果,不抛。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "gateway boom"})

    p, client = _llm_provider(handler)
    try:
        out = await p.call_llm(None, "p", raise_on_error=False)
    finally:
        await client.aclose()
    assert "gateway boom" in out


def test_export_session_reads_history_and_translates_to_alfred_format():
    """export_session 走 /session/history(#128)→ canonical Message[] 翻成 alfred
    history 格式:assistant tool_use→tool_calls、tool→tool_call_id、content 数组→字符串。"""
    canonical = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": [
            {"type": "text", "text": "let me check"},
            {"type": "tool_use", "id": "call_1", "name": "search", "input": {"q": "x"}},
        ]},
        {"role": "tool", "content": [
            {"type": "tool_result", "tool_use_id": "call_1", "content": "result text"},
        ]},
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    ]
    cap: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cap["url"] = str(request.url)
        cap["body"] = json.loads(request.content)
        return httpx.Response(200, json={"messages": canonical})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        p = MilkieProvider("http://x", sync_client=client)
        out = p.export_session(MilkieAgentHandle("http://sidecar", "c1"))
    finally:
        client.close()
    assert cap["url"].endswith("/session/history")
    assert cap["body"] == {"contextId": "c1"}
    assert out["history_messages"] == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "let me check", "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "search", "arguments": '{"q": "x"}'}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "result text"},
        {"role": "assistant", "content": "done"},
    ]
    assert out["variables"] == {}


def test_export_session_applies_history_hygiene_orphan_and_empty():
    """A4 端到端:export_session 走 /session/history → 翻译 → **数据卫生**。
    含中断轮的空 assistant + orphan tool(无配对 tool_use)的 milkie 历史,
    export 出的 history_messages 必须已清洗(空 assistant 剔除、orphan 不残留)。"""
    canonical = [
        {"role": "user", "content": [{"type": "text", "text": "do x"}]},
        {"role": "assistant", "content": []},  # 空 assistant(中断轮)
        {"role": "tool", "content": [
            {"type": "tool_result", "tool_use_id": "ghost", "content": "orphan output"},
        ]},  # orphan:无配对 assistant tool_use
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"messages": canonical})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        p = MilkieProvider("http://x", sync_client=client)
        hist = p.export_session(MilkieAgentHandle("http://sidecar", "c1"))["history_messages"]
    finally:
        client.close()

    # 空 assistant 被剔除
    assert not any(m["role"] == "assistant" and not (m.get("content") or "").strip()
                   and not m.get("tool_calls") for m in hist)
    # orphan tool 不残留
    assert not any(m["role"] == "tool" and m.get("tool_call_id") == "ghost" for m in hist)
    # orphan 内容并入 user 上下文(不丢)
    assert any(m["role"] == "user" and "orphan output" in (m.get("content") or "") for m in hist)


async def test_interrupt_posts_to_interrupt_endpoint():
    """MilkieProvider.interrupt 经 serve /interrupt 端点(contextId)跨进程发信号。"""
    cap: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cap["url"] = str(request.url)
        cap["body"] = json.loads(request.content)
        return httpx.Response(200, json={"contextId": "c1", "signaled": True})

    p, client = _llm_provider(handler)
    try:
        await p.interrupt(MilkieAgentHandle("http://sidecar", "c1"))
    finally:
        await client.aclose()
    assert cap["url"].endswith("/interrupt")
    assert cap["body"] == {"contextId": "c1"}


async def test_run_turn_raises_on_chat_server_error():
    """/chat 返回 500 → 迭代 run_turn 必须抛 RuntimeError(含状态码+body 片段),
    否则无事件 → core_service 显示「(无响应)」,错误被静默吞。"""
    import pytest

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal boom")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    p = MilkieProvider("http://sidecar", client=client)
    try:
        with pytest.raises(RuntimeError, match="500"):
            async for _ in p.run_turn(MilkieAgentHandle("http://sidecar", "c"), "hi"):
                pass
    finally:
        await client.aclose()


async def test_interrupt_raises_on_server_error():
    """/interrupt 非2xx → interrupt() 必须抛错,不静默吞。"""
    import pytest

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "no such context"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    p = MilkieProvider("http://x", client=client)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await p.interrupt(MilkieAgentHandle("http://sidecar", "c1"))
    finally:
        await client.aclose()


def test_set_variable_raises_on_server_error():
    """/context/set 非2xx → set_variable() 必须抛错。"""
    import pytest

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        p = MilkieProvider("http://x", sync_client=client)
        with pytest.raises(httpx.HTTPStatusError):
            p.set_variable(MilkieAgentHandle("http://sidecar", "c1"), "k", "v")
    finally:
        client.close()


def test_get_variable_raises_on_server_error():
    """/context/get 非2xx → get_variable() 必须抛错(而非静默返回 None)。"""
    import pytest

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        p = MilkieProvider("http://x", sync_client=client)
        with pytest.raises(httpx.HTTPStatusError):
            p.get_variable(MilkieAgentHandle("http://sidecar", "c1"), "k")
    finally:
        client.close()


def test_milkie_does_not_need_history_restore():
    """milkie serve 用 sqlite/jsonl 自持久化(milkie#130),同 contextId 重启自动从
    checkpoint 恢复 → alfred 不需灌回历史。"""
    assert MilkieProvider("http://x").needs_history_restore() is False


def test_export_session_empty_on_no_session():
    """无该 context(serve 404)→ 返回空历史,不抛(新会话场景)。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": 'No session for contextId "c"'})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        p = MilkieProvider("http://x", sync_client=client)
        out = p.export_session(MilkieAgentHandle("http://sidecar", "c"))
    finally:
        client.close()
    assert out == {"history_messages": [], "variables": {}}


class _FakeSidecarStub:
    def __init__(self):
        self.closed = 0
    @property
    def base_url(self):
        return "http://127.0.0.1:19999"
    async def close(self):
        self.closed += 1


async def test_create_agent_uses_pool_base_url(monkeypatch):
    stub = _FakeSidecarStub()

    class _FakePool:
        async def get_or_spawn(self, name):
            return stub
        async def shutdown_all(self):
            await stub.close()

    prov = MilkieProvider.__new__(MilkieProvider)   # 跳过 __init__ 的 config 读取
    prov._base_url = None
    prov._client = None
    prov._sync_client = None
    prov._pool = _FakePool()
    prov._system_prompt_loader = lambda name: "sys"

    handle = await prov.create_agent("alice", workspace_path="/tmp")
    assert isinstance(handle, MilkieAgentHandle)
    assert handle.base_url == "http://127.0.0.1:19999"
    assert handle.context_id.startswith("alice-")

    await prov.shutdown_sidecars()
    assert stub.closed == 1


async def test_call_llm_fails_loud_when_base_url_is_none():
    """per-agent pool 模式下 provider 无固定 base_url(get_provider_for_agent
    建出的 provider base_url=None)。call_llm 必须 fail-loud,而非静默 POST
    到死端口。"""
    prov = MilkieProvider.__new__(MilkieProvider)
    prov._base_url = None
    prov._client = None
    with pytest.raises(RuntimeError, match="base_url"):
        await prov.call_llm(None, "anything")


def test_construction_does_no_config_io(monkeypatch):
    """构造 MilkieProvider 绝不触发 config/factory I/O:pool 惰性构建。
    monkeypatch _build_pool 让其一旦被调用就炸 → 构造不抛即证明 __init__ 未建 pool。"""
    import src.everbot.core.agent.provider.milkie.provider as mod

    def _boom():
        raise AssertionError("config/factory I/O during construction")

    monkeypatch.setattr(mod.MilkieProvider, "_build_pool", staticmethod(_boom))
    p = mod.MilkieProvider("http://x")  # must NOT raise
    assert p is not None
    assert p._pool is None  # 仍未装配


async def test_pool_built_lazily_on_first_create_agent(monkeypatch):
    """pool 首次 create_agent 时才装配,且只建一次(复用)。"""
    import src.everbot.core.agent.provider.milkie.provider as mod

    calls = {"n": 0}
    stub = _FakeSidecarStub()

    class _FakePool:
        async def get_or_spawn(self, name):
            return stub

    def _fake_build():
        calls["n"] += 1
        return _FakePool()

    monkeypatch.setattr(mod.MilkieProvider, "_build_pool", staticmethod(_fake_build))
    p = mod.MilkieProvider("http://x")
    assert calls["n"] == 0  # 构造未建
    await p.create_agent("a", "/ws")
    assert calls["n"] == 1  # 首次 create_agent 才建
    await p.create_agent("b", "/ws")
    assert calls["n"] == 1  # 复用,不重建


async def test_shutdown_sidecars_noop_when_pool_never_built(monkeypatch):
    """从未 spawn → shutdown_sidecars 不为关停而强行建 pool(no-op)。"""
    import src.everbot.core.agent.provider.milkie.provider as mod

    def _boom():
        raise AssertionError("should not build pool just to shut down")

    monkeypatch.setattr(mod.MilkieProvider, "_build_pool", staticmethod(_boom))
    p = mod.MilkieProvider("http://x")
    await p.shutdown_sidecars()  # must NOT raise
    assert p._pool is None


def test_injected_system_prompt_loader_is_used(monkeypatch):
    """注入的 system_prompt_loader 必须真正流到 launcher.build 的 system_prompt;
    且模块级默认 loader 绝不被调用(回归 dead-seam:_build_pool 曾硬编码默认 loader)。"""
    from pathlib import Path

    import src.everbot.core.agent.provider.milkie.provider as mod
    from src.everbot.core.agent.provider.milkie.launcher import LaunchSpec

    captured_prompts: list = []
    captured_skills: list = []

    class _CapturingLauncher:
        def build(self, agent_name, *, system_prompt, skills=None, default_model=None, agent_workspace=None, sandbox_enabled=None):
            captured_prompts.append(system_prompt)
            captured_skills.append(skills)
            return LaunchSpec(
                cmd=["node"], env={}, data_dir=Path("/tmp"), agent_md=Path("/tmp/a.md")
            )

    # _build_pool 走 `from .launcher import SidecarLauncher`,故 patch 源模块属性
    monkeypatch.setattr(
        "src.everbot.core.agent.provider.milkie.launcher.SidecarLauncher",
        lambda **kw: _CapturingLauncher(),
    )
    # config 读取桩成无害:让 _build_pool 能跑到 _build 闭包。
    monkeypatch.setattr(
        "src.everbot.infra.config.get_config", lambda *a, **k: {}
    )
    # #38:_build_pool 现经 model_config.load_model_config 读模型路由(非 dolphin factory),
    # 走真实 config/dolphin.yaml 即可;_CapturingLauncher 忽略 llms/clouds,无害。

    # 模块级默认 loader 一旦被调用就炸 → 证明注入版真正接通(而非静默走默认)
    def _default_must_not_be_called(agent_name):
        raise AssertionError("module-level _default_system_prompt_loader must NOT be called")

    monkeypatch.setattr(mod, "_default_system_prompt_loader", _default_must_not_be_called)

    prov = mod.MilkieProvider(system_prompt_loader=lambda name: f"PROMPT::{name}")
    pool = prov._get_pool()
    # 触发 build 闭包(pool 把闭包存为 self._build)
    cmd, env = pool._build("alice")
    assert captured_prompts == ["PROMPT::alice"]
    # 注入式 loader 绕过发现 → skills=None → 不产出 manifest(milkie#139)
    assert captured_skills == [None]
    assert cmd == ["node"]


def test_default_loader_feeds_discovered_skills_to_launcher(monkeypatch):
    """默认 loader 路径:_build 跑一次 discover_skills,把 skills 喂给 launcher(同源 manifest)。"""
    from pathlib import Path

    import src.everbot.core.agent.provider.milkie.provider as mod
    from src.everbot.core.agent.provider.milkie.launcher import LaunchSpec

    captured = {}

    class _CapturingLauncher:
        def build(self, agent_name, *, system_prompt, skills=None, default_model=None, agent_workspace=None, sandbox_enabled=None):
            captured["system_prompt"] = system_prompt
            captured["skills"] = skills
            return LaunchSpec(
                cmd=["node"], env={}, data_dir=Path("/tmp"), agent_md=Path("/tmp/a.md")
            )

    monkeypatch.setattr(
        "src.everbot.core.agent.provider.milkie.launcher.SidecarLauncher",
        lambda **kw: _CapturingLauncher(),
    )
    monkeypatch.setattr("src.everbot.infra.config.get_config", lambda *a, **k: {})

    # 让默认 loader 返回确定的 (prompt, skills),验证它确实被 _build 调用并透传
    fake_skills = [{"name": "twitter-watch", "description": "d", "abs_path": "/abs/tw"}]
    monkeypatch.setattr(
        mod, "_build_default_prompt_and_skills",
        lambda name: (f"PROMPT::{name}", fake_skills),
    )

    # 不注入 loader → 用模块默认 loader → 走"同源 discover"分支
    prov = mod.MilkieProvider("http://x")
    cmd, env = prov._get_pool()._build("alice")
    assert captured["system_prompt"] == "PROMPT::alice"
    assert captured["skills"] == fake_skills  # 默认路径把 skills 喂到 launcher


async def test_skill_change_respawn_chain_other_sessions_not_orphaned():
    """全链路(真实 SidecarPool + fake sidecar):技能指纹变化 → 下一轮触发重生;
    **另一个 session 的旧 handle** 后续调用自动解析到新 sidecar(验收 1/5:
    免重启生效、重生后无僵尸 handle)。"""
    from src.everbot.core.agent.provider.milkie.pool import SidecarPool

    class _FakeSidecar:
        seq = 0

        def __init__(self):
            _FakeSidecar.seq += 1
            self.url = f"http://sidecar-v{_FakeSidecar.seq}"
            self.closed = 0

        @property
        def base_url(self):
            return self.url

        async def start(self):
            pass

        async def close(self):
            self.closed += 1

    fp = {"v": "skills-v1"}
    pool = SidecarPool(
        build=lambda name: ([], {}),
        sidecar_factory=lambda cmd, env: _FakeSidecar(),
        fingerprint=lambda name: fp["v"],
    )

    hit_urls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        hit_urls.append(f"{request.url.scheme}://{request.url.host}")
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(("agent.run.completed", {"status": "completed", "output": "ok"})).encode(),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    p = MilkieProvider(client=client, pool=pool)
    try:
        h_session1 = await p.create_agent("alice", "/ws")
        h_session2 = await p.create_agent("alice", "/ws")   # 同 agent、另一 session

        _ = [e async for e in p.run_turn(h_session1, "hi")]
        assert hit_urls[-1] == "http://sidecar-v1"

        fp["v"] = "skills-v2"                               # 技能变化(装/改技能)
        _ = [e async for e in p.run_turn(h_session2, "hi")]  # 下一轮 → 触发重生
        assert hit_urls[-1] == "http://sidecar-v2"

        # 关键:session1 的旧 handle(构造时拿的是 v1 地址)不僵尸,自动跟到 v2
        _ = [e async for e in p.run_turn(h_session1, "hi")]
        assert hit_urls[-1] == "http://sidecar-v2"
    finally:
        await client.aclose()


def test_build_pool_wires_skills_fingerprint(monkeypatch):
    """#43 接线:默认 loader 装配的 pool 必须带技能指纹(freshness 检查生效);
    注入式 loader → 不带(prompt 与 discover_skills 脱钩,检查无意义)。"""
    from pathlib import Path

    import src.everbot.core.agent.provider.milkie.provider as mod
    from src.everbot.core.agent.provider.milkie.launcher import LaunchSpec

    class _StubLauncher:
        def build(self, agent_name, *, system_prompt, skills=None, default_model=None, agent_workspace=None, sandbox_enabled=None):
            return LaunchSpec(
                cmd=["node"], env={}, data_dir=Path("/tmp"), agent_md=Path("/tmp/a.md")
            )

    monkeypatch.setattr(
        "src.everbot.core.agent.provider.milkie.launcher.SidecarLauncher",
        lambda **kw: _StubLauncher(),
    )
    monkeypatch.setattr("src.everbot.infra.config.get_config", lambda *a, **k: {})

    pool_default = mod.MilkieProvider("http://x")._get_pool()
    assert pool_default._fingerprint is mod._skills_fingerprint

    pool_injected = mod.MilkieProvider(
        "http://x", system_prompt_loader=lambda name: "static"
    )._get_pool()
    assert pool_injected._fingerprint is None


# ---------------------------------------------------------------------------
# #57: capture_trace 走 node+dist(而非字面 milkie)
# ---------------------------------------------------------------------------
def test_milkie_cli_cmd_defaults(monkeypatch):
    monkeypatch.setattr("src.everbot.infra.config.get_config", lambda *a, **k: {})
    node_bin, dist = provider_mod._milkie_cli_cmd()
    assert node_bin == "node"
    assert dist.endswith("milkie/dist/cli/index.js")


def test_milkie_cli_cmd_reads_config(monkeypatch):
    monkeypatch.setattr(
        "src.everbot.infra.config.get_config",
        lambda *a, **k: {"everbot": {"milkie": {"node_bin": "/usr/bin/node18", "dist_path": "/opt/milkie/x.js"}}},
    )
    node_bin, dist = provider_mod._milkie_cli_cmd()
    assert node_bin == "/usr/bin/node18"
    assert dist == "/opt/milkie/x.js"


def test_capture_trace_forwards_node_dist_cmd(monkeypatch, tmp_path):
    """capture_trace 必须把 milkie_cmd=(node, dist) 转发给 chokepoint,而非默认字面 milkie。"""
    monkeypatch.setattr("src.everbot.infra.config.get_config", lambda *a, **k: {})
    captured: dict = {}

    def _fake_report(run_id, *, traces_dir, data_dir, milkie_cmd=("milkie",), **kw):
        captured.update(run_id=run_id, milkie_cmd=tuple(milkie_cmd), data_dir=data_dir)
        return tmp_path / f"{run_id}.html"

    monkeypatch.setattr(provider_mod, "capture_trace_report", _fake_report)

    agent = MilkieAgentHandle(base_url="http://x", context_id="c", name="demo_agent")
    agent.last_run_id = "run-123"
    out = MilkieProvider("http://x").capture_trace(agent)

    assert out == tmp_path / "run-123.html"
    assert captured["run_id"] == "run-123"
    # 关键:前缀是 node + dist,不是字面 milkie
    assert captured["milkie_cmd"][0] == "node"
    assert captured["milkie_cmd"][1].endswith("milkie/dist/cli/index.js")
    assert "milkie" != captured["milkie_cmd"][0]


def test_capture_trace_none_without_run_id(monkeypatch):
    """无 last_run_id(非 milkie 路径/未跑过)→ None,不调 chokepoint。"""
    def _boom(*a, **k):
        raise AssertionError("不应调用 capture_trace_report")

    monkeypatch.setattr(provider_mod, "capture_trace_report", _boom)
    agent = MilkieAgentHandle(base_url="http://x", context_id="c", name="a")  # last_run_id 默认 None
    assert MilkieProvider("http://x").capture_trace(agent) is None


# ============================================================================
# #60 / milkie#146:把投递到 channel 的外部产出登记为 context projection
# (读侧、不进 history)。attach_projection 走 serve POST /projection/attach。
# ============================================================================

async def test_attach_projection_posts_to_projection_attach_endpoint():
    """attach_projection 以 channel 的 contextId 为 target、job 的 milkie runId
    为 sourceRunId,把 displayText POST 到 /projection/attach。"""
    capture: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        capture["url"] = str(request.url)
        capture["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"projection": {"sourceRunId": "job-run-1"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = MilkieProvider("http://sidecar", client=client)
    handle = MilkieAgentHandle("http://sidecar", "tg_session_demo_agent__123")

    await provider.attach_projection(
        handle,
        source_run_id="job-run-1",
        display_text="今日 $SIVE 推文分析…",
        delivered_at="2026-06-06T02:02:00Z",
    )

    assert capture["url"] == "http://sidecar/projection/attach"
    assert capture["payload"]["contextId"] == "tg_session_demo_agent__123"
    assert capture["payload"]["sourceRunId"] == "job-run-1"
    assert capture["payload"]["displayText"].startswith("今日 $SIVE")
    assert capture["payload"]["deliveredAt"] == "2026-06-06T02:02:00Z"


async def test_attach_projection_raises_on_non_2xx():
    """serve 非 2xx 不静默吞 —— 让调用方(带外 best-effort)能记日志。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = MilkieProvider("http://sidecar", client=client)
    handle = MilkieAgentHandle("http://sidecar", "ctx")

    with pytest.raises(httpx.HTTPStatusError):
        await provider.attach_projection(handle, source_run_id="r", display_text="x")


# ── per-agent sidecar 沙箱解析(#112)──────────────────────────────────────────
from src.everbot.infra import config as _cfgmod


def _set_cfg(monkeypatch, d):
    monkeypatch.setattr(_cfgmod, "get_config", lambda *a, **k: d)


def test_agent_sandbox_global_on_agent_override_off(monkeypatch):
    _set_cfg(monkeypatch, {"everbot": {
        "security": {"sidecar_sandbox": True},
        "agents": {"a": {"security": {"sidecar_sandbox": False}}}}})
    assert provider_mod._agent_sandbox_enabled("a") is False


def test_agent_sandbox_global_off_agent_override_on(monkeypatch):
    _set_cfg(monkeypatch, {"everbot": {
        "security": {"sidecar_sandbox": False},
        "agents": {"a": {"security": {"sidecar_sandbox": True}}}}})
    assert provider_mod._agent_sandbox_enabled("a") is True


def test_agent_sandbox_no_override_follows_global(monkeypatch):
    _set_cfg(monkeypatch, {"everbot": {
        "security": {"sidecar_sandbox": True},
        "agents": {"a": {}}}})
    assert provider_mod._agent_sandbox_enabled("a") is True


def test_agent_sandbox_defaults_false_when_unset(monkeypatch):
    _set_cfg(monkeypatch, {"everbot": {}})
    assert provider_mod._agent_sandbox_enabled("a") is False
