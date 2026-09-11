"""#221 — natural skill-listing questions get an authoritative catalog."""

from src.everbot.core.runtime.context_strategy import (
    BuildMessageResult,
    PrimaryContextStrategy,
    RuntimeDeps,
)
from src.everbot.core.runtime.skill_catalog import (
    append_catalog_if_listing_query,
    format_authoritative_skill_catalog,
    is_skill_catalog_query,
)


def test_is_skill_catalog_query_accepts_natural_phrasing() -> None:
    assert is_skill_catalog_query("有哪些 skills")
    assert is_skill_catalog_query("有哪些技能")
    assert is_skill_catalog_query("你有哪些技能？")
    assert is_skill_catalog_query("列出技能")
    assert is_skill_catalog_query("what skills do you have")
    assert is_skill_catalog_query("list skills")


def test_is_skill_catalog_query_rejects_non_listing() -> None:
    assert not is_skill_catalog_query("用 web 搜一下")
    assert not is_skill_catalog_query("技能怎么装")
    assert not is_skill_catalog_query("帮我装个 skill")
    assert not is_skill_catalog_query("kairo 有哪些 workspace")
    assert not is_skill_catalog_query("有哪些定时任务")


def test_format_catalog_forbids_names_outside_the_list() -> None:
    text = format_authoritative_skill_catalog(
        [
            {"name": "papers", "description": "researcher papers CLI"},
            {"name": "kairo", "description": "topic workspaces"},
        ]
    )
    assert "**papers**" in text
    assert "**kairo**" in text
    assert "禁止根据记忆补充" in text
    assert "paper-discovery" in text  # named as a forbidden example, not an installed skill
    assert "- **paper-discovery**" not in text
    assert "- **kweaver**" not in text


def test_append_only_on_listing_query() -> None:
    skills = [{"name": "papers", "description": "cli"}]
    assert "Installed skills" not in append_catalog_if_listing_query(
        "用 web 搜一下", "用 web 搜一下", skills
    )
    out = append_catalog_if_listing_query("有哪些 skills", "有哪些 skills", skills)
    assert out.startswith("有哪些 skills")
    assert "**papers**" in out


def test_primary_compose_injects_catalog_for_listing_query() -> None:
    captured: list[str] = []

    def load_ws(_name: str) -> str:
        return ""

    def list_skills(name: str):
        captured.append(name)
        return [{"name": "papers", "description": "researcher CLI"}, {"name": "kairo"}]

    deps = RuntimeDeps(
        load_workspace_instructions=load_ws,
        list_installed_skills=list_skills,
    )
    session = type("S", (), {"agent_name": "demo_agent", "mailbox": []})()
    result = PrimaryContextStrategy().build_message(session, "有哪些 skills", deps)
    assert isinstance(result, BuildMessageResult)
    assert captured == ["demo_agent"]
    assert "**papers**" in result.message
    assert "**kairo**" in result.message
    assert "- **paper-discovery**" not in result.message
