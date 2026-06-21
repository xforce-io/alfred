"""alfred agent 配置 → ``milkie serve`` 命令 + env + data-dir(纯函数)。

复用 :mod:`agent_spec` 生成 agent.md(两档 model);per-cloud api_key 注入 env
(milkie OpenAICompatibleAdapter 仅从 env 读 key)。data-dir 预建(SQLiteStore 不自建)。
"""
from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .agent_spec import build_milkie_agent_md, build_milkie_model_tiers

logger = logging.getLogger(__name__)


# skill_list manifest(milkie #139):写在 milkie data-dir,经 MILKIE_SKILL_MANIFEST
# env 告知 serve;milkie 默认 handler 读它返回真实技能列表(去 stub)。
SKILL_MANIFEST_FILENAME = "skill-manifest.json"
SKILL_MANIFEST_ENV = "MILKIE_SKILL_MANIFEST"

# E2b(#108):sidecar OS 沙箱 —— launcher 给 milkie serve 套 macOS sandbox-exec,
# 物理禁止子进程(含 run_command fork 的 shell)写共享/系统路径,半径锁在自身 workspace。
SANDBOX_PROFILE_FILENAME = "sandbox.sb"


def _is_darwin() -> bool:
    """是否 macOS —— sandbox-exec 仅此可用(测试 seam,可 monkeypatch)。"""
    return sys.platform == "darwin"


def build_sandbox_profile(*, alfred_root: Path, agent_workspace: Path) -> str:
    """生成 macOS seatbelt profile:默认放行 + 黑名单禁写共享路径,放行自身 workspace。

    爆炸半径控制(#103 E2):
    - 禁写 ``<root>/skills``(全局 skill,半径=所有 agent);
    - 禁写 ``<root>/agents`` 整树(隔离其他 agent),再**放行自身 workspace**(在该树下);
    - 禁写 ``<root>/config.yaml``。

    **必须 realpath**:seatbelt ``subpath`` 按真实路径匹配,``/tmp``→``/private/tmp`` 之类
    软链会让规则静默失效(spike 实测踩到)。``allow`` 自身 workspace 排在 ``deny`` agents
    之后 —— seatbelt last-match-wins,后者覆盖前者。
    """
    rp = lambda p: os.path.realpath(str(p))
    skills = rp(Path(alfred_root) / "skills")
    agents = rp(Path(alfred_root) / "agents")
    config = rp(Path(alfred_root) / "config.yaml")
    ws = rp(agent_workspace)
    return "\n".join([
        "(version 1)",
        "(allow default)",
        f'(deny file-write* (subpath "{skills}"))',
        f'(deny file-write* (subpath "{agents}"))',
        f'(allow file-write* (subpath "{ws}"))',
        f'(deny file-write* (literal "{config}"))',
        "",
    ])


