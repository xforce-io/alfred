"""milkie 运行时启动期自检(#91 A)。

被 ``bin/everbot doctor`` 与 daemon boot 复用,产出 :class:`DoctorItem`。本文件先落
**件3:node_bin 显式钉死** —— 件2(native deps 探针)后续补入同一模块。

为何钉死 node_bin:daemon(launchd PATH)与交互 shell(nvm 等)解析到的 ``node``
往往不同版本,原生模块(better-sqlite3)按某个 node 编译后,另一 node 加载即
``NODE_MODULE_VERSION`` 不匹配崩溃 —— 2026-06-21 demo_agent 改模型后 sidecar 全崩即此根因。
裸 ``node`` 走 PATH = 只检查"当下碰巧解析到的 node",没消掉根因。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Optional

from .doctor import DoctorItem

_PIN_HINT = (
    "在 ~/.alfred/config.yaml 的 everbot.milkie.node_bin 填**绝对路径**"
    "(如 {suggest}),确保 daemon 与重装依赖用同一个 node,避免 ABI 漂移。"
)


def _node_version(path: str) -> Optional[str]:
    """best-effort 取 node 版本字符串;失败返回 None(不阻塞自检)。"""
    try:
        out = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=5
        )
    except Exception:
        return None
    return (out.stdout or out.stderr or "").strip() or None


def check_node_bin(node_bin: str, *, service_mode: bool = False) -> DoctorItem:
    """校验 milkie 用的 ``node_bin`` 是否显式钉死(绝对路径)。

    service_mode=True(daemon/service)对"未钉死"更严格 —— 升级为 ERROR。
    """
    title = "milkie node_bin"

    if os.path.isabs(node_bin):
        if os.path.isfile(node_bin) and os.access(node_bin, os.X_OK):
            ver = _node_version(node_bin)
            details = f"已钉死绝对路径:{node_bin}" + (f"(版本 {ver})" if ver else "")
            return DoctorItem(level="OK", title=title, details=details)
        return DoctorItem(
            level="ERROR",
            title=title,
            details=f"配置的绝对路径 node_bin 不存在或不可执行:{node_bin}",
            hint=_PIN_HINT.format(suggest=shutil.which("node") or "/opt/homebrew/bin/node"),
        )

    # 非绝对路径:走 PATH 解析 —— 根因未消除。
    resolved = shutil.which(node_bin)
    suggest = resolved or "/opt/homebrew/bin/node"
    if resolved is None:
        return DoctorItem(
            level="ERROR",
            title=title,
            details=f"node_bin '{node_bin}' 非绝对路径,且当前 PATH 中找不到。",
            hint=_PIN_HINT.format(suggest=suggest),
        )
    level = "ERROR" if service_mode else "WARN"
    return DoctorItem(
        level=level,
        title=title,
        details=(
            f"node_bin '{node_bin}' 未钉死(走 PATH 解析)。"
            f"当前解析到:{resolved}。daemon 与 shell 的 PATH 可能解析到不同 node。"
        ),
        hint=_PIN_HINT.format(suggest=suggest),
    )
