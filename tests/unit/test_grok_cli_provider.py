"""Host-spawned grok CLI provider: argv, env scrub, JSON text → _progress."""
from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from src.everbot.core.agent.provider import get_provider_for_agent, provider_for, reset_provider
from src.everbot.core.agent.provider.grok_cli.invoke import (
    _has_terminal_json,
    default_grok_runner,
    grok_json_to_progress,
    invoke_grok,
)
from src.everbot.core.agent.provider.grok_cli.provider import GrokCliProvider


def _fake_runner_factory(recorded: dict, payload: dict | None = None):
    body = json.dumps(payload if payload is not None else {"text": "pong"})

    def fake_runner(cmd, args, *, cwd, env, stdout_file, timeout=None):
        recorded["cmd"] = cmd
        recorded["args"] = list(args)
        recorded["cwd"] = str(cwd)
        recorded["env"] = dict(env)
        recorded["stdout_file"] = str(stdout_file)
        recorded["timeout"] = timeout
        prompt_arg = args[args.index("--prompt-file") + 1]
        recorded["prompt"] = (Path(cwd) / prompt_arg).read_text(encoding="utf-8")
        Path(stdout_file).write_text(body, encoding="utf-8")

    return fake_runner


def test_invoke_grok_headless_argv_and_scrubs_xai_key(tmp_path, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-must-not-leak")
    recorded: dict = {}
    data = invoke_grok(
        "Reply with exactly: pong",
        cwd=tmp_path,
        model="grok-4.6",
        runner=_fake_runner_factory(recorded),
    )
    assert recorded["cmd"] == "grok"
    args = recorded["args"]
    assert "--output-format" in args
    assert "json" in args
    assert "--prompt-file" in args or "-p" in args
    assert "XAI_API_KEY" not in recorded["env"]
    assert data.get("text") == "pong"
    items = grok_json_to_progress(data)
    assert items
    assert items[0]["stage"] == "llm"
    assert "pong" in (items[0].get("delta") or items[0].get("answer") or "")


@pytest.mark.asyncio
async def test_run_turn_maps_json_text_to_progress(tmp_path, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-must-not-leak")
    recorded: dict = {}
    provider = GrokCliProvider(runner=_fake_runner_factory(recorded))
    (tmp_path / "SOUL.md").write_text("You are a test agent.", encoding="utf-8")
    handle = await provider.create_agent("demo_agent", tmp_path)
    events = []
    async for ev in provider.run_turn(handle, "Reply with exactly: pong"):
        events.append(ev)
    assert recorded["cmd"] == "grok"
    assert "--output-format" in recorded["args"]
    assert "json" in recorded["args"]
    assert "--prompt-file" in recorded["args"] or "-p" in recorded["args"]
    assert "XAI_API_KEY" not in recorded["env"]
    texts = []
    for ev in events:
        for item in ev.get("_progress") or []:
            texts.append(item.get("delta") or "")
            texts.append(item.get("answer") or "")
    joined = "".join(texts)
    assert "pong" in joined


def test_concurrent_invocations_use_isolated_prompt_and_stdout_files(tmp_path):
    calls = []
    ready = Barrier(2)

    def runner(cmd, args, *, cwd, env, stdout_file, timeout=None):
        prompt_arg = args[args.index("--prompt-file") + 1]
        calls.append((Path(cwd) / prompt_arg, Path(stdout_file)))
        ready.wait(timeout=1)
        prompt = (Path(cwd) / prompt_arg).read_text(encoding="utf-8")
        Path(stdout_file).write_text(json.dumps({"text": prompt}), encoding="utf-8")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(invoke_grok, text, cwd=tmp_path, runner=runner)
            for text in ("first", "second")
        ]
        results = [future.result(timeout=2)["text"] for future in futures]

    assert results == ["first", "second"]
    assert calls[0][0] != calls[1][0]
    assert calls[0][1] != calls[1][1]


@pytest.mark.parametrize("payload", [{"text": "pong"}, {"type": "error", "message": "upstream"}])
def test_runner_stops_process_group_after_terminal_json(tmp_path, monkeypatch, payload):
    script = tmp_path / "fake-grok"
    pid_file = tmp_path / "pids"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, subprocess, time\n"
        "child = subprocess.Popen(['/bin/sh', '-c', \"trap '' TERM; sleep 30\"])\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(f'{{os.getpid()}} {{child.pid}}')\n"
        f"print({json.dumps(payload)!r}, flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setattr(
        "src.everbot.core.agent.provider.grok_cli.invoke.resolve_grok_executable",
        lambda _cmd: str(script),
    )
    stdout_file = tmp_path / "out.json"

    default_grok_runner(
        "grok", [], cwd=tmp_path, env={}, stdout_file=stdout_file, timeout=5
    )

    assert json.loads(stdout_file.read_text(encoding="utf-8")) == payload
    for pid in map(int, pid_file.read_text().split()):
        state = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
        ).stdout.strip()
        assert not state or state.startswith("Z")


