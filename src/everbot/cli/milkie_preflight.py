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
from pathlib import Path
from typing import List, Optional, Tuple

from .doctor import DoctorItem

# 诊断尾部保留行数(与 sidecar 诊断对齐)。
_DIAG_TAIL_LINES = 20

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


# --- 件2:native deps 探针 ----------------------------------------------------
#
# 用 daemon 同款 node_bin 实测在 milkie 包上下文 require('better-sqlite3')。一条探针
# 同时覆盖:node_modules 缺失(Cannot find module)、ABI 不匹配(NODE_MODULE_VERSION)、
# 动态库加载失败(dlopen/.node/image not found)。比"只扫目录"或"抽象比版本号"更真。

# require 解析相对 cwd 的 node_modules;故 _run_probe 以 milkie 包根为 cwd。
_PROBE_JS = (
    "console.log('MILKIE_DEPS_ABI ' + process.version + ' ' + process.versions.modules);"
    "require('better-sqlite3');"
    "console.log('MILKIE_DEPS_OK');"
)

_PROBE_TITLE = "milkie native deps"


def _run_probe(node_bin: str, cwd: str) -> Tuple[int, str, str]:
    """跑一次 node -e 探针,返回 (returncode, stdout, stderr)。seam:测试可 monkeypatch。"""
    out = subprocess.run(
        [node_bin, "-e", _PROBE_JS],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return out.returncode, out.stdout, out.stderr


def _tail(text: str) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[-_DIAG_TAIL_LINES:])


def probe_native_deps(node_bin: str, milkie_root) -> Optional[DoctorItem]:
    """实测 better-sqlite3 能否被 node_bin 加载。

    milkie 目录整体不存在 → 返回 None(provider 未用 milkie,无需探测)。
    成功 → OK(带 node 版本/ABI);失败 → ERROR,按 stderr 给可执行修复命令。
    """
    root = Path(milkie_root)
    if not root.exists():
        return None

    try:
        rc, out, err = _run_probe(node_bin, str(root))
    except Exception as e:  # node 不存在 / 无法执行
        return DoctorItem(
            level="ERROR",
            title=_PROBE_TITLE,
            details=f"native deps 探针无法执行({node_bin}):{e}",
            hint="确认 everbot.milkie.node_bin 指向可用的 node 可执行(见上一项 node_bin 检查)。",
        )

    blob = f"{out}\n{err}"

    if rc == 0 and "MILKIE_DEPS_OK" in out:
        abi = next(
            (ln for ln in out.splitlines() if ln.startswith("MILKIE_DEPS_ABI")),
            "MILKIE_DEPS_ABI ?",
        )
        return DoctorItem(
            level="OK",
            title=_PROBE_TITLE,
            details=f"better-sqlite3 加载正常({abi.replace('MILKIE_DEPS_ABI ', 'node ')})",
        )

    if "NODE_MODULE_VERSION" in blob:
        return DoctorItem(
            level="ERROR",
            title=_PROBE_TITLE,
            details=_tail(err) or _tail(blob),
            hint=(
                f"原生模块 ABI 与 {node_bin} 不匹配 → 用该 node 对应的 npm 跑:"
                f"(cd {root} && npm rebuild better-sqlite3)"
            ),
        )

    if "Cannot find module" in blob:
        return DoctorItem(
            level="ERROR",
            title=_PROBE_TITLE,
            details=_tail(err) or _tail(blob),
            hint=f"milkie 依赖缺失/不全 → 用 {node_bin} 对应的 npm 跑:(cd {root} && npm ci)",
        )

    if any(k in blob for k in ("dlopen", ".node", "image not found", "dylib", "shared library", "undefined symbol")):
        return DoctorItem(
            level="ERROR",
            title=_PROBE_TITLE,
            details=_tail(err) or _tail(blob),
            hint=f"原生模块动态库加载失败 → 用 {node_bin} 重装/重编:(cd {root} && npm rebuild better-sqlite3)",
        )

    return DoctorItem(
        level="ERROR",
        title=_PROBE_TITLE,
        details=_tail(blob) or f"探针非零退出(exit {rc}),无 stderr 输出。",
        hint=f"手动复现:(cd {root} && {node_bin} -e \"require('better-sqlite3')\")",
    )


# --- daemon boot 接入(#91 PR4)----------------------------------------------
#
# boot 只做**全局静态 preflight**(node_bin + native deps),不在 boot 全量真 spawn
# 各 agent sidecar(那会拖慢启动、写 agent.md/data-dir)。fail-fast 策略:仅当 native
# deps 真失败(deps 加载不了 → 所有 sidecar 必死)才拒绝启动;node_bin 未钉死是
# advisory(WARN),deps 能加载就说明当前 node 可用,不阻塞 boot。


def resolve_node_bin_and_milkie_root(config: dict, project_root) -> Tuple[str, Path]:
    """从 everbot.milkie 配置解析 (node_bin, milkie 包根) —— 与 provider 装配同源。"""
    milkie_cfg = ((config.get("everbot") or {}).get("milkie") or {})
    node_bin = milkie_cfg.get("node_bin") or "node"
    dist = milkie_cfg.get("dist_path")
    milkie_root = (
        Path(dist).expanduser().parents[2]  # …/milkie/dist/cli/index.js → …/milkie
        if dist
        else (Path(project_root).parent / "milkie")
    )
    return node_bin, milkie_root


def run_boot_preflight(node_bin: str, milkie_root) -> Tuple[List[DoctorItem], bool]:
    """跑全局静态 preflight,返回 (findings, fatal)。

    fatal 仅由 native deps 真失败决定;node_bin 未钉死走 WARN(不致命)。
    """
    findings: List[DoctorItem] = [check_node_bin(node_bin, service_mode=False)]
    probe = probe_native_deps(node_bin, milkie_root)
    if probe is not None:
        findings.append(probe)
    fatal = any(f.title == _PROBE_TITLE and f.level == "ERROR" for f in findings)
    return findings, fatal


def enforce_boot_preflight(config: dict, project_root, log) -> None:
    """daemon boot 调用:跑 preflight、按级别打日志,native deps 致命则 raise 拒绝启动。"""
    node_bin, milkie_root = resolve_node_bin_and_milkie_root(config, project_root)
    findings, fatal = run_boot_preflight(node_bin, milkie_root)
    for f in findings:
        emit = {"ERROR": log.error, "WARN": log.warning}.get(f.level, log.info)
        emit("[preflight] %s: %s", f.title, f.details)
        if f.hint and f.level != "OK":
            emit("[preflight]   修复: %s", f.hint)
    if fatal:
        raise RuntimeError(
            "milkie native deps preflight 失败,拒绝启动(见上方 [preflight] 诊断)"
        )
