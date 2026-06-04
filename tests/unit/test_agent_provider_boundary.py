"""硬约束(#38 去 dolphin 后反转):src/everbot **任何** 文件都不得 import dolphin。

dolphin 已彻底移除(provider/dolphin/ 包删除、dolphin_compat 纯化、oneshot/模型配置
脱钩),不再有任何例外目录。这是「彻底删除 dolphin」的可执行验收。
"""
import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src" / "everbot"
_PAT = re.compile(r"^\s*(?:from|import)\s+dolphin(?:\.|\s|$)", re.MULTILINE)


def test_no_dolphin_imports_anywhere():
    scanned = 0
    offenders = []
    for py in _SRC.rglob("*.py"):
        scanned += 1
        if _PAT.search(py.read_text(encoding="utf-8")):
            offenders.append(str(py))
    assert scanned > 50, f"too few files scanned ({scanned}) — glob misconfigured"
    assert not offenders, "dolphin imports must be fully removed:\n" + "\n".join(offenders)
