"""E2E(#187 S1/S2/S3): Milkie skill observation → SLM segment → Skill Evaluate.

Acceptance paths (no Dolphin trajectory):

  S1  real milkie serve skill_request hit → complete SkillObservationBatch
      → unique EvaluationSegment keyed by session
  S2  Skill Evaluate consumes the new segment → completed + matching counts
      + eval_report coverage
  S3  second evaluate → skipped/no_changes;
      incomplete observation state → degraded/observation_unavailable

No API key required. Missing milkie dist → skip milkie-backed cases only.
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.everbot.core.agent.provider.milkie.launcher import (
    SKILL_MANIFEST_ENV,
    _render_skill_manifest,
)
from src.everbot.core.agent.provider.milkie.pool import SidecarPool
from src.everbot.core.agent.provider.milkie.provider import MilkieProvider
from src.everbot.core.jobs.skill_evaluate import run as run_skill_evaluate
from src.everbot.core.slm.models import EvalReport, JudgeResult
from src.everbot.core.slm.segment_logger import SegmentLogger
from src.everbot.core.slm.skill_log_recorder import SkillLogRecorder


SESSION_ID = "job_routine_187_e2e"
SKILL_NAME = "web"
SKILL_VERSION = "1.0.0"


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


def _node_bin() -> str:
    """Prefer a Node binary whose ABI matches milkie's better-sqlite3 build.

    Local milkie dist is typically built against Node 22 (NODE_MODULE_VERSION
    127). PATH ``node`` may be 23+ and fail sidecar start; allow override via
    ``MILKIE_NODE``.
    """
    configured = os.environ.get("MILKIE_NODE")
    if configured:
        p = Path(configured).expanduser()
        if p.exists():
            return str(p)
    candidates = [
        Path.home() / ".nvm/versions/node/v22.22.3/bin/node",
        Path("/opt/homebrew/opt/node@22/bin/node"),
        Path("/usr/local/opt/node@22/bin/node"),
    ]
    nvm_root = Path.home() / ".nvm/versions/node"
    if nvm_root.is_dir():
        for child in sorted(nvm_root.glob("v22.*/bin/node"), reverse=True):
            candidates.append(child)
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return "node"


def _require_milkie_cli() -> Path:
    cli = _milkie_cli()
    if cli is None:
        pytest.skip("milkie dist not built (set MILKIE_CLI or build sibling milkie)")
    return cli


class _ScriptedSkillRequestHandler(BaseHTTPRequestHandler):
    """Fake OpenAI that emits a fixed sequence of skill_request calls then text.

    Class attrs set before the server starts:
      skill_calls: ordered skill names to request (empty → final text only)
      tool_results / tool_calls_seen: harvested for optional assertions
    """

    protocol_version = "HTTP/1.1"
    skill_calls: Sequence[str] = ()
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

        for m in messages:
            if m.get("role") == "tool":
                content = m.get("content", "")
                try:
                    parsed = json.loads(content) if isinstance(content, str) else content
                except (TypeError, json.JSONDecodeError):
                    parsed = content
                type(self).tool_results.append(parsed)

        n_tool_msgs = sum(1 for m in messages if m.get("role") == "tool")
        calls = list(type(self).skill_calls)

        if n_tool_msgs < len(calls):
            name = calls[n_tool_msgs]
            call_id = f"call_sr_{n_tool_msgs}"
            type(self).tool_calls_seen.append("skill_request")
            self._send_stream([
                {"choices": [{"index": 0, "finish_reason": None, "delta": {
                    "tool_calls": [{
                        "index": 0, "id": call_id, "type": "function",
                        "function": {
                            "name": "skill_request",
                            "arguments": json.dumps({"name": name}),
                        },
                    }],
                }}]},
                {"choices": [{"index": 0, "finish_reason": "tool_calls", "delta": {}}]},
            ])
            return

        self._send_stream([
            {"choices": [{"index": 0, "finish_reason": None, "delta": {
                "content": "skill-observation-done",
            }}]},
            {"choices": [{"index": 0, "finish_reason": "stop", "delta": {}}]},
        ])

    def log_message(self, *args):
        pass


def _make_web_skill(root: Path) -> Dict[str, Any]:
    skill = root / "skills" / SKILL_NAME
    skill.mkdir(parents=True)
    body = (
        "---\n"
        f"name: {SKILL_NAME}\n"
        f'version: "{SKILL_VERSION}"\n'
        "---\n"
        "# Web\n\n"
        "Closed-world e2e fixture for skill observation (#187).\n\n"
        "Load via skill_request; use returned instructions and dir.\n"
    )
    (skill / "SKILL.md").write_text(body, encoding="utf-8")
    return {
        "name": SKILL_NAME,
        "title": "Web",
        "description": "web skill observation e2e fixture",
        "abs_path": str(skill.resolve()),
    }


def _write_agent(tmp_path: Path, fake_port: int) -> Path:
    md = tmp_path / "skill_obs_agent.md"
    md.write_text(
        "---\n"
        "agentId: skillobs\n"
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


def _start_scripted_server(skill_calls: Sequence[str]):
    handler_cls = type(
        "_SkillObsHandler",
        (_ScriptedSkillRequestHandler,),
        {
            "skill_calls": tuple(skill_calls),
            "tool_results": [],
            "tool_calls_seen": [],
        },
    )
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, handler_cls, server.server_address[1]


async def _run_milkie_turn(
    tmp_path: Path,
    monkeypatch,
    *,
    skill_calls: Sequence[str],
    include_manifest_skill: bool = True,
):
    """Boot real milkie serve + fake OpenAI; return (provider, handle, events, paths)."""
    cli = _require_milkie_cli()
    skill_meta = _make_web_skill(tmp_path)
    skills_root = tmp_path / "skills"
    manifest_skills = [skill_meta] if include_manifest_skill else []
    manifest = _render_skill_manifest(manifest_skills)
    data_dir = tmp_path / "milkie-data" / "skillobs"
    data_dir.mkdir(parents=True)
    manifest_path = data_dir / "skill-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    server, handler_cls, fake_port = _start_scripted_server(skill_calls)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    agent_md = _write_agent(tmp_path, fake_port)

    def _build(_name: str):
        env = {
            "OPENAI_API_KEY": "sk-fake",
            "PATH": os.environ.get("PATH", ""),
            SKILL_MANIFEST_ENV: str(manifest_path),
        }
        cmd = [
            _node_bin(), str(cli), "serve",
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
        handle = await provider.create_agent("skillobs", "/ws-skill-obs")
        events = [e async for e in provider.run_turn(handle, "exercise skill observation")]
    finally:
        await provider.shutdown_sidecars()
        server.shutdown()

    return {
        "provider": provider,
        "handle": handle,
        "events": events,
        "handler_cls": handler_cls,
        "skills_root": skills_root,
        "skill_dir": Path(skill_meta["abs_path"]),
        "data_dir": data_dir,
        "tmp_path": tmp_path,
    }


def _assert_no_legacy_trajectory(tmp_path: Path) -> None:
    leftovers = list(tmp_path.rglob("trajectory_*.json"))
    assert leftovers == [], f"legacy trajectory must not exist: {leftovers}"


def _make_recorder(tmp_path: Path, skills_root: Path) -> tuple[SkillLogRecorder, Path, Path]:
    logs_dir = tmp_path / "skill_logs"
    eval_dir = tmp_path / "skill_eval"
    recorder = SkillLogRecorder(
        logs_dir,
        skill_dirs=[skills_root],
        eval_base_dir=eval_dir,
    )
    return recorder, logs_dir, eval_dir


def _record_complete_batch(
    recorder: SkillLogRecorder,
    batch,
    *,
    session_id: str = SESSION_ID,
    context_before: str = "exercise skill observation",
    skill_output: str = "skill-observation-done",
) -> int:
    assert recorder.record_observation_state(
        complete=batch.complete,
        session_id=session_id,
        reason=batch.reason,
        observed_skills=len(batch.skill_names),
    )
    recorded = 0
    for skill_name in batch.skill_names:
        if recorder.maybe_record(
            skill_name,
            session_id=session_id,
            context_before=context_before,
            skill_output=skill_output,
        ):
            recorded += 1
    return recorded


def _eval_context(logs_dir: Path, eval_dir: Path, skills_root: Path, tmp_path: Path):
    context = SimpleNamespace(
        agent_name="skillobs",
        llm=MagicMock(),
        mailbox=SimpleNamespace(deposit=AsyncMock(return_value=None)),
        skill_logs_dir=logs_dir,
        skill_eval_dir=eval_dir,
    )
    udm = MagicMock()
    udm.skill_logs_dir = logs_dir
    udm.skills_dir = skills_root
    udm.repo_skills_dir = skills_root
    udm.sessions_dir = tmp_path / "sessions"
    udm.get_agent_writable_skills_dir.return_value = skills_root
    udm.get_agent_read_skill_dirs.return_value = [skills_root]
    return context, udm


def _healthy_report(segment_count: int = 1) -> EvalReport:
    return EvalReport(
        skill_id=SKILL_NAME,
        skill_version=SKILL_VERSION,
        evaluated_at="2026-08-02T00:00:00+00:00",
        segment_count=segment_count,
        critical_issue_count=0,
        critical_issue_rate=0.0,
        mean_satisfaction=0.9,
        results=[
            JudgeResult(
                segment_index=i,
                has_critical_issue=False,
                satisfaction=0.9,
                reason="e2e fixture ok",
            )
            for i in range(segment_count)
        ],
    )


# ---------------------------------------------------------------------------
# S1 + S2 + S3 (happy path): hit → segment → evaluated → no_changes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s1_s2_s3_hit_segment_evaluate_no_changes(tmp_path, monkeypatch):
    """#187 full loop: one hit sample, evaluate once, second run is no_changes."""
    run = await _run_milkie_turn(tmp_path, monkeypatch, skill_calls=[SKILL_NAME])
    provider, handle = run["provider"], run["handle"]
    skills_root = run["skills_root"]

    # S1: complete batch, only the hit skill, no trajectory.
    batch = provider.get_skill_observations(handle)
    assert batch.complete is True
    assert batch.reason == ""
    assert batch.skill_names == (SKILL_NAME,)
    _assert_no_legacy_trajectory(tmp_path)

    recorder, logs_dir, eval_dir = _make_recorder(tmp_path, skills_root)
    recorded = _record_complete_batch(recorder, batch)
    assert recorded == 1

    segments = SegmentLogger(logs_dir).load(SKILL_NAME)
    assert len(segments) == 1
    seg = segments[0]
    assert seg.skill_id == SKILL_NAME
    assert seg.skill_version == SKILL_VERSION
    assert seg.session_id == SESSION_ID
    assert seg.context_before
    assert seg.skill_output
    assert seg.triggered_at

    state = SegmentLogger(logs_dir).load_observation_state()
    assert state is not None
    assert state["complete"] is True
    assert state["session_id"] == SESSION_ID
    assert state["observed_skills"] == 1

    # S2: Skill Evaluate covers the new sample with matching counts.
    context, udm = _eval_context(logs_dir, eval_dir, skills_root, tmp_path)
    report = _healthy_report(segment_count=1)
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
    judge.assert_awaited_once()

    reports = list(eval_dir.glob(f"{SKILL_NAME}/versions/*/eval_report.json"))
    assert len(reports) == 1
    report_body = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report_body["skill_id"] == SKILL_NAME
    assert report_body["skill_version"] == SKILL_VERSION
    assert report_body["segment_count"] == 1

    # S3a: no new samples → skipped/no_changes, evaluated_count = 0.
    assert second.status == "skipped"
    assert second.reason == "no_changes"
    assert second.detail["evaluated_count"] == 0
    assert second.detail["eligible_count"] == 0


