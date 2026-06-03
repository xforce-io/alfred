"""alfred agent 配置 → ``milkie serve`` 命令 + env + data-dir(纯函数)。

复用 :mod:`agent_spec` 生成 agent.md(两档 model);per-cloud api_key 注入 env
(milkie OpenAICompatibleAdapter 仅从 env 读 key)。data-dir 预建(SQLiteStore 不自建)。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from .agent_spec import build_milkie_agent_md, build_milkie_model_tiers


@dataclass
class LaunchSpec:
    cmd: List[str]
    env: Dict[str, str]
    data_dir: Path
    agent_md: Path


class SidecarLauncher:
    def __init__(
        self,
        *,
        dist_path: Path,
        data_dir_root: Path,
        node_bin: str,
        llms: Dict[str, Any],
        clouds: Dict[str, Any],
        default_model: str,
        fast_model: str,
    ) -> None:
        self._dist_path = Path(dist_path)
        self._data_dir_root = Path(data_dir_root)
        self._node_bin = node_bin
        self._llms = llms
        self._clouds = clouds
        self._default_model = default_model
        self._fast_model = fast_model

    def build(self, agent_name: str, *, system_prompt: str) -> LaunchSpec:
        tiers = build_milkie_model_tiers(
            self._llms, self._clouds, default=self._default_model, fast=self._fast_model
        )  # 未知 model → KeyError(fail fast)
        data_dir = (self._data_dir_root / agent_name).expanduser()
        data_dir.mkdir(parents=True, exist_ok=True)
        agent_md = data_dir / "agent.md"
        agent_md.write_text(
            build_milkie_agent_md(agent_name, system_prompt, tiers), encoding="utf-8"
        )
        cmd = [
            self._node_bin, str(self._dist_path.expanduser()), "serve",
            "--agent", str(agent_md), "--port", "0",
            "--state-store", "sqlite", "--data-dir", str(data_dir),
        ]
        env = dict(os.environ)
        default_cloud = self._llms[self._default_model]["cloud"]
        api_key = self._clouds[default_cloud].get("api_key")
        if api_key:
            env["OPENAI_API_KEY"] = api_key
        return LaunchSpec(cmd=cmd, env=env, data_dir=data_dir, agent_md=agent_md)
