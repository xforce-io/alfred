"""provider/dolphin/compat 的常量必须与 infra/dolphin_compat（规范来源）一致。

两者都从 dolphin 解析同一批常量；本测试防止两份 compat 垫片漂移。
"""


def test_provider_compat_matches_infra():
    from src.everbot.core.agent.provider.dolphin import compat as pc
    from src.everbot.infra import dolphin_compat as dc

    assert pc.KEY_HISTORY == dc.KEY_HISTORY
    assert pc.KEY_HISTORY_COMPACT_ON_PERSIST == dc.KEY_HISTORY_COMPACT_ON_PERSIST
    assert pc.KEY_HISTORY_COMPACT_RECENT_TURNS == dc.KEY_HISTORY_COMPACT_RECENT_TURNS
