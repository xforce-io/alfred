"""Cross-repository E2E for Alfred-owned retries over a real Milkie sidecar."""

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


class _TransientThenSuccessHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests_seen = 0

    def do_POST(self):  # noqa: N802
        type(self).requests_seen += 1
        length = int(self.headers.get("content-length", 0))
        request = json.loads(self.rfile.read(length) or "{}")
        if type(self).requests_seen <= 3:
            body = json.dumps({
                "error": {
                    "message": "temporary provider failure",
                    "type": "server_error",
                    "code": "server_error",
                }
            }).encode()
            self.send_response(503)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        model = request.get("model", "fake")
        frames = [
            "data: " + json.dumps({
                "id": "c", "object": "chat.completion.chunk", "created": 0,
                "model": model,
                "choices": [{
                    "index": 0, "delta": {"content": "final report"},
                    "finish_reason": None,
                }],
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


@pytest.mark.asyncio
async def test_transient_model_failure_retries_once_and_delivers_once(tmp_path):
    cli = _milkie_cli()
    if not cli.exists():
        pytest.skip(f"milkie dist not built: {cli}")

    _TransientThenSuccessHandler.requests_seen = 0
    server = HTTPServer(("127.0.0.1", 0), _TransientThenSuccessHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    data_dir = tmp_path / "milkie"
    data_dir.mkdir()
    agent_file = data_dir / "agent.md"
    agent_file.write_text(
        "---\n"
        "agentId: retry-agent\n"
        "version: 1.0.0\n"
        "fsm:\n  states:\n    - name: react\n      type: llm\n"
        "model:\n  provider: local-stub\n  model: retry-model\n"
        "  adapter: openai-compatible\n"
        f"  baseUrl: http://127.0.0.1:{server.server_address[1]}/v1\n"
        "---\nRespond with the report.\n",
        encoding="utf-8",
    )
    sidecar = MilkieSidecar(
        [
            "node", str(cli), "serve", "--agent", str(agent_file), "--port", "0",
            "--state-store", "sqlite", "--data-dir", str(data_dir),
        ],
        env={**os.environ, "OPENAI_API_KEY": "test-key"},
        ready_timeout=20,
    )
    await sidecar.start()

    try:
        provider = MilkieProvider(sidecar.base_url)
        handles = [
            MilkieAgentHandle(sidecar.base_url, "retry-attempt-1", name="retry-agent"),
            MilkieAgentHandle(sidecar.base_url, "retry-attempt-2", name="retry-agent"),
        ]

        session_manager = AsyncMock()
        session_manager.get_primary_session_id.return_value = "primary"
        session_manager.get_heartbeat_session_id.return_value = "heartbeat"
        delivery = CronDelivery(
            session_manager=session_manager,
            primary_session_id="primary",
            heartbeat_session_id="heartbeat",
            agent_name="retry-agent",
            realtime_push=False,
        )
        delivery.deposit_job_event = AsyncMock()
        delivery.inject_to_history = AsyncMock()
        delivery._emit_realtime = AsyncMock()

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        manager = RoutineManager(workspace)
        manager.add_routine(
            title="Retry E2E", schedule="1h", execution_mode="isolated",
            next_run_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        )
        task_list = manager.load_task_list()
        task = task_list.tasks[0]
        task.max_retry = 1
        manager.flush(task_list)

        executor = CronExecutor(
            agent_name="retry-agent", workspace_path=workspace,
            session_manager=session_manager, agent_factory=AsyncMock(),
            routine_manager=manager, delivery=delivery,
        )
        executor._create_job_agent = AsyncMock(side_effect=handles)
        executor._build_job_system_prompt = MagicMock(return_value="system")
        executor._record_skill_log = MagicMock()

        async def run_agent(handle, prompt, **kwargs):
            chunks = []
            async for event in provider.run_turn(handle, prompt):
                chunks.extend(
                    item["delta"] for item in event.get("_progress", [])
                    if item.get("stage") == "llm" and item.get("delta")
                )
            return "".join(chunks)

        first = await executor.tick(
            task_list, run_agent=run_agent, inject_context=AsyncMock(), run_id="cron-1",
        )
        assert first.failed == 1
        assert task.state == "pending"
        assert task.retry == 1
        assert task.last_error_code == "MODEL_BAD_RESPONSE", task.error_message
        assert task.last_error_retryable is True

        task.next_run_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        second = await executor.tick(
            task_list, run_agent=run_agent, inject_context=AsyncMock(), run_id="cron-2",
        )
        assert second.executed == 1
        assert executor._create_job_agent.await_count == 2
        successful_pushes = [
            call for call in delivery._emit_realtime.await_args_list
            if call.kwargs.get("transcript_worthy") is True
        ]
        assert len(successful_pushes) == 1
        assert successful_pushes[0].args[0] == "final report"
    finally:
        await sidecar.close()
        server.shutdown()
