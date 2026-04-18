"""Unit tests for resource skill change detection."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.everbot.core.channel.skill_change_detector import (
    SESSION_VAR_KNOWN_SKILLS,
    get_current_resource_skills,
    inject_skill_updates_if_needed,
)
from src.everbot.infra.dolphin_compat import KEY_HISTORY


def _build_agent_with_resource_skills(skills: dict[str, str]):
    """Build a minimal agent object with a mocked resource skill toolkit."""
    resource_skillkit = MagicMock()
    resource_skillkit.getName.return_value = "resource_skillkit"
    resource_skillkit.get_available_skills.return_value = list(skills.keys())
    resource_skillkit.get_skill_meta.side_effect = lambda name: SimpleNamespace(
        description=skills[name]
    )

    loader_tool = SimpleNamespace(owner_skillkit=resource_skillkit)
    installed_toolset = MagicMock()
    installed_toolset.getTools.return_value = [loader_tool]

    context = MagicMock()
    context.get_var_value.return_value = []

    agent = SimpleNamespace(
        global_toolkits=SimpleNamespace(installedToolSet=installed_toolset),
        global_skills=None,
        executor=SimpleNamespace(context=context),
    )
    return agent, context


def test_get_current_resource_skills_reads_installed_toolset():
    """Current resource skills should be resolved from installedToolSet.getTools()."""
    agent, _ = _build_agent_with_resource_skills(
        {"example-skill": "Example description"}
    )

    assert get_current_resource_skills(agent) == {
        "example-skill": "Example description"
    }


def test_inject_skill_updates_persists_first_seen_baseline_only():
    """First observation should persist the baseline without injecting history."""
    agent, context = _build_agent_with_resource_skills(
        {"example-skill": "Example description"}
    )
    session_data = SimpleNamespace(variables={})

    inject_skill_updates_if_needed(agent, "session-1", session_data)

    context.set_variable.assert_called_once_with(
        SESSION_VAR_KNOWN_SKILLS, ["example-skill"]
    )
    history_calls = [
        call for call in context.set_variable.call_args_list
        if call.args and call.args[0] == KEY_HISTORY
    ]
    assert history_calls == []
