"""E2E:turn_orchestrator + MilkieProvider(真 milkie serve)端到端替换验证。

无 key、可重复。证明**整个 turn 驱动 + policy 层能用 milkie 替代 dolphin**,
产出与 dolphin 同构的 TurnEvent:

  fake OpenAI(多 content 帧) → milkie LLM stream → serve SSE(message_delta)
    → MilkieProvider.run_turn(_progress) → turn_orchestrator policy → TurnEvent

同时覆盖 milkie#86 验收指出的「子进程 e2e」缺口(就绪信号 + SIGTERM 退出)。
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

import everbot.core.agent.provider as provider_pkg
import everbot.infra.config as config_module
from everbot.core.agent.provider.milkie.provider import MilkieAgentHandle, MilkieProvider
from everbot.core.agent.provider.milkie.sidecar import MilkieSidecar
from everbot.core.runtime.turn_orchestrator import TurnOrchestrator
from everbot.core.runtime.turn_policy import CHAT_POLICY, TurnEventType

_TOKENS = ["Hello", ", ", "world", "!"]


def _milkie_cli() -> Path | None:
    cli = Path(__file__).resolve().parents[2].parent / "milkie" / "dist" / "cli" / "index.js"
    return cli if cli.exists() else None


class _FakeOpenAIHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length", 0))
        req = json.loads(self.rfile.read(length) or "{}")
        model = req.get("model", "fake")
        if not req.get("stream"):
            # 非流式(/llm 走 gateway.complete):回显收到的 model 名,验证 tier 路由。
            payload = json.dumps({
                "id": "c", "object": "chat.completion", "created": 0, "model": model,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": f"echo:{model}"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        frames = [
            "data: " + json.dumps({
                "id": "c", "object": "chat.completion.chunk", "created": 0, "model": model,
                "choices": [{"index": 0, "delta": {"content": t}, "finish_reason": None}],
            })
            for t in _TOKENS
        ]
        frames.append("data: " + json.dumps({
            "id": "c", "object": "chat.completion.chunk", "created": 0, "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }))
        frames.append("data: [DONE]")
        body = ("\n\n".join(frames) + "\n\n").encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def fake_openai_port():
    server = HTTPServer(("127.0.0.1", 0), _FakeOpenAIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()


def _write_agent(tmp_path: Path, fake_port: int) -> Path:
    md = tmp_path / "smoke.md"
    md.write_text(
        "---\n"
        "agentId: smoke\n"
        "version: 1.0.0\n"
        "fsm:\n"
        "  states:\n"
        "    - name: react\n"
        "      type: llm\n"
        "      instructions: respond to the user\n"
        "model:\n"
        "  provider: openai\n"
        "  model: fake-model\n"
        "  adapter: openai-compatible\n"
        f"  baseUrl: http://127.0.0.1:{fake_port}/v1\n"
        "---\n"
        "You are a smoke-test agent.\n",
        encoding="utf-8",
    )
    return md


async def test_milkie_drives_turn_via_orchestrator_end_to_end(tmp_path, fake_openai_port, monkeypatch):
    cli = _milkie_cli()
    if cli is None:
        pytest.skip("milkie dist not built at ../milkie/dist/cli/index.js")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-smoke")

    agent_md = _write_agent(tmp_path, fake_openai_port)
    sidecar = MilkieSidecar(
        ["node", str(cli), "serve", "--agent", str(agent_md), "--port", "0"],
        ready_timeout=20.0,
    )
    await sidecar.start()
    # 经配置开关切到 milkie:config=milkie + base_url → get_provider() 自动返回 MilkieProvider
    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda *a, **k: {"everbot": {"provider": "milkie", "milkie": {"base_url": sidecar.base_url}}},
    )
    provider_pkg.reset_provider()
    try:
        provider = provider_pkg.get_provider()
        assert isinstance(provider, MilkieProvider)  # C3 配置开关生效
        handle = await provider.create_agent("smoke", "/ws")
        orchestrator = TurnOrchestrator(CHAT_POLICY)
        events = [e async for e in orchestrator.run_turn(handle, "say hello")]
    finally:
        provider_pkg.reset_provider()
        await sidecar.close()

    deltas = [e.content for e in events if e.type == TurnEventType.LLM_DELTA]
    assert len(deltas) >= 2, f"expected token-level streaming, got {deltas}"
    assert "".join(deltas) == "Hello, world!"

    completes = [e for e in events if e.type == TurnEventType.TURN_COMPLETE]
    assert len(completes) == 1, f"expected exactly one TURN_COMPLETE, got {events}"
    assert completes[0].answer == "Hello, world!"

    assert sidecar.returncode is not None  # SIGTERM 后子进程已退出


async def test_context_var_roundtrip_via_real_serve(tmp_path, monkeypatch):
    """跨进程 context var 端到端:MilkieProvider.set_variable → 真 serve /context/set →
    get_variable 读回(milkie#83 HTTP 暴露 + alfred MilkieProvider sync client)。"""
    cli = _milkie_cli()
    if cli is None:
        pytest.skip("milkie dist not built at ../milkie/dist/cli/index.js")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-smoke")

    agent_md = _write_agent(tmp_path, 1)  # context var 不触发 LLM,baseUrl 不会被用到
    sidecar = MilkieSidecar(
        ["node", str(cli), "serve", "--agent", str(agent_md), "--port", "0"],
        ready_timeout=20.0,
    )
    await sidecar.start()
    try:
        provider = MilkieProvider(sidecar.base_url)
        handle = MilkieAgentHandle(sidecar.base_url, "ctx-rt")
        provider.set_variable(handle, "model_name", "claude-x")
        assert provider.get_variable(handle, "model_name") == "claude-x"
        assert provider.get_variable(handle, "missing") is None
    finally:
        await sidecar.close()


def _write_agent_tiers(tmp_path: Path, fake_port: int) -> Path:
    """agent.md 配两档 model:default 与 fast 各指向不同 model 名(验证 tier 路由)。"""
    base = f"http://127.0.0.1:{fake_port}/v1"
    md = tmp_path / "tiers.md"
    md.write_text(
        "---\n"
        "agentId: tiers\n"
        "version: 1.0.0\n"
        "fsm:\n"
        "  states:\n"
        "    - name: react\n"
        "      type: llm\n"
        "      instructions: respond\n"
        "model:\n"
        "  provider: openai\n"
        "  model: default-model\n"
        "  adapter: openai-compatible\n"
        f"  baseUrl: {base}\n"
        "models:\n"
        "  fast:\n"
        "    provider: openai\n"
        "    model: fast-model\n"
        "    adapter: openai-compatible\n"
        f"    baseUrl: {base}\n"
        "---\n"
        "You are a tier-routing test agent.\n",
        encoding="utf-8",
    )
    return md


async def test_call_llm_tier_routing_via_real_serve(tmp_path, fake_openai_port, monkeypatch):
    """跨进程一次性 LLM:MilkieProvider.call_llm → 真 serve /llm(非流式)→ gateway.complete。
    fast=False 命中 default 档、fast=True 命中 fast 档(milkie#126 tier 路由端到端)。"""
    cli = _milkie_cli()
    if cli is None:
        pytest.skip("milkie dist not built at ../milkie/dist/cli/index.js")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-smoke")

    agent_md = _write_agent_tiers(tmp_path, fake_openai_port)
    sidecar = MilkieSidecar(
        ["node", str(cli), "serve", "--agent", str(agent_md), "--port", "0"],
        ready_timeout=20.0,
    )
    await sidecar.start()
    try:
        provider = MilkieProvider(sidecar.base_url)
        default_out = await provider.call_llm(None, "summarize", fast=False)
        fast_out = await provider.call_llm(None, "summarize", fast=True)
    finally:
        await sidecar.close()

    # fake server 把收到的 model 名回显进 content,故 output 暴露实际路由到的档。
    assert "default-model" in default_out, f"default tier 应路由到 default-model,得 {default_out!r}"
    assert "fast-model" in fast_out, f"fast tier 应路由到 fast-model,得 {fast_out!r}"


async def test_session_history_persists_across_serve_restart(tmp_path, fake_openai_port, monkeypatch):
    """#130 + #128 端到端:sqlite serve → run_turn 产历史 → export_session 翻译取回;
    SIGTERM 重启(全新进程,同 data-dir)→ 同 contextId export_session 仍完整取回(sqlite 持久化)。
    一并验证 sidecar 的 --state-store sqlite --data-dir 接线。"""
    cli = _milkie_cli()
    if cli is None:
        pytest.skip("milkie dist not built at ../milkie/dist/cli/index.js")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-smoke")

    agent_md = _write_agent(tmp_path, fake_openai_port)
    data_dir = tmp_path / "milkie-data"
    data_dir.mkdir(parents=True, exist_ok=True)  # serve 的 SQLiteStore 要求 dir 预存(sidecar 接线职责)
    ctx = "persist-ctx"

    def _cmd():
        return ["node", str(cli), "serve", "--agent", str(agent_md), "--port", "0",
                "--state-store", "sqlite", "--data-dir", str(data_dir)]

    sidecar = MilkieSidecar(_cmd(), ready_timeout=20.0)
    await sidecar.start()
    try:
        provider = MilkieProvider(sidecar.base_url)
        handle = MilkieAgentHandle(sidecar.base_url, ctx)
        _ = [e async for e in provider.run_turn(handle, "say hello")]
        hist1 = provider.export_session(handle)["history_messages"]
    finally:
        await sidecar.close()

    assert any(m["role"] == "user" and m["content"] == "say hello" for m in hist1), hist1
    assert any(
        m["role"] == "assistant" and "Hello, world!" in m.get("content", "") for m in hist1
    ), hist1

    # 重启:全新 serve 进程,指向同一 data-dir。
    sidecar2 = MilkieSidecar(_cmd(), ready_timeout=20.0)
    await sidecar2.start()
    try:
        provider2 = MilkieProvider(sidecar2.base_url)
        handle2 = MilkieAgentHandle(sidecar2.base_url, ctx)
        hist2 = provider2.export_session(handle2)["history_messages"]
    finally:
        await sidecar2.close()

    assert hist2 == hist1, f"sqlite 持久化:重启后历史应完整保留\nhist1={hist1}\nhist2={hist2}"