def _render_skill_manifest(skills: List[Dict[str, Any]]) -> Dict[str, Any]:
    """discover_skills 结果 → skill_list manifest(milkie #139 定稿 schema)。

    v1 每条目写 ``{name, description, dir}``(``dir`` = discover_skills 的
    ``abs_path``);``version`` 留空待 s-010。milkie 消费 name/description,``dir``
    为宿主附加字段(milkie handler 原样透传)。
    """
    return {
        "skills": [
            {
                "name": s["name"],
                "description": s.get("description", ""),
                "dir": s["abs_path"],
            }
            for s in skills
        ]
    }


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
        sandbox_enabled: bool = False,
        alfred_root: Optional[Path] = None,
    ) -> None:
        self._dist_path = Path(dist_path)
        self._data_dir_root = Path(data_dir_root)
        self._node_bin = node_bin
        self._llms = llms
        self._clouds = clouds
        self._default_model = default_model
        self._fast_model = fast_model
        # E2b(#108):默认关(灰度),由 everbot.security.sidecar_sandbox 开。
        self._sandbox_enabled = sandbox_enabled
        self._alfred_root = Path(alfred_root) if alfred_root is not None else None

    def build(
        self,
        agent_name: str,
        *,
        system_prompt: str,
        skills: Optional[List[Dict[str, Any]]] = None,
        default_model: str | None = None,
        agent_workspace: Optional[Path] = None,
        sandbox_enabled: Optional[bool] = None,
    ) -> LaunchSpec:
        # default_model:per-agent 模型覆盖(everbot.agents.<name>.model)。缺省回退全局默认。
        # 不传则**所有 agent 用同一全局模型**——会无视 per-agent 配置(实测踩到:demo_agent
        # 配 deepseek-volcengine 却被用成全局 kimi-code)。
        default = default_model or self._default_model
        tiers = build_milkie_model_tiers(
            self._llms, self._clouds, default=default, fast=self._fast_model
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
        default_cloud = self._llms[default]["cloud"]  # per-agent 模型的 cloud(决定 key/VOLCENGINE 处理)
        api_key = self._clouds[default_cloud].get("api_key")
        if api_key:
            # 展开 ${ENV}:原样写字面 ${...} 会让 milkie 拿到坏 key(非 volcengine cloud → 401)。
            env["OPENAI_API_KEY"] = os.path.expandvars(api_key)
        # milkie GatewayFactory 取 key 顺序 = VOLCENGINE_TOKEN ?? OPENAI_API_KEY。
        # 若部署环境带 VOLCENGINE_TOKEN 而本 agent 不是 volcengine,它会抢占我们设的
        # OPENAI_API_KEY → 拿错 key 打目标端点(401)。故非 volcengine 时清掉这俩。
        if default_cloud != "volcengine":
            env.pop("VOLCENGINE_TOKEN", None)
            env.pop("VOLCENGINE_API_BASE", None)
        # skill_list manifest(milkie #139):skills 非 None 即产出(含空列表 → configured
        # but empty)。skills is None(注入式 loader / reflector)→ 不写、不设 env,milkie
        # 侧据缺失 degrade(registryConfigured:false)。与 prompt 技能段同源(provider 侧
        # 单次 discover_skills)。
        if skills is not None:
            manifest_path = data_dir / SKILL_MANIFEST_FILENAME
            manifest_path.write_text(
                json.dumps(_render_skill_manifest(skills), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            env[SKILL_MANIFEST_ENV] = str(manifest_path)
        # per-agent override(#112):build 传 sandbox_enabled 则覆盖构造默认;None → 沿用默认。
        enabled = self._sandbox_enabled if sandbox_enabled is None else sandbox_enabled
        cmd = self._maybe_sandbox(cmd, agent_name, data_dir, agent_workspace, enabled)
        return LaunchSpec(cmd=cmd, env=env, data_dir=data_dir, agent_md=agent_md)

    def _maybe_sandbox(
        self,
        cmd: List[str],
        agent_name: str,
        data_dir: Path,
        agent_workspace: Optional[Path],
        enabled: bool,
    ) -> List[str]:
        """E2b(#108):启用且 darwin 时,写 per-agent profile 并用 sandbox-exec 包裹 cmd。

        非 darwin → 跳过 + WARNING(暂不支持,Linux 留后续 bwrap/namespaces);未配 alfred_root
        → 无从定位受保护路径,跳过 + WARNING。灰度默认关(``sandbox_enabled=False``)。
        ``enabled`` 由调用方解析(per-agent override > 构造默认),见 :meth:`build`。
        """
        if not enabled:
            return cmd
        if not _is_darwin():
            logger.warning(
                "sidecar_sandbox 已启用但当前平台非 macOS(%s)——跳过 sandbox-exec 包裹"
                "(暂不支持,Linux 待 bwrap/namespaces)。", sys.platform,
            )
            return cmd
        if self._alfred_root is None:
            logger.warning("sidecar_sandbox 已启用但未配 alfred_root —— 无法定位受保护路径,跳过。")
            return cmd
        # 自身 workspace:缺省按约定 <root>/agents/<name>(放行写,隔离其他 agent)。
        ws = agent_workspace if agent_workspace is not None else self._alfred_root / "agents" / agent_name
        profile_path = data_dir / SANDBOX_PROFILE_FILENAME
        profile_path.write_text(
            build_sandbox_profile(alfred_root=self._alfred_root, agent_workspace=ws),
            encoding="utf-8",
        )
        return ["sandbox-exec", "-f", str(profile_path), *cmd]