# ---------------------------------------------------------------------------
# S1: provider-level dedupe + miss exclusion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s1_duplicate_hits_dedupe_to_one_observation(tmp_path, monkeypatch):
    """Same skill loaded twice in one turn → batch carries one unique name."""
    run = await _run_milkie_turn(
        tmp_path, monkeypatch, skill_calls=[SKILL_NAME, SKILL_NAME],
    )
    batch = run["provider"].get_skill_observations(run["handle"])
    assert batch.complete is True
    assert batch.skill_names == (SKILL_NAME,)
    assert run["handler_cls"].tool_calls_seen == ["skill_request", "skill_request"]

    recorder, logs_dir, _eval_dir = _make_recorder(tmp_path, run["skills_root"])
    # Cron-style recording iterates unique names only.
    recorded = _record_complete_batch(recorder, batch)
    assert recorded == 1
    assert len(SegmentLogger(logs_dir).load(SKILL_NAME)) == 1
    _assert_no_legacy_trajectory(tmp_path)


@pytest.mark.asyncio
async def test_s1_miss_only_yields_empty_complete_batch(tmp_path, monkeypatch):
    """not_found skill_request must not create an evaluation sample."""
    unknown = "no-such-skill-xyz"
    run = await _run_milkie_turn(
        tmp_path,
        monkeypatch,
        skill_calls=[unknown],
        include_manifest_skill=True,
    )
    batch = run["provider"].get_skill_observations(run["handle"])
    assert batch.complete is True
    assert batch.skill_names == ()
    assert run["handler_cls"].tool_results, "milkie should feed tool result back to LLM"
    miss = run["handler_cls"].tool_results[0]
    assert isinstance(miss, dict)
    assert miss.get("status") == "not_found"

    recorder, logs_dir, eval_dir = _make_recorder(tmp_path, run["skills_root"])
    recorded = _record_complete_batch(recorder, batch)
    assert recorded == 0
    assert SegmentLogger(logs_dir).list_skills() == []
    _assert_no_legacy_trajectory(tmp_path)

    # Empty observation with healthy state → evaluate is no_changes, not success.
    context, udm = _eval_context(logs_dir, eval_dir, run["skills_root"], tmp_path)
    with patch("src.everbot.infra.user_data.get_user_data_manager", return_value=udm):
        outcome = await run_skill_evaluate(context)
    assert outcome.status == "skipped"
    assert outcome.reason == "no_changes"
    assert outcome.detail["evaluated_count"] == 0
    assert outcome.detail["observed_count"] == 0


