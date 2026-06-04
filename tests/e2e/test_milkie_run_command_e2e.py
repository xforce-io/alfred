"""E2E(#38 E 能力层 ★真实 skill 端到端):

证明 alfred → 真 `milkie serve` 子进程 → 内建 `run_command`(milkie#134)→ 真实子进程
脚本 整条链路通。fake LLM 第 1 轮发 `run_command` tool_call 跑一个真实 skill 脚本
(写副作用文件 + stdout),第 2 轮收到工具结果后给最终答复。

断言:① 脚本真的被执行(副作用文件含唯一 marker —— 只有真子进程能写出);
② turn 正常完成。这是 goal.md §4 E『★真实 skill 端到端』的可证伪验收。

无 key、可重复;milkie dist 未 build 时自动 skip。
"""
from __future__ import annotations

import json
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from src.everbot.core.agent.provider.milkie.pool import SidecarPool
from src.everbot.core.agent.provider.milkie.provider import MilkieAgentHandle, MilkieProvider


def _milkie_cli() -> Path | None:
    cli = Path(__file__).resolve().parents[2].parent / "milkie" / "dist" / "cli" / "index.js"
    return cli if cli.exists() else None


class _ToolCallingOpenAIHandler(BaseHTTPRequestHandler):
    """Stateful fake OpenAI: turn1 → run_command tool_call;turn2(已带 tool 结果)→ 终答。"""

    protocol_version = "HTTP/1.1"
    command = ""  # 由测试在起 server 前注入

    def _send_stream(self, frames: list[dict]) -> None:
        lines = ["data: " + json.dumps(f) for f in frames]
        lines.append("data: [DONE]")
        body = ("\n\n".join(lines) + "\n\n").encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length", 0))
        req = json.loads(self.rfile.read(length) or "{}")
        model = req.get("model", "fake")
        messages = req.get("messages", [])
        has_tool_result = any(m.get("role") == "tool" for m in messages)

        if not has_tool_result:
            # turn 1:发起 run_command 调用(name+arguments 一帧给全,再一帧收尾 tool_calls)
            self._send_stream([
                {"choices": [{"index": 0, "finish_reason": None, "delta": {
                    "tool_calls": [{
                        "index": 0, "id": "call_run", "type": "function",
                        "function": {"name": "run_command",
                                     "arguments": json.dumps({"command": type(self).command})},
                    }],
                }}]},
                {"choices": [{"index": 0, "finish_reason": "tool_calls", "delta": {}}]},
            ])
        else:
            # turn 2:工具已执行,给最终答复
            self._send_stream([
                {"choices": [{"index": 0, "finish_reason": None, "delta": {"content": "done-ok"}}], "model": model},
                {"choices": [{"index": 0, "finish_reason": "stop", "delta": {}}]},
            ])

    def log_message(self, *args):
        pass


def _make_real_skill(root: Path, marker_file: Path) -> Path:
    """造一个真实 shell 型 skill(SKILL.md + python 脚本),脚本写 marker 文件 + 打印 stdout。"""
    skill = root / "skills" / "echoskill"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "# Echo Skill\n\n把一个 marker 写进给定文件并回显。\n\n## 用法\n"
        "`python $SKILL_DIR/scripts/run.py <outfile> <marker>`\n",
        encoding="utf-8",
    )
    (skill / "scripts" / "run.py").write_text(
        "import sys\n"
        "out, marker = sys.argv[1], sys.argv[2]\n"
        "open(out, 'w', encoding='utf-8').write(marker)\n"
        "print('ran-skill:' + marker)\n",
        encoding="utf-8",
    )
    return skill / "scripts" / "run.py"


def _write_agent(tmp_path: Path, fake_port: int) -> Path:
    md = tmp_path / "exec_agent.md"
    md.write_text(
        "---\n"
        "agentId: execagent\n"
        "version: 1.0.0\n"
        "fsm:\n"
        "  states:\n"
        "    - name: react\n"
        "      type: llm\n"
        "      max_iterations: 6\n"
        "      instructions: use run_command to run the skill script, then answer\n"
        "model:\n"
        "  provider: openai\n"
        "  model: fake-model\n"
        "  adapter: openai-compatible\n"
        f"  baseUrl: http://127.0.0.1:{fake_port}/v1\n"
        "---\n"
        "You can run shell commands with run_command.\n",
        encoding="utf-8",
    )
    return md


# command 在 fixture 起 server 时注入;用 indirect 参数化把 marker/脚本路径传进去。
@pytest.fixture
def marker(tmp_path):
    return "MARKER-" + uuid.uuid4().hex[:12]


async def test_real_run_command_executes_skill_script_through_sidecar(tmp_path, marker, monkeypatch):
    cli = _milkie_cli()
    if cli is None:
        pytest.skip("milkie dist not built at ../milkie/dist/cli/index.js")

    out_file = tmp_path / "ran.txt"
    script = _make_real_skill(tmp_path, out_file)
    command = f'{sys.executable} {script} {out_file} {marker}'

    # 起带 tool_call 的 fake server(把要跑的真实命令注入 handler)
    handler = type("_H", (_ToolCallingOpenAIHandler,), {"command": command})
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    fake_port = server.server_address[1]
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")

    import os
    agent_md = _write_agent(tmp_path, fake_port)

    def _build(name):
        return (["node", str(cli), "serve", "--agent", str(agent_md), "--port", "0"],
                {"OPENAI_API_KEY": "sk-fake", "PATH": os.environ.get("PATH", "")})

    pool = SidecarPool(build=_build)
    provider = MilkieProvider()
    provider._pool = pool
    try:
        handle = await provider.create_agent("execagent", "/ws")
        events = [e async for e in provider.run_turn(handle, "run the echo skill")]
    finally:
        await provider.shutdown_sidecars()
        server.shutdown()

    # ★ 核心证伪:脚本真的被真子进程执行 → 副作用文件存在且含唯一 marker。
    assert out_file.exists(), "run_command 应通过真 sidecar 执行了脚本(副作用文件未生成)"
    assert out_file.read_text(encoding="utf-8") == marker

    # turn 正常完成(收到终态)。
    assert events, "应产出 turn 事件"
