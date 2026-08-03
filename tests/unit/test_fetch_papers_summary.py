"""#193 P0a — paper-discovery summary must use resolve_model, not dolphin/qwen-turbo."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


_SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "paper-discovery" / "scripts"
_SCRIPT = _SCRIPTS / "fetch_papers.py"


def _load_fetch_papers():
    name = "fetch_papers_under_test"
    # Fresh load so patches apply to the module under test.
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_fetch_papers_source_has_no_dolphin_or_qwen_hardcode():
    src = _SCRIPT.read_text(encoding="utf-8")
    assert "import dolphin" not in src
    assert "from dolphin" not in src
    assert "dolphin.yaml" not in src
    assert "qwen-turbo" not in src
    assert "GlobalConfig" not in src
    assert "mf_chat_stream" not in src
    assert "resolve_model" in src


def test_generate_one_line_summary_uses_resolve_model_fast_tier(monkeypatch):
    fp = _load_fetch_papers()

    route = SimpleNamespace(
        model="doubao-seed-fake",
        api_key="sk-test",
        base_url="https://example.test/v1",
        headers={},
        extra_body={},
    )
    resolved = SimpleNamespace(route=route, logical_name="doubao-nothink", source="system_fast")

    mock_resolve = MagicMock(return_value=resolved)
    monkeypatch.setattr(
        "src.everbot.core.agent.provider.model_config.resolve_model",
        mock_resolve,
        raising=False,
    )
    # Script imports resolve_model into its own namespace after load; patch both.
    if hasattr(fp, "resolve_model"):
        monkeypatch.setattr(fp, "resolve_model", mock_resolve)

    class _Resp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "  提出共享记忆的多智能体协调方法。  "}}]}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            self.url = url
            self.json_payload = json
            self.headers = headers
            return _Resp()

    with patch.object(fp, "httpx") as httpx_mod:
        httpx_mod.AsyncClient = _Client
        httpx_mod.Timeout = lambda *a, **k: None
        out = fp.generate_one_line_summary(
            "We propose multi-agent coordination via shared memory."
        )

    assert out == "提出共享记忆的多智能体协调方法。"
    mock_resolve.assert_called()
    kwargs = mock_resolve.call_args.kwargs
    assert kwargs.get("tier") == "fast"
    # Must not hardcode qwen; agent env may be None or EVERBOT_AGENT.
    assert kwargs.get("override") in (None, "") or "qwen" not in str(kwargs.get("override"))


def test_generate_one_line_summary_degrades_on_resolve_failure(monkeypatch, capsys):
    fp = _load_fetch_papers()

    def _boom(**kwargs):
        raise RuntimeError("no model config")

    if hasattr(fp, "resolve_model"):
        monkeypatch.setattr(fp, "resolve_model", _boom)
    else:
        monkeypatch.setattr(
            "src.everbot.core.agent.provider.model_config.resolve_model",
            _boom,
            raising=False,
        )

    out = fp.generate_one_line_summary("Some abstract about transformers.")
    assert out == ""
    err = capsys.readouterr().err
    assert "Failed to generate summary" in err
    assert "qwen-turbo" not in err
