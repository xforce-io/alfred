"""E2E test configuration for ops skill against a real Alfred environment."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

# ---------------------------------------------------------------------------
# Default environment registry
# ---------------------------------------------------------------------------

E2E_ENV = {
    "env0": {
        "project_root": "~/lab/env0",
        "alfred_home": "~/lab/env0/.alfred",
    },
}


# ---------------------------------------------------------------------------
# pytest CLI options
# ---------------------------------------------------------------------------

def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--e2e-env", default="env0",
        help="E2E environment name from E2E_ENV registry (default: env0)",
    )
    parser.addoption(
        "--e2e-project-root", default=None,
        help="Override project_root (path containing bin/everbot)",
    )
    parser.addoption(
        "--e2e-alfred-home", default=None,
        help="Override ALFRED_HOME path",
    )
    parser.addoption(
        "--run-destructive", action="store_true", default=False,
        help="Enable destructive lifecycle tests (start/stop/restart)",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "destructive: marks tests that mutate daemon state (start/stop/restart)",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: List[pytest.Item],
) -> None:
    if config.getoption("--run-destructive"):
        return
    skip = pytest.mark.skip(reason="needs --run-destructive option to run")
    for item in items:
        if "destructive" in item.keywords:
            item.add_marker(skip)


# ---------------------------------------------------------------------------
# Shared milkie e2e helpers: fake OpenAI streaming server
# ---------------------------------------------------------------------------

# 逐 content 帧驱动 milkie LLM stream → serve SSE(message_delta);共享给所有 milkie
# serve e2e(serve smoke + daemon smoke)。
_TOKENS = ["Hello", ", ", "world", "!"]


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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def e2e_env(request: pytest.FixtureRequest) -> Dict[str, Path]:
    """Resolve and validate the E2E environment paths.

    Returns dict with keys ``project_root`` and ``alfred_home`` as Paths.
    Skips the entire session if the environment is not available.
    """
    env_name = request.config.getoption("--e2e-env")
    env_cfg = E2E_ENV.get(env_name, E2E_ENV["env0"])

    project_root = request.config.getoption("--e2e-project-root")
    if project_root is None:
        project_root = env_cfg["project_root"]
    project_root = Path(project_root).expanduser().resolve()

    alfred_home = request.config.getoption("--e2e-alfred-home")
    if alfred_home is None:
        alfred_home = env_cfg["alfred_home"]
    alfred_home = Path(alfred_home).expanduser().resolve()

    everbot_bin = project_root / "bin" / "everbot"
    if not everbot_bin.exists():
        pytest.skip(
            f"E2E environment not available: {everbot_bin} does not exist",
        )

    return {
        "project_root": project_root,
        "alfred_home": alfred_home,
    }


@pytest.fixture(scope="session")
def everbot_bin(e2e_env: Dict[str, Path]) -> Path:
    """Absolute path to ``bin/everbot``."""
    return e2e_env["project_root"] / "bin" / "everbot"


@pytest.fixture(scope="session")
def alfred_home(e2e_env: Dict[str, Path]) -> Path:
    """Absolute path to ALFRED_HOME."""
    return e2e_env["alfred_home"]


@pytest.fixture(scope="session")
def ops_cli_path() -> Path:
    """Absolute path to ``ops_cli.py``."""
    return (
        Path(__file__).resolve().parent.parent.parent
        / "skills" / "ops" / "scripts" / "ops_cli.py"
    )


@pytest.fixture(scope="session")
def run_everbot(everbot_bin: Path, e2e_env: Dict[str, Path]):
    """Factory fixture: run ``bin/everbot <args>`` and return (rc, stdout, stderr)."""

    def _run(args: List[str], timeout: int = 30) -> Tuple[int, str, str]:
        cmd = [str(everbot_bin)] + args
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(e2e_env["project_root"]),
        )
        return proc.returncode, proc.stdout, proc.stderr

    return _run


@pytest.fixture(scope="session")
def run_ops(ops_cli_path: Path, alfred_home: Path):
    """Factory fixture: run ``ops_cli.py --alfred-home <path> <args>`` and return parsed JSON."""

    def _run(args: List[str], timeout: int = 30) -> Dict[str, Any]:
        cmd = [
            sys.executable, str(ops_cli_path),
            "--alfred-home", str(alfred_home),
        ] + args
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            return {
                "ok": False,
                "error": f"non-JSON output (rc={proc.returncode})",
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            }

    return _run
