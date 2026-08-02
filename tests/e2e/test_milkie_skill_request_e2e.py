"""E2E(#164 S1/S2): alfred → real milkie serve → closed-world skill_request.

Proves host skill-manifest injection + milkie tool handler contract:

  fake OpenAI (skill_request tool_call) → milkie serve → skill_request handler
    → hit: instructions + dir from manifest.dir/SKILL.md only
    → miss: not_found without HOME/full-disk SKILL.md search

No API key required; milkie dist missing → skip.
"""
from __future__ import annotations

import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.everbot.core.agent.provider.milkie.launcher import (
    SKILL_MANIFEST_ENV,
    _render_skill_manifest,
)
from src.everbot.core.agent.provider.milkie.pool import SidecarPool
from src.everbot.core.agent.provider.milkie.provider import MilkieProvider


def _milkie_cli() -> Optional[Path]:
    configured = os.environ.get("MILKIE_CLI")
    if configured:
        p = Path(configured).expanduser()
        return p if p.exists() else None
    alfred_root = Path(__file__).resolve().parents[2]
    for sibling in ("milkie-164", "milkie"):
        candidate = alfred_root.parent / sibling / "dist" / "cli" / "index.js"
        if candidate.exists():
            return candidate
    return None


class _SkillRequestOpenAIHandler(BaseHTTPRequestHandler):
    """Stateful fake OpenAI: skill_request(hit) → skill_request(miss) → final text.

    Captures tool result payloads from subsequent LLM requests for assertion.
    """

    protocol_version = "HTTP/1.1"
    # Injected before server starts
    skill_name: str = "web"
    unknown_name: str = "no-such-skill-xyz"
    tool_results: List[Any] = []
    tool_calls_seen: List[str] = []

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
        messages = req.get("messages", [])

        # Harvest tool results milkie feeds back into the next completion.
        for m in messages:
            if m.get("role") == "tool":
                content = m.get("content", "")
                try:
                    parsed = json.loads(content) if isinstance(content, str) else content
                except (TypeError, json.JSONDecodeError):
                    parsed = content
                type(self).tool_results.append(parsed)

        n_tool_msgs = sum(1 for m in messages if m.get("role") == "tool")

        if n_tool_msgs == 0:
            type(self).tool_calls_seen.append("skill_request")
            self._send_stream([
                {"choices": [{"index": 0, "finish_reason": None, "delta": {
                    "tool_calls": [{
                        "index": 0, "id": "call_sr_hit", "type": "function",
                        "function": {
                            "name": "skill_request",
                            "arguments": json.dumps({"name": type(self).skill_name}),
                        },
                    }],
                }}]},
                {"choices": [{"index": 0, "finish_reason": "tool_calls", "delta": {}}]},
            ])
        elif n_tool_msgs == 1:
            type(self).tool_calls_seen.append("skill_request")
            self._send_stream([
                {"choices": [{"index": 0, "finish_reason": None, "delta": {
                    "tool_calls": [{
                        "index": 0, "id": "call_sr_miss", "type": "function",
                        "function": {
                            "name": "skill_request",
                            "arguments": json.dumps({"name": type(self).unknown_name}),
                        },
                    }],
                }}]},
                {"choices": [{"index": 0, "finish_reason": "tool_calls", "delta": {}}]},
            ])
        else:
            self._send_stream([
                {"choices": [{"index": 0, "finish_reason": None, "delta": {
                    "content": "skill-load-done",
                }}]},
                {"choices": [{"index": 0, "finish_reason": "stop", "delta": {}}]},
            ])

    def log_message(self, *args):
        pass


def _make_web_skill(root: Path) -> Dict[str, Any]:
    skill = root / "skills" / "web"
    skill.mkdir(parents=True)
    # Body must not contain banned discovery phrases: hit tool_result embeds
    # instructions, and trajectory assertions scan tool outputs.
    body = (
        "---\n"
        "name: web\n"
        "version: \"1.0.0\"\n"
        "---\n"
        "# Web\n\n"
        "Closed-world e2e fixture for skill_request.\n\n"
        "Load via skill_request; use returned instructions and dir.\n"
    )
    (skill / "SKILL.md").write_text(body, encoding="utf-8")
    return {
        "name": "web",
        "title": "Web",
        "description": "web skill e2e fixture",
        "abs_path": str(skill.resolve()),
    }


