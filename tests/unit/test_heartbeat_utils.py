"""Tests for heartbeat utility prompt builders."""

from src.everbot.core.runtime.heartbeat_utils import build_isolated_task_prompt


class _Task:
    def __init__(self, **kwargs):
        self.id = kwargs.get("id", "routine_test")
        self.title = kwargs.get("title", "")
        self.description = kwargs.get("description", "")


def test_build_isolated_task_prompt_warns_against_python_wrapping():
    """Skill loader calls in task descriptions must stay as direct tool calls."""
    task = _Task(
        id="routine_test",
        title="每日巡检",
        description='加载技能 _load_resource_skill("kweaver-code-review", mode="full")',
    )

    prompt = build_isolated_task_prompt(task)

    assert "Never execute `_load_resource_skill(...)` inside `_python` or `_bash`." in prompt
    assert '_load_resource_skill("kweaver-code-review", mode="full")' in prompt
