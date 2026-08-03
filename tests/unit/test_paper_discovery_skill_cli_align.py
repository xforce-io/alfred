"""#193 P0b — paper-discovery SKILL CLI flags must match fetch_papers argparse."""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SKILL = _ROOT / "skills" / "paper-discovery" / "SKILL.md"
_SCRIPT = _ROOT / "skills" / "paper-discovery" / "scripts" / "fetch_papers.py"

_IGNORE = frozenset({"--help"})


def _script_flags() -> set[str]:
    src = _SCRIPT.read_text(encoding="utf-8")
    return set(re.findall(r'add_argument\(\s*"(--[a-z0-9-]+)"', src))


def _skill_documented_cli_flags(text: str) -> set[str]:
    """Flags that SKILL presents as invokable CLI (examples + options table).

    Prose that *forbids* a flag (e.g. "do not call --with-analysis") must not
    count as documenting a supported option.
    """
    chunks: list[str] = []
    # Fenced code blocks (bash examples).
    chunks.extend(re.findall(r"```(?:bash|sh|shell)?\n(.*?)```", text, flags=re.S))
    # Markdown tables (options / fields).
    for line in text.splitlines():
        if line.strip().startswith("|") and "--" in line:
            chunks.append(line)
    # Inline code that looks like a full CLI invocation.
    chunks.extend(re.findall(r"`([^`]*fetch_papers\.py[^`]*)`", text))

    flags: set[str] = set()
    for chunk in chunks:
        flags.update(re.findall(r"--[a-z][a-z0-9-]+", chunk))
    return flags - _IGNORE


def test_repo_skill_cli_flags_subset_of_script():
    script = _script_flags()
    skill = _skill_documented_cli_flags(_SKILL.read_text(encoding="utf-8"))
    missing = sorted(skill - script)
    assert missing == [], (
        "SKILL.md documents CLI flags not implemented by fetch_papers.py: "
        f"{missing}. Either implement them or remove from SKILL examples/tables."
    )


def test_repo_skill_examples_have_no_phantom_analysis_flags():
    skill = _skill_documented_cli_flags(_SKILL.read_text(encoding="utf-8"))
    for phantom in (
        "--with-analysis",
        "--filter-by-domain",
        "--filter-opensource",
    ):
        assert phantom not in skill, f"phantom flag still in CLI examples/tables: {phantom}"


def test_repo_skill_documents_with_summary_and_agent_side_interpretation():
    """Daily digest: script may --with-summary; structured interpretation is agent work."""
    text = _SKILL.read_text(encoding="utf-8")
    assert "--with-summary" in text
    assert "核心创新" in text or "Structured interpretation" in text
    assert "arXiv" in text or "arxiv" in text.lower()
