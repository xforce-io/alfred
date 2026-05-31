"""硬约束：除 provider/dolphin/** 与 infra/dolphin_compat.py 外，
src/everbot 主干代码不得 import dolphin。

这是 AgentProvider 抽象「把 dolphin 收敛到一个包」的可执行验收。
"""
import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src" / "everbot"
_ALLOW_PREFIXES = (
    str(_SRC / "core" / "agent" / "provider" / "dolphin"),
    str(_SRC / "infra" / "dolphin_compat.py"),
)
_PAT = re.compile(r"^\s*(?:from|import)\s+dolphin(?:\.|\s|$)", re.MULTILINE)


def test_no_dolphin_imports_outside_provider():
    scanned = 0
    offenders = []
    for py in _SRC.rglob("*.py"):
        sp = str(py)
        scanned += 1
        if sp.startswith(_ALLOW_PREFIXES):
            continue
        text = py.read_text(encoding="utf-8")
        if _PAT.search(text):
            offenders.append(sp)
    # Sanity: ensure the walk actually covered the package.
    assert scanned > 50, f"too few files scanned ({scanned}) — glob misconfigured"
    assert not offenders, (
        "Unexpected dolphin imports outside provider:\n" + "\n".join(offenders)
    )
