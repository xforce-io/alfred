"""E2E 冒烟:真 spawn ``milkie serve`` + 本地 fake OpenAI 流式 server。

无 key、可重复。端到端证明 token 流透传:
  fake OpenAI(多 content 帧) → milkie LLM stream → onModelEvent
    → serve SSE(message_delta) → alfred MilkieProvider → 逐 LLM_DELTA + 终态

同时覆盖验收报告里 runServeServer 的子进程行为(就绪信号 stdout + SIGTERM
优雅退出)—— 即「子进程 e2e」缺口由 alfred 侧兜住。
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from everbot.core.agent.provider.milkie.provider import MilkieProvider
from everbot.core.agent.provider.milkie.sidecar import MilkieSidecar
from everbot.core.runtime.turn_policy import TurnEventType

_TOKENS = ["Hello", ", ", "world", "!"]


def _milkie_cli() -> Path | None:
    # alfred 与 milkie 是兄弟仓库:<github>/alfred 与 <github>/milkie
    cli = Path(__file__).resolve().parents[2].parent / "milkie" / "dist" / "cli" / "index.js"
    return cli if cli.exists() else None


class _FakeOpenAIHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length", 0))
        self.rfile.read(length)
        frames = [
            "data: " + json.dumps({
                "id": "c", "object": "chat.completion.chunk", "created": 0, "model": "fake",
                "choices": [{"index": 0, "delta": {"content": t}, "finish_reason": None}],
            })
            for t in _TOKENS
        ]
        frames.append("data: " + json.dumps({
            "id": "c", "object": "chat.completion.chunk", "created": 0, "model": "fake",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }))
        frames.append("data: [DONE]")
        body = ("\n\n".join(frames) + "\n\n").encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence
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


async def test_milkie_serve_streams_tokens_end_to_end(tmp_path, fake_openai_port, monkeypatch):
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
    try:
        provider = MilkieProvider(sidecar.base_url)
        events = [e async for e in provider.run_turn("say hello", context_id="smoke-1")]
    finally:
        await sidecar.close()

    deltas = [e.content for e in events if e.type == TurnEventType.LLM_DELTA]
    assert len(deltas) >= 2, f"expected token-level streaming, got {deltas}"
    assert "".join(deltas) == "Hello, world!"

    terminal = [e for e in events if e.type == TurnEventType.TURN_COMPLETE]
    assert len(terminal) == 1
    assert terminal[0].answer == "Hello, world!"
    assert terminal[0].status == "completed"

    assert sidecar.returncode is not None  # SIGTERM 后子进程已退出
