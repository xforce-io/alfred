"""真实 LLM 端到端验证(milkie 能否接管生产)—— 删 dolphin 的前置门槛(#38)。

需真 key + 网络,默认 skip;显式 opt-in 才跑:
    MILKIE_E2E_REAL=1 ZHIPU_API_KEY=... PYTHONPATH=. .venv/bin/python -m pytest \
        tests/e2e/test_milkie_real_llm.py -q -s

验收(两者皆过 = milkie 可接管生产):
  ① 真模型逐 token 流式文本回复;
  ② 真 LLM **自主**调内建 run_command 跑真实命令,并把输出带回答复。

用 zhipu glm-4-flash(OpenAI 兼容、支持 function calling、key 易得)。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.everbot.core.agent.provider.milkie.provider import MilkieAgentHandle, MilkieProvider
from src.everbot.core.agent.provider.milkie.sidecar import MilkieSidecar

_CLI = Path(__file__).resolve().parents[2].parent / "milkie" / "dist" / "cli" / "index.js"
_BASE = "https://open.bigmodel.cn/api/paas/v4"
_MODEL = "glm-4-flash"

pytestmark = pytest.mark.skipif(
    not (os.environ.get("MILKIE_E2E_REAL") and os.environ.get("ZHIPU_API_KEY") and _CLI.exists()),
    reason="real-LLM e2e: set MILKIE_E2E_REAL=1 + ZHIPU_API_KEY + build milkie dist",
)


def _agent_md(tmp: Path) -> Path:
    md = tmp / "real.md"
    md.write_text(
        "---\n"
        "agentId: realagent\nversion: 1.0.0\n"
        "fsm:\n  states:\n    - name: react\n      type: llm\n      max_iterations: 6\n"
        "      instructions: 回答用户;需要时用 run_command 执行 shell 命令\n"
        f"model:\n  provider: zhipu\n  model: {_MODEL}\n  adapter: openai-compatible\n  baseUrl: {_BASE}\n"
        "---\n你是助手,可以用 run_command 执行 shell 命令。\n",
        encoding="utf-8",
    )
    return md


async def _text(provider, handle, msg) -> str:
    chunks = []
    async for e in provider.run_turn(handle, msg):
        for item in e.get("_progress", []):
            if item.get("stage") == "llm" and item.get("delta"):
                chunks.append(item["delta"])
    return "".join(chunks)


async def test_milkie_takes_over_with_real_llm(tmp_path):
    env = dict(os.environ)
    env.pop("VOLCENGINE_TOKEN", None)  # 防抢占(见 launcher 同款修复)
    env.pop("VOLCENGINE_API_BASE", None)
    env["OPENAI_API_KEY"] = os.environ["ZHIPU_API_KEY"]

    sidecar = MilkieSidecar(
        ["node", str(_CLI), "serve", "--agent", str(_agent_md(tmp_path)), "--port", "0"],
        env=env, ready_timeout=30.0,
    )
    await sidecar.start()
    try:
        provider = MilkieProvider(sidecar.base_url)
        # ① 真流式文本
        a1 = await _text(provider, MilkieAgentHandle(sidecar.base_url, "c1", name="realagent"),
                         "用一句话回答:1+1 等于几?")
        assert a1.strip() and ("2" in a1 or "二" in a1), a1
        # ② 真 LLM 自主调 run_command
        a2 = await _text(provider, MilkieAgentHandle(sidecar.base_url, "c2", name="realagent"),
                         "请用 run_command 执行 `echo MILKIE_REAL_OK_4242`,然后把命令输出原样告诉我。")
        assert "MILKIE_REAL_OK_4242" in a2, a2
    finally:
        await sidecar.close()
