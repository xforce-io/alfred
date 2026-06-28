"""dolphin_compat 纯化后的单测(#38:无 dolphin import)。

KEY_HISTORY* 是主干依赖的具体常量值。
"""
from src.everbot.infra import dolphin_compat as dc


def test_history_constants_values():
    assert dc.KEY_HISTORY == "_history"
    assert dc.KEY_HISTORY_COMPACT_ON_PERSIST == "_history_compact_on_persist"
    assert dc.KEY_HISTORY_COMPACT_RECENT_TURNS == "_history_compact_recent_turns"


def test_module_has_no_dolphin_import():
    import pathlib
    import re
    src = pathlib.Path(dc.__file__).read_text(encoding="utf-8")
    # 只查真实 import 语句(行首),不误判 docstring 里的字样
    assert not re.search(r"^\s*(?:from|import)\s+dolphin(?:\.|\s|$)", src, re.MULTILINE)
