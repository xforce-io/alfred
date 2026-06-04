"""dolphin_compat 不在导入期硬依赖 dolphin(#38 去硬依赖,可证伪验收)。

主干多处 eager import dolphin_compat;若它硬 import dolphin,milkie-only 部署
(未装 dolphin)连主干都加载不了。本测试模拟 dolphin 缺失,证明仍可导入 + 退化 no-op。
"""
import builtins
import importlib
import sys

import pytest


def test_ensure_continue_chat_is_noop_when_flags_absent(monkeypatch):
    import src.everbot.infra.dolphin_compat as dc

    monkeypatch.setattr(dc, "flags", None)
    assert dc.ensure_continue_chat_compatibility() is False  # 不崩、no-op


def test_module_imports_even_when_dolphin_not_installed(monkeypatch):
    """硬核证伪:拦截所有 `import dolphin*` → dolphin_compat 仍能 import。"""
    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "dolphin" or name.startswith("dolphin."):
            raise ImportError("simulated: dolphin not installed (milkie-only)")
        return real_import(name, *args, **kwargs)

    mod_name = "src.everbot.infra.dolphin_compat"
    saved = {k: v for k, v in sys.modules.items()
             if k == "dolphin" or k.startswith("dolphin.") or k == mod_name}
    for k in saved:
        sys.modules.pop(k, None)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    try:
        mod = importlib.import_module(mod_name)
        assert mod.flags is None                              # 优雅退化
        assert mod.ensure_continue_chat_compatibility() is False
        assert mod.KEY_HISTORY == "history"                  # 常量 fallback 生效
    finally:
        monkeypatch.undo()  # 恢复真 __import__
        sys.modules.pop(mod_name, None)
        sys.modules.update(saved)
        importlib.import_module(mod_name)  # 重新加载真实(带 dolphin)版本,避免污染后续测试
