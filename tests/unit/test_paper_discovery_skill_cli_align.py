"""#193 P0b — paper-discovery SKILL CLI flags must match fetch_papers argparse."""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SKILL = _ROOT / "skills" / "paper-discovery" / "SKILL.md"
_SCRIPT = _ROOT / "skills" / "paper-discovery" / "scripts" / "fetch_papers.py"

# Flags that are documentation noise / not CLI options.
_IGNORE = frozenset({"--help"})


def _script_flags() -> set[str]:
    src = _SCRIPT.read_text(encoding="utf-8")
    return set(re.findall(r'add_argument\(\s*"(--[a-z0-9-]+)"', src))


def _skill_flags(text: str) -> set[str]:
    # Match long options used in examples/tables; exclude markdown em-dashes leftovers.
    return {f for f in re.findall(r"--[a-z][a-z0-9-]+", text) if f not in _IGNORE}


def test_repo_skill_cli_flags_subset_of_script():
    script = _script_flags()
    skill = _skill_flags(_SKILL.read_text(encoding="utf-8"))
    missing = sorted(skill - script)
    assert missing == [], (
        "SKILL.md documents CLI flags not implemented by fetch_papers.py: "
        f"{missing}. Either implement them or remove from SKILL."
    )


def test_repo_skill_has_no_phantom_analysis_flags():
    text = _SKILL.read_text(encoding="utf-8")
    for phantom in (
        "--with-analysis",
        "--filter-by-domain",
        "--filter-opensource",
    ):
        assert phantom not in text, f"phantom flag still documented: {phantom}"


def test_repo_skill_documents_with_summary_and_agent_side_interpretation():
    """Daily digest: script may --with-summary; structured interpretation is agent work."""
    text = _SKILL.read_text(encoding="utf-8")
    assert "--with-summary" in text
    # Agent-side interpretation requirements (not fake CLI).
    assert "核心创新" in text or "core innovation" in text.lower() or "Structured interpretation" in text
    assert "arXiv" in text or "arxiv" in text.lower()
