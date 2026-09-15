"""#221 option B — primary compose always attaches a short skill catalog."""

from src.everbot.core.runtime.context_strategy import (
    BuildMessageResult,
    PrimaryContextStrategy,
    RuntimeDeps,
)
from src.everbot.core.runtime.skill_catalog import (
    append_authoritative_skill_catalog,
    format_authoritative_skill_catalog,
)


def test_format_catalog_is_names_plus_forbid_line() -> None:
    text = format_authoritative_skill_catalog(
        [
            {"name": "papers", "description": "researcher papers CLI"},
            {"name": "kairo", "description": "topic workspaces"},
        ]
    )
    assert "**papers**" in text
    assert "**kairo**" in text
    assert "researcher papers CLI" not in text
    assert "topic workspaces" not in text
    assert "禁止补充清单外的技能名" in text
    assert "paper-discovery" in text  # named as a forbidden example, not an installed skill
    assert "- **paper-discovery**" not in text
    assert "- **kweaver**" not in text
    assert text.count("\n") <= 6


def test_append_catalog_always_attaches() -> None:
    skills = [{"name": "papers", "description": "cli"}]
    for trigger in ("用 web 搜一下", "有哪些 skills", "技能怎么装"):
        out = append_authoritative_skill_catalog(trigger, skills)
        assert out.startswith(trigger)
        assert "**papers**" in out
        assert "Installed skills" in out
        assert "cli" not in out


def _primary_deps(list_skills):
    return RuntimeDeps(
        load_workspace_instructions=lambda _name: "",
        list_installed_skills=list_skills,
    )


def test_primary_compose_always_injects_when_skills_load() -> None:
    captured: list[str] = []

    def list_skills(name: str):
        captured.append(name)
        return [{"name": "papers", "description": "researcher CLI"}, {"name": "kairo"}]

    deps = _primary_deps(list_skills)
    session = type("S", (), {"agent_name": "demo_agent", "mailbox": []})()
    strategy = PrimaryContextStrategy()
    for trigger in ("有哪些 skills", "用 web 搜一下", "技能怎么装"):
        result = strategy.build_message(session, trigger, deps)
        assert isinstance(result, BuildMessageResult)
        assert result.message.startswith(trigger)
        assert "**papers**" in result.message
        assert "**kairo**" in result.message
        assert "- **paper-discovery**" not in result.message
        assert "researcher CLI" not in result.message
    assert captured == ["demo_agent"] * 3


def test_primary_compose_skips_inject_when_skills_fail() -> None:
    def list_skills(_name: str):
        raise RuntimeError("discover_skills failed")

    deps = _primary_deps(list_skills)
    session = type("S", (), {"agent_name": "demo_agent", "mailbox": []})()
    result = PrimaryContextStrategy().build_message(session, "有哪些 skills", deps)
    assert result.message == "有哪些 skills"
    assert "Installed skills" not in result.message
