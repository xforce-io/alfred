"""provider 中立常量值必须与 dolphin 当前值一致。"""
from src.everbot.core.agent.provider import (
    KEY_HISTORY,
    KEY_HISTORY_COMPACT_ON_PERSIST,
    KEY_HISTORY_COMPACT_RECENT_TURNS,
)


def test_key_history_matches_dolphin_compat():
    from src.everbot.infra.dolphin_compat import KEY_HISTORY as DC_KEY_HISTORY
    assert KEY_HISTORY == DC_KEY_HISTORY


def test_compact_constants_match_dolphin_compat():
    from src.everbot.infra import dolphin_compat as dc
    assert KEY_HISTORY_COMPACT_ON_PERSIST == dc.KEY_HISTORY_COMPACT_ON_PERSIST
    assert KEY_HISTORY_COMPACT_RECENT_TURNS == dc.KEY_HISTORY_COMPACT_RECENT_TURNS