def test_terminal_json_tolerates_partial_utf8(tmp_path):
    stdout_file = tmp_path / "out.json"
    stdout_file.write_bytes(b'{"text":"\xe4')
    assert _has_terminal_json(stdout_file) is False
    stdout_file.write_text('{"text":"你"}', encoding="utf-8")
    assert _has_terminal_json(stdout_file) is True


def test_runner_timeout_cleans_process(tmp_path, monkeypatch):
    script = tmp_path / "silent-grok"
    pid_file = tmp_path / "pid"
    script.write_text(
        "#!/bin/sh\n"
        f"echo $$ > {pid_file}\n"
        "sleep 30\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setattr(
        "src.everbot.core.agent.provider.grok_cli.invoke.resolve_grok_executable",
        lambda _cmd: str(script),
    )

    with pytest.raises(RuntimeError, match="timeout"):
        default_grok_runner(
            "grok", [], cwd=tmp_path, env={}, stdout_file=tmp_path / "out.json", timeout=0.1
        )
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


def test_grok_cli_skips_dolphin_history_restore():
    assert GrokCliProvider().needs_history_restore() is False


@pytest.mark.asyncio
async def test_probe_llm_grok_cli_skips_http(monkeypatch, tmp_path):
    from src.everbot.core.runtime.heartbeat import HeartbeatRunner
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    runner = HeartbeatRunner(
        agent_name="demo_agent",
        workspace_path=tmp_path,
        session_manager=SimpleNamespace(
            get_primary_session_id=lambda n: f"web_session_{n}",
            get_heartbeat_session_id=lambda n: f"heartbeat_session_{n}",
        ),
        agent_factory=AsyncMock(),
        interval_minutes=1,
        active_hours=(0, 24),
        max_retries=3,
        on_result=None,
    )
    monkeypatch.setattr(
        "src.everbot.core.agent.provider._agent_runtime",
        lambda name, everbot_cfg=None: "grok-cli",
    )
    monkeypatch.setattr(
        "src.everbot.core.agent.provider.grok_cli.invoke.grok_cli_available",
        lambda: True,
    )
    called = []

    async def boom(*_a, **_k):
        called.append(1)
        raise AssertionError("HTTP complete must not run for grok-cli probe")

    monkeypatch.setattr(
        "src.everbot.core.runtime.heartbeat._SkillLLMClient.complete",
        boom,
    )
    assert await runner._probe_llm() is True
    assert called == []


@pytest.mark.asyncio
async def test_provider_for_real_handle_skips_executor_paths(tmp_path):
    """Shipped restore/cron/heartbeat gates skip agent.executor on a real handle."""
    reset_provider()
    (tmp_path / "SOUL.md").write_text("You are a test agent.", encoding="utf-8")
    created = GrokCliProvider(runner=_fake_runner_factory({}))
    handle = await created.create_agent("demo_agent", tmp_path)

    routed = provider_for(handle)
    assert type(routed).__name__ == "GrokCliProvider"
    assert routed.needs_history_restore() is False
    assert getattr(handle, "executor", None) is None

    from src.everbot.core.session.persistence import SessionPersistence
    from src.everbot.core.session.session_data import SessionData

    persistence = SessionPersistence(sessions_dir=str(tmp_path / "sessions"))
    session = SessionData(
        session_id="heartbeat_session_demo_agent",
        agent_name="demo_agent",
        model_name="grok-4.6",
        session_type="heartbeat",
        history_messages=[{"role": "user", "content": "hi"}],
        variables={},
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
    )
    await persistence.restore_to_agent(handle, session)
    restored = handle.variables.get("history_messages") or []
    assert restored and restored[0].get("content") == "hi"

    from src.everbot.core.runtime.cron import CronExecutor
    from src.everbot.core.tasks.task_manager import Task

    prompt = CronExecutor._build_job_system_prompt(
        handle, Task(id="t1", title="t", description="do the thing")
    )
    assert "do the thing" in prompt
    reset_provider()


