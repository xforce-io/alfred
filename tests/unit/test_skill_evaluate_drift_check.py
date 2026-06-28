"""skill_evaluate.run() runs drift detection per evaluated skill (#132, AC#1)."""

import asyncio
from types import SimpleNamespace

import pytest

from src.everbot.core.jobs import skill_evaluate
import src.everbot.infra.user_data as user_data_mod


class _StubSegmentLogger:
    def __init__(self, *_a, **_k):
        pass

    def list_skills(self):
        return ["twitter-watch", "invest"]

    def cleanup(self, _skill_id):
        pass


class _StubVersionManager:
    def __init__(self, *_a, **_k):
        pass


def test_run_checks_drift_for_each_skill(tmp_path, monkeypatch):
    checked = []

    udm = SimpleNamespace(
        skill_logs_dir=tmp_path / "skill_logs",
        sessions_dir=tmp_path / "sessions",
        get_agent_writable_skills_dir=lambda a: tmp_path / "writable",
        get_agent_read_skill_dirs=lambda a: [tmp_path / "writable"],
        check_skill_override_drift=lambda agent, skill: checked.append((agent, skill)),
    )

    monkeypatch.setattr(user_data_mod, "get_user_data_manager", lambda: udm)
    monkeypatch.setattr(skill_evaluate, "SegmentLogger", _StubSegmentLogger)
    monkeypatch.setattr(skill_evaluate, "VersionManager", _StubVersionManager)

    async def _stub_eval_one(*_a, **_k):
        return None

    monkeypatch.setattr(skill_evaluate, "_evaluate_one", _stub_eval_one)

    context = SimpleNamespace(
        skill_logs_dir=None,
        skill_eval_dir=tmp_path / "skill_eval",
        agent_name="demo",
    )

    asyncio.run(skill_evaluate.run(context))

    assert checked == [("demo", "twitter-watch"), ("demo", "invest")]


def test_run_drift_check_failure_does_not_break_evaluation(tmp_path, monkeypatch):
    # A drift-check exception must never abort the evaluation loop.
    udm = SimpleNamespace(
        skill_logs_dir=tmp_path / "skill_logs",
        sessions_dir=tmp_path / "sessions",
        get_agent_writable_skills_dir=lambda a: tmp_path / "writable",
        get_agent_read_skill_dirs=lambda a: [tmp_path / "writable"],
        check_skill_override_drift=lambda agent, skill: (_ for _ in ()).throw(
            RuntimeError("hash failed")
        ),
    )
    monkeypatch.setattr(user_data_mod, "get_user_data_manager", lambda: udm)
    monkeypatch.setattr(skill_evaluate, "SegmentLogger", _StubSegmentLogger)
    monkeypatch.setattr(skill_evaluate, "VersionManager", _StubVersionManager)

    evaluated = []

    async def _stub_eval_one(_ctx, _sl, _vm, skill_id, _sessions):
        evaluated.append(skill_id)
        return None

    monkeypatch.setattr(skill_evaluate, "_evaluate_one", _stub_eval_one)

    context = SimpleNamespace(
        skill_logs_dir=None,
        skill_eval_dir=tmp_path / "skill_eval",
        agent_name="demo",
    )

    asyncio.run(skill_evaluate.run(context))  # must not raise

    assert evaluated == ["twitter-watch", "invest"]