def _write_agent(tmp_path: Path, fake_port: int) -> Path:
    md = tmp_path / "skill_req_agent.md"
    md.write_text(
        "---\n"
        "agentId: skillreq\n"
        "version: 1.0.0\n"
        "fsm:\n"
        "  states:\n"
        "    - name: react\n"
        "      type: llm\n"
        "      max_iterations: 8\n"
        "      instructions: load skills via skill_request then answer\n"
        "model:\n"
        "  provider: openai\n"
        "  model: fake-model\n"
        "  adapter: openai-compatible\n"
        f"  baseUrl: http://127.0.0.1:{fake_port}/v1\n"
        "---\n"
        "Prefer skill_request over shell discovery for SKILL.md.\n",
        encoding="utf-8",
    )
    return md


def _progress_items(events: list) -> list:
    items = []
    for e in events:
        if isinstance(e, dict) and "_progress" in e:
            items.extend(e["_progress"])
    return items


async def test_skill_request_hit_and_miss_through_real_sidecar(tmp_path, monkeypatch):
    """E1+E2: installed skill → instructions+dir; unknown → not_found; no HOME find."""
    cli = _milkie_cli()
    if cli is None:
        pytest.skip("milkie dist not built (set MILKIE_CLI or build milkie-164/milkie)")

    skill_meta = _make_web_skill(tmp_path)
    skill_dir = skill_meta["abs_path"]
    manifest = _render_skill_manifest([skill_meta])
    data_dir = tmp_path / "milkie-data" / "skillreq"
    data_dir.mkdir(parents=True)
    manifest_path = data_dir / "skill-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    handler_cls = type(
        "_H",
        (_SkillRequestOpenAIHandler,),
        {
            "skill_name": "web",
            "unknown_name": "no-such-skill-xyz",
            "tool_results": [],
            "tool_calls_seen": [],
        },
    )
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    fake_port = server.server_address[1]
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")

    agent_md = _write_agent(tmp_path, fake_port)

    def _build(_name: str):
        env = {
            "OPENAI_API_KEY": "sk-fake",
            "PATH": os.environ.get("PATH", ""),
            SKILL_MANIFEST_ENV: str(manifest_path),
        }
        cmd = [
            "node", str(cli), "serve",
            "--agent", str(agent_md),
            "--port", "0",
            "--state-store", "sqlite",
            "--data-dir", str(data_dir),
        ]
        return cmd, env

    pool = SidecarPool(build=_build)
    provider = MilkieProvider()
    provider._pool = pool
    try:
        handle = await provider.create_agent("skillreq", "/ws-skill-req")
        events = [e async for e in provider.run_turn(handle, "load the web skill then try missing")]
    finally:
        await provider.shutdown_sidecars()
        server.shutdown()

    assert events, "turn should emit progress events"

    # Skill-plane tools: only skill_request (≤ 2 calls for hit+miss load path)
    requested = [
        i for i in _progress_items(events)
        if i.get("stage") == "skill" and i.get("status") == "running"
    ]
    completed = [
        i for i in _progress_items(events)
        if i.get("stage") == "skill" and i.get("status") == "completed"
    ]
    tool_names = [i.get("skill_info", {}).get("name") for i in requested]
    assert all(n == "skill_request" for n in tool_names), (
        f"only skill_request allowed on load path, got {tool_names}"
    )
    assert 1 <= len(tool_names) <= 2, (
        f"skill-plane calls should be ≤ 2 for load (list+request or request×2), got {len(tool_names)}"
    )
    # Fake issues hit then miss → exactly 2 skill_request
    assert len(completed) == 2, f"expected hit+miss tool results, got {len(completed)}: {completed}"

    def _parse_answer(item: dict) -> dict:
        raw = item.get("answer") or ""
        if isinstance(raw, dict):
            return raw
        return json.loads(raw)

    hit = _parse_answer(completed[0])
    miss = _parse_answer(completed[1])

    # S1 / E1: hit returns instructions + authoritative dir
    assert hit.get("status") == "ok", hit
    assert hit.get("dir") == skill_dir
    assert hit.get("instructionPath") == str(Path(skill_dir) / "SKILL.md")
    assert "Closed-world e2e fixture" in str(hit.get("instructions") or "")
    assert hit.get("truncated") is False
    # Guidance fields only (not instructions body): must not teach shell discovery
    hit_msg = str(hit.get("message") or "")
    assert "run_command/cat" not in hit_msg
    assert not re.search(r"find\s+\$HOME|find\s+/Users", hit_msg)

    # S2 / E2: unknown name → not_found, no disk search guidance
    assert miss.get("status") in ("not_found", "unavailable"), miss
    assert miss.get("status") == "not_found", miss
    assert "instructions" not in miss or miss.get("instructions") in (None, "")
    miss_msg = str(miss.get("message") or "")
    assert "run_command/cat" not in miss_msg
    assert not re.search(r"find\s+\$HOME|find\s+/", miss_msg)

    # Trajectory / tool plane: only skill_request — no run_command / shell discovery
    assert "run_command" not in tool_names
    progress = _progress_items(events)
    plane_tools = [
        (i.get("skill_info") or {}).get("name")
        for i in progress
        if i.get("stage") in ("skill", "tool") and (i.get("skill_info") or {}).get("name")
    ]
    assert all(n == "skill_request" for n in plane_tools), plane_tools
    # Command args (if present on progress items) must not show HOME/root SKILL.md search
    for item in progress:
        info = item.get("skill_info") or {}
        cmd_blob = json.dumps(
            {"input": info.get("input"), "command": info.get("command"), "args": info.get("args")},
            ensure_ascii=False,
        )
        assert not re.search(r"find\s+\$HOME|find\s+/Users|find\s+/ ", cmd_blob)
    assert handler_cls.tool_calls_seen == ["skill_request", "skill_request"]

    # #187 S1/S2/S3: real sidecar observation → SLM segment → report → no_changes.
    # No Dolphin trajectory is created anywhere in this path.
    batch = provider.get_skill_observations(handle)
    assert batch.complete is True
    assert batch.skill_names == ("web",)
    assert not (tmp_path / "trajectory_job_routine_fixture.json").exists()

    from src.everbot.core.slm.skill_log_recorder import SkillLogRecorder
    from src.everbot.core.slm.segment_logger import SegmentLogger

    logs_dir = tmp_path / "skill_logs"
    eval_dir = tmp_path / "skill_eval"
    skills_root = tmp_path / "skills"
    recorder = SkillLogRecorder(
        logs_dir,
        skill_dirs=[skills_root],
        eval_base_dir=eval_dir,
    )
    assert recorder.record_observation_state(
        complete=batch.complete,
        session_id="job_routine_e2e",
        reason=batch.reason,
        observed_skills=len(batch.skill_names),
    )
    for skill_name in batch.skill_names:
        assert recorder.maybe_record(
            skill_name,
            session_id="job_routine_e2e",
            context_before="load web skill",
            skill_output="skill-load-done",
        )

    segments = SegmentLogger(logs_dir).load("web")
    assert len(segments) == 1
    assert segments[0].skill_id == "web"
    assert segments[0].skill_version == "1.0.0"
    assert segments[0].session_id == "job_routine_e2e"

    from src.everbot.core.jobs.skill_evaluate import run as run_skill_evaluate
    from src.everbot.core.slm.models import EvalReport, JudgeResult

    context = SimpleNamespace(
        agent_name="skillreq",
        llm=MagicMock(),
        mailbox=SimpleNamespace(deposit=AsyncMock(return_value=None)),
        skill_logs_dir=logs_dir,
        skill_eval_dir=eval_dir,
    )
    report = EvalReport(
        skill_id="web",
        skill_version="1.0.0",
        evaluated_at="2026-08-02T00:00:00+00:00",
        segment_count=1,
        critical_issue_count=0,
        critical_issue_rate=0.0,
        mean_satisfaction=0.9,
        results=[JudgeResult(
            segment_index=0,
            has_critical_issue=False,
            satisfaction=0.9,
            reason="e2e fixture ok",
        )],
    )
    udm = MagicMock()
    udm.skill_logs_dir = logs_dir
    udm.skills_dir = skills_root
    udm.repo_skills_dir = skills_root
    udm.sessions_dir = tmp_path / "sessions"
    udm.get_agent_writable_skills_dir.return_value = skills_root
    udm.get_agent_read_skill_dirs.return_value = [skills_root]

    with patch("src.everbot.infra.user_data.get_user_data_manager", return_value=udm), patch(
        "src.everbot.core.jobs.skill_evaluate.evaluate_skill",
        new=AsyncMock(return_value=report),
    ) as judge:
        first = await run_skill_evaluate(context)
        second = await run_skill_evaluate(context)

    assert first.status == "completed"
    assert first.reason == "evaluated"
    assert first.detail["observed_count"] == 1
    assert first.detail["eligible_count"] == 1
    assert first.detail["evaluated_count"] == 1
    assert second.status == "skipped"
    assert second.reason == "no_changes"
    assert second.detail["evaluated_count"] == 0
    judge.assert_awaited_once()
    reports = list(eval_dir.glob("web/versions/*/eval_report.json"))
    assert len(reports) == 1