def test_get_provider_for_agent_routes_grok_cli(monkeypatch):
    reset_provider()
    import src.everbot.core.agent.provider as mod

    monkeypatch.setattr(
        mod,
        "_load_everbot_cfg",
        lambda: {
            "agents": {
                "demo_agent": {"runtime": "grok-cli"},
                "coding-master": {"model": "deepseek-chat"},
            }
        },
    )
    grok = get_provider_for_agent("demo_agent")
    milkie = get_provider_for_agent("coding-master")
    assert type(grok).__name__ == "GrokCliProvider"
    assert type(milkie).__name__ == "MilkieProvider"
    handle = type("H", (), {"runtime": "grok-cli", "name": "demo_agent"})()
    assert provider_for(handle) is grok
    reset_provider()


@pytest.mark.asyncio
async def test_run_turn_includes_prior_history(tmp_path):
    recorded: dict = {}
    provider = GrokCliProvider(runner=_fake_runner_factory(recorded))
    (tmp_path / "SOUL.md").write_text("You are a test agent.", encoding="utf-8")
    handle = await provider.create_agent("demo_agent", tmp_path)
    handle.variables["history_messages"] = [
        {"role": "user", "content": "my name is Ada"},
        {"role": "assistant", "content": "hi Ada"},
    ]
    async for _ in provider.run_turn(handle, "what is my name?"):
        pass
    prompt = recorded["prompt"]
    assert "my name is Ada" in prompt
    assert "what is my name?" in prompt
    hist = handle.variables["history_messages"]
    assert hist[-1]["role"] == "assistant"
    assert "pong" in (hist[-1].get("content") or "")


@pytest.mark.asyncio
async def test_oneshot_grok_cli_does_not_http(monkeypatch, tmp_path):
    from src.everbot.core.agent.provider.oneshot_llm import OneshotLLMProvider

    http = []

    def fake_oneshot(prompt, *, cwd, model="", runner=None):
        return "pong"

    monkeypatch.setattr(
        "src.everbot.core.agent.provider.agent_uses_grok_cli",
        lambda name, everbot_cfg=None: name == "demo_agent",
    )
    monkeypatch.setattr(
        "src.everbot.core.agent.agent_config.resolve_agent_model",
        lambda name: "grok-4.6",
    )
    monkeypatch.setattr(
        "src.everbot.core.agent.provider.grok_cli.invoke.grok_oneshot_text",
        fake_oneshot,
    )

    async def boom(*_a, **_k):
        http.append(1)
        raise AssertionError("oneshot must not HTTP for grok-cli")

    monkeypatch.setattr("httpx.AsyncClient.post", boom)
    out = await OneshotLLMProvider().call_llm(None, "ping", agent_name="demo_agent")
    assert out == "pong"
    assert http == []


@pytest.mark.asyncio
async def test_skill_complete_grok_cli_skips_volcengine(monkeypatch):
    from src.everbot.core.runtime.heartbeat import _SkillLLMClient

    monkeypatch.setattr(
        "src.everbot.core.agent.provider.agent_uses_grok_cli",
        lambda name, everbot_cfg=None: True,
    )
    monkeypatch.setenv("EVERBOT_AGENT", "demo_agent")
    monkeypatch.setattr(
        "src.everbot.core.agent.provider.grok_cli.invoke.grok_oneshot_text",
        lambda prompt, *, cwd, model="", runner=None: "pong",
    )
    routed = []

    def boom_route(model):
        routed.append(model)
        raise AssertionError("must not resolve volcengine route")

    monkeypatch.setattr(
        "src.everbot.core.runtime.heartbeat._resolve_skill_model_route",
        boom_route,
    )
    client = _SkillLLMClient(model="doubao-nothink")
    out = await client.complete("score this")
    assert out == "pong"
    assert routed == []


def test_cron_skill_model_grok_cli_not_volcengine(monkeypatch):
    from src.everbot.core.runtime.cron import CronExecutor

    monkeypatch.setattr(
        "src.everbot.core.agent.provider.agent_uses_grok_cli",
        lambda name, everbot_cfg=None: name == "demo_agent",
    )
    monkeypatch.setattr(
        "src.everbot.core.agent.agent_config.resolve_agent_model",
        lambda name: "grok-4.6",
    )
    ex = CronExecutor.__new__(CronExecutor)
    ex.agent_name = "demo_agent"
    assert ex._resolve_skill_model() == "grok-4.6"