# ---------------------------------------------------------------------------
# S3: observation unavailable must not look like evaluated success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s3_incomplete_observation_state_is_degraded(tmp_path):
    """Persisted incomplete observation → degraded/observation_unavailable."""
    logs_dir = tmp_path / "skill_logs"
    eval_dir = tmp_path / "skill_eval"
    skills_root = tmp_path / "skills"
    skills_root.mkdir()

    # Even if stale segments exist, incomplete observation wins.
    recorder = SkillLogRecorder(logs_dir, skill_dirs=[skills_root], eval_base_dir=eval_dir)
    # Write a decoy segment without going through a complete observation.
    (skills_root / SKILL_NAME).mkdir()
    (skills_root / SKILL_NAME / "SKILL.md").write_text(
        f"---\nname: {SKILL_NAME}\nversion: \"{SKILL_VERSION}\"\n---\n# Web\n",
        encoding="utf-8",
    )
    assert recorder.maybe_record(
        SKILL_NAME,
        session_id="stale-session",
        context_before="stale",
        skill_output="stale-output",
    )
    assert recorder.record_observation_state(
        complete=False,
        session_id="routine-broken",
        reason="terminal_not_seen",
        observed_skills=0,
    )

    context, udm = _eval_context(logs_dir, eval_dir, skills_root, tmp_path)
    with patch("src.everbot.infra.user_data.get_user_data_manager", return_value=udm), patch(
        "src.everbot.core.jobs.skill_evaluate.evaluate_skill",
        new=AsyncMock(),
    ) as judge:
        outcome = await run_skill_evaluate(context)

    assert outcome.status == "degraded"
    assert outcome.reason == "observation_unavailable"
    assert outcome.detail["evaluated_count"] == 0
    assert outcome.detail["eligible_count"] == 0
    assert outcome.detail["observed_count"] == 0
    assert outcome.detail["observation_reason"] == "terminal_not_seen"
    assert outcome.detail["observation_session_id"] == "routine-broken"
    judge.assert_not_awaited()
    assert list(eval_dir.glob(f"{SKILL_NAME}/versions/*/eval_report.json")) == []


@pytest.mark.asyncio
async def test_s3_empty_skill_logs_is_no_changes_not_completed(tmp_path):
    """Healthy but empty skill_logs must be skipped/no_changes."""
    logs_dir = tmp_path / "skill_logs"
    logs_dir.mkdir()
    eval_dir = tmp_path / "skill_eval"
    skills_root = tmp_path / "skills"
    skills_root.mkdir()

    # Explicit complete/zero observation health (cron writes this after empty batch).
    SegmentLogger(logs_dir).save_observation_state(
        complete=True,
        session_id="routine-empty",
        observed_skills=0,
    )

    context, udm = _eval_context(logs_dir, eval_dir, skills_root, tmp_path)
    with patch("src.everbot.infra.user_data.get_user_data_manager", return_value=udm):
        outcome = await run_skill_evaluate(context)

    assert outcome.status == "skipped"
    assert outcome.reason == "no_changes"
    assert outcome.detail == {
        "observed_count": 0,
        "eligible_count": 0,
        "evaluated_count": 0,
    }
