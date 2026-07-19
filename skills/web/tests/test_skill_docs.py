"""E5: SKILL.md static checks for stop-search guidance and read path."""

from __future__ import annotations

from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent / "SKILL.md"


def test_e5_skill_md_guidance():
    text = SKILL.read_text(encoding="utf-8")
    assert "materials_hint" in text
    assert "extract_available" in text
    assert "content_id" in text
    assert "read_extract.py" in text
    # Coverage is agent-judged; not a forced stop signal
    assert "覆盖度" in text or "coverage" in text.lower() or "agent" in text.lower()
    # Stop searching when materials suffice
    assert "停止" in text or "stop" in text.lower()
    # Forbid cat of cache files
    assert "cat" in text.lower() or "禁止" in text
    # #160 boundary / tail@8000
    assert "8000" in text or "#160" in text or "tail@" in text
    # full-extract is debug only
    assert "full-extract" in text or "--full-extract" in text
    # cite page objectId, not summary as full-text handle
    assert "objectId" in text or "cite" in text.lower()
