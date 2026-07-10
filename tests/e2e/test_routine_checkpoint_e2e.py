"""Cross-process E2E for staged routine recovery over a real Milkie sidecar."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.everbot.core.agent.provider.milkie.provider import MilkieAgentHandle, MilkieProvider
from src.everbot.core.agent.provider.milkie.sidecar import MilkieSidecar
from src.everbot.core.runtime.cron import CronExecutor
from src.everbot.core.runtime.cron_delivery import CronDelivery
from src.everbot.core.tasks.routine_manager import RoutineManager


def _milkie_cli() -> Path:
    configured = os.environ.get("MILKIE_CLI")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[2].parent / "milkie" / "dist" / "cli" / "index.js"


class _FetchThenAnalyzeFailureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests_seen = 0

    def do_POST(self):  # noqa: N802
        type(self).requests_seen += 1
        length = int(self.headers.get("content-length", 0))
        request = json.loads(self.rfile.read(length) or "{}")
        request_number = type(self).requests_seen
        if 2 <= request_number <= 4:
            body = json.dumps({"error": {"message": "analyze unavailable", "type": "server_error"}}).encode()
            self.send_response(503)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        text = "fetched artifact" if request_number == 1 else "final report"
        model = request.get("model", "fake")
        frames = [
            "data: " + json.dumps({
                "id": "c", "object": "chat.completion.chunk", "created": 0,
                "model": model,
                "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
            }),
            "data: " + json.dumps({
                "id": "c", "object": "chat.completion.chunk", "created": 0,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }),
            "data: [DONE]",
        ]
        body = ("\n\n".join(frames) + "\n\n").encode()
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _sidecar(cli: Path, agent_file: Path, data_dir: Path) -> MilkieSidecar:
    return MilkieSidecar(
        [
            "node", str(cli), "serve", "--agent", str(agent_file), "--port", "0",
            "--state-store", "sqlite", "--data-dir", str(data_dir),
        ],
        env={**os.environ, "OPENAI_API_KEY": "test-key"},
        ready_timeout=20,
    )


@pytest.mark.asyncio
async def test_restart_resumes_analyze_and_delivers_once(tmp_path):
    cli = _milkie_cli()
    if not cli.exists():
        pytest.skip(f"milkie dist not built: {cli}")

    _FetchThenAnalyzeFailureHandler.requests_seen = 0
    server = HTTPServer(("127.0.0.1", 0), _FetchThenAnalyzeFailureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    data_dir = tmp_path / "milkie"
    data_dir.mkdir()
    agent_file = data_dir / "agent.md"
    agent_file.write_text(
        "---\nagentId: staged-agent\nversion: 1.0.0\n"
        "fsm:\n  states:\n    - name: react\n      type: llm\n"
        "model:\n  provider: local-stub\n  model: staged-model\n"
        "  adapter: openai-compatible\n"
        f"  baseUrl: http://127.0.0.1:{server.server_address[1]}/v1\n"
        "---\nFollow the stage prompt.\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = RoutineManager(workspace)
    manager.add_routine(
        title="Staged E2E", schedule="1h", execution_mode="isolated",
        next_run_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        staged={
            "fetch": {"prompt": "fetch fixture"},
            "analyze": {"prompt": "analyze fixture"},
            "destination": "primary",
        },
    )
    task_list = manager.load_task_list()
    task_list.tasks[0].max_retry = 1
    manager.flush(task_list)

    session_manager = AsyncMock()
    session_manager.get_primary_session_id.return_value = "primary"
    session_manager.get_heartbeat_session_id.return_value = "heartbeat"
    delivery = CronDelivery(
        session_manager=session_manager, primary_session_id="primary",
        heartbeat_session_id="heartbeat", agent_name="staged-agent", realtime_push=False,
    )
    delivery.deposit_job_event = AsyncMock()
    delivery.inject_to_history = AsyncMock()
    delivery._emit_realtime = AsyncMock()

    async def execute(sidecar: MilkieSidecar, run_id: str, tasks):
        provider = MilkieProvider(sidecar.base_url)
        handle = MilkieAgentHandle(sidecar.base_url, run_id, name="staged-agent")
        executor = CronExecutor(
            agent_name="staged-agent", workspace_path=workspace,
            session_manager=session_manager, agent_factory=AsyncMock(),
            routine_manager=RoutineManager(workspace), delivery=delivery,
        )
        executor._create_job_agent = AsyncMock(return_value=handle)
        executor._build_job_system_prompt = MagicMock(return_value="system")
        executor._record_skill_log = MagicMock()

        async def run_agent(agent, prompt, **kwargs):
            chunks = []
            async for event in provider.run_turn(agent, prompt):
                chunks.extend(
                    item["delta"] for item in event.get("_progress", [])
                    if item.get("stage") == "llm" and item.get("delta")
                )
            return "".join(chunks)

        return await executor.tick(
            tasks, run_agent=run_agent, inject_context=AsyncMock(), run_id=run_id,
        )

    first_sidecar = _sidecar(cli, agent_file, data_dir)
    await first_sidecar.start()
    try:
        first = await execute(first_sidecar, "cron-1", task_list)
        assert first.failed == 1
        assert _FetchThenAnalyzeFailureHandler.requests_seen == 4
    finally:
        await first_sidecar.close()

    restarted_manager = RoutineManager(workspace)
    restarted_tasks = restarted_manager.load_task_list()
    restarted_tasks.tasks[0].next_run_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    restarted_manager.flush(restarted_tasks)
    second_sidecar = _sidecar(cli, agent_file, data_dir)
    await second_sidecar.start()
    try:
        second = await execute(second_sidecar, "cron-2", restarted_tasks)
        assert second.executed == 1
        assert _FetchThenAnalyzeFailureHandler.requests_seen == 5
        successful_pushes = [
            call for call in delivery._emit_realtime.await_args_list
            if call.kwargs.get("transcript_worthy") is True
        ]
        assert len(successful_pushes) == 1
        assert successful_pushes[0].args[0] == "final report"
        manifests = list((workspace / ".runtime" / "routine-checkpoints").glob("*/manifest.json"))
        assert len(manifests) == 1
        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        assert set(manifest["stages"]) == {"fetch", "analyze"}
        assert all(state == "delivered" for state in manifest["delivery"]["steps"].values())
    finally:
        await second_sidecar.close()
        server.shutdown()
