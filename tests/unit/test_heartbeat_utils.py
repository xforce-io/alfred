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


def test_build_isolated_task_prompt_includes_cite_convention():
    """#124 L2(铺开):共享 isolated 提示词须含 cite 约定 —— 报告里的事实/数字
    应 cite 到产出它的工具 object(objectId),使「来源哪里」可经 get_lineage 溯源、
    且来源不可伪造。一处加,全报告型 routine 继承。"""
    prompt = build_isolated_task_prompt(_Task(id="routine_x", title="每日投资信号", description="跑投资信号"))

    assert "cite" in prompt
    assert "objectId" in prompt
    # 明确是"溯源/来源"语义,而非泛泛
    assert ("溯源" in prompt) or ("来源" in prompt)


def test_build_isolated_task_prompt_includes_fail_fast():
    """#153: isolated prompt must stop on clear skill/tool failure instead of shell archaeology."""
    prompt = build_isolated_task_prompt(
        _Task(id="routine_3d785e79", title="Serenity账号定时分析", description="twitter-watch")
    )
    assert "FAIL-FAST" in prompt
    assert "SELECTOR_OR_STRUCTURE_CHANGED" in prompt
    assert "STOP" in prompt
