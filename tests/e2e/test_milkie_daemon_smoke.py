"""E2E:MilkieProvider 经 SidecarPool 真 spawn serve → create_agent 拿到 handle →
run_turn 跑通逐 token → shutdown_sidecars 后子进程已退出。需 ../milkie 已 build。"""
import os
from pathlib import Path

import pytest

from everbot.core.agent.provider.milkie.pool import SidecarPool
from everbot.core.agent.provider.milkie.provider import MilkieProvider
from everbot.core.agent.provider.milkie.agent_spec import build_milkie_model_tiers, build_milkie_agent_md

CLI = Path(__file__).resolve().parents[2].parent / "milkie" / "dist" / "cli" / "index.js"


@pytest.mark.skipif(not CLI.exists(), reason="milkie dist not built")
async def test_pool_spawn_create_agent_and_shutdown(tmp_path, fake_openai_port):
    base = f"http://127.0.0.1:{fake_openai_port}"
    llms = {"fake": {"cloud": "fc", "model_name": "fake-model", "type_api": "openai"}}
    clouds = {"fc": {"api": base, "api_key": "sk-fake"}}
    tiers = build_milkie_model_tiers(llms, clouds, default="fake", fast="fake")
    data_dir = tmp_path / "alice"
    data_dir.mkdir()
    agent_md = data_dir / "agent.md"
    agent_md.write_text(build_milkie_agent_md("alice", "You are Alice.", tiers), encoding="utf-8")

    def _build(name):
        return (["node", str(CLI), "serve", "--agent", str(agent_md), "--port", "0",
                 "--state-store", "sqlite", "--data-dir", str(data_dir)],
                {"OPENAI_API_KEY": "sk-fake", "PATH": os.environ.get("PATH", "")})

    pool = SidecarPool(build=_build)
    prov = MilkieProvider.__new__(MilkieProvider)
    prov._base_url = None
    prov._client = None
    prov._sync_client = None
    prov._pool = pool

    handle = await prov.create_agent("alice", workspace_path=str(data_dir))
    assert handle.base_url.startswith("http://127.0.0.1:")
    sidecar = pool._sidecars["alice"]

    deltas = []
    async for ev in prov.run_turn(handle, "hi"):
        for item in ev.get("_progress", []):
            # MilkieProvider adapter 把 milkie message_delta → {"stage":"llm","delta":...}
            # (中立 _progress 契约,非 TurnEvent),故按 stage/delta 判,而非 type==LLM_DELTA。
            if item.get("stage") == "llm" and item.get("delta"):
                deltas.append(item)
    assert len(deltas) >= 1

    await prov.shutdown_sidecars()
    assert sidecar.returncode is not None   # 子进程已退出
