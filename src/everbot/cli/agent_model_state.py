"""从运行 sidecar 的 agent.md 读「生效模型」,与配置目标比对出 stale(#93 件B)。

sidecar 启动那刻把模型烧进 ``~/.alfred/milkie/<agent>/agent.md`` 后不再重读;改了
models.yaml 不重启就不生效。本模块让"生效模型 vs 配置目标"可见,供 status 展示。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, List, Optional

_INDENT_MODEL_RE = re.compile(r"\s+model:\s*(\S+)")


def parse_agent_md_model(path) -> Optional[str]:
    """取 agent.md 顶层 ``model:`` 块里的主模型名(忽略 ``models.fast`` 与正文)。"""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    in_block = False
    for ln in lines:
        if ln.rstrip() == "model:":  # 顶层 model: 块开始
            in_block = True
            continue
        if in_block:
            if ln[:1].isspace():  # 块内缩进行
                m = _INDENT_MODEL_RE.match(ln)
                if m:
                    return m.group(1)
            elif ln.strip():  # 遇到下一个顶层键 → 块结束
                break
    return None


def collect_agent_model_states(
    agent_names: List[str],
    *,
    milkie_root,
    configured_resolver: Callable[[str], Optional[str]],
) -> List[dict]:
    """对每个 agent 汇总 {agent, effective, configured, stale}。

    effective 来自运行 agent.md;configured 来自配置解析。无 agent.md(sidecar 未拉起)
    → effective=None、stale=False(尚无"生效"可言)。
    """
    root = Path(milkie_root)
    out: List[dict] = []
    for name in agent_names:
        effective = parse_agent_md_model(root / name / "agent.md")
        try:
            configured = configured_resolver(name)
        except Exception:
            configured = None
        stale = bool(effective and configured and effective != configured)
        out.append(
            {"agent": name, "effective": effective, "configured": configured, "stale": stale}
        )
    return out
