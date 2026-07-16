"""#127/#124 P3 — twitter-watch L2(a):分析报告须逐条带推文原文链接。

tweet 的 url 本就进了喂给 LLM 的数据,但分析 prompt 没要求报告**带上**逐条
原文链接 → 生成的报告会漏。补 prompt 指令,让 Serenity 这类报告可溯源到单条推文。
"""
import importlib.util
import asyncio
import json
import sys
from pathlib import Path

import httpx

_SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "twitter-watch" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


az = _load("analyze")


def _data():
    return {
        "handle": "aleabitoreddit",
        "tweets": [
            {"text": "$XFAB gets EU chip subsidy", "ts": "2026-06-24",
             "url": "https://x.com/aleabitoreddit/status/123"},
        ],
    }


def test_tweet_url_reaches_llm_prompt():
    """数据层:推文 url 进入喂给 LLM 的 prompt(供其引用)。"""
    prompt = az.build_prompt(_data())
    assert "https://x.com/aleabitoreddit/status/123" in prompt


def test_prompt_requires_per_tweet_source_link():
    """指令层:prompt 明确要求报告逐条带原文链接(L2a 可溯源)。"""
    prompt = az.build_prompt(_data())
    assert "原文链接" in prompt


def test_analyze_uses_configured_openai_compatible_route(monkeypatch):
    """twitter-watch analysis should not shell out to Claude CLI."""
    from src.everbot.core.agent.provider.model_config import ModelRoute, ResolvedModel

    resolved = ResolvedModel(
        logical_name="m",
        route=ModelRoute(base_url="http://fake/v1", api_key="k", model="mm"),
        source="system_default",
    )
    monkeypatch.setattr(az, "resolve_model", lambda **k: resolved)
    cap = {}

    def handler(req: httpx.Request) -> httpx.Response:
        cap["url"] = str(req.url)
        cap["auth"] = req.headers.get("authorization")
        cap["json"] = json.loads(req.content.decode("utf-8"))
        return httpx.Response(200, json={"choices": [{"message": {"content": " 报告 "}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(az.httpx, "AsyncClient", lambda *a, **k: client)

    out = asyncio.run(az.run_analysis("prompt", None, 30))

    assert out == "报告"
    assert cap["url"] == "http://fake/v1/chat/completions"
    assert cap["auth"] == "Bearer k"
    assert cap["json"]["model"] == "mm"
    assert cap["json"]["messages"][0]["content"] == "prompt"


def test_analyze_passes_agent_name_into_resolve_model(monkeypatch):
    """#155: agent context must be forwarded so default is not models.yaml top-level."""
    from src.everbot.core.agent.provider.model_config import ModelRoute, ResolvedModel

    seen = {}

    def fake_resolve(**kwargs):
        seen.update(kwargs)
        return ResolvedModel(
            logical_name="deepseek-volcengine",
            route=ModelRoute(base_url="http://fake/v1", api_key="k", model="glm-5.2"),
            source="agent",
        )

    monkeypatch.setattr(az, "resolve_model", fake_resolve)

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(az.httpx, "AsyncClient", lambda *a, **k: client)

    asyncio.run(az.run_analysis("p", None, 30, agent_name="demo_agent"))
    assert seen.get("agent_name") == "demo_agent"
    assert seen.get("override") is None


def test_analyze_script_no_longer_references_claude_cli():
    src = (_SCRIPTS / "analyze.py").read_text(encoding="utf-8")
    assert '"claude"' not in src
    assert "claude -p" not in src
