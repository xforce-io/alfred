"""#217 — papers skill must point at researcher CLI, not paper-discovery scripts."""

from pathlib import Path

_SKILL = Path(__file__).resolve().parents[2] / "skills" / "papers" / "SKILL.md"


def test_papers_skill_documents_researcher_cli() -> None:
    md = _SKILL.read_text(encoding="utf-8")
    assert "researcher papers trending" in md
    assert "researcher papers search" in md
    assert "researcher papers show" in md
    assert "researcher papers read" in md
    assert "fetch_papers.py" not in md
    assert "Do **not** curl/wget" in md
