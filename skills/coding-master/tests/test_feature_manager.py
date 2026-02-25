"""Tests for feature_manager.py — FeatureManager state machine."""

import json

import pytest

from feature_manager import FeatureManager, ARTIFACT_DIR


@pytest.fixture
def fm(ws_dir):
    """FeatureManager with artifact dir pre-created."""
    (ws_dir / ARTIFACT_DIR).mkdir(exist_ok=True)
    return FeatureManager(str(ws_dir))


class TestCreatePlan:
    def test_create_and_persist(self, fm, ws_dir):
        features = [
            {"title": "Auth", "task": "add auth"},
            {"title": "API", "task": "add api", "depends_on": [0]},
        ]
        result = fm.create_plan("big task", features)
        assert result["ok"] is True
        assert result["data"]["total"] == 2
        # Persisted to disk
        assert fm.plan_path.exists()
        plan = json.loads(fm.plan_path.read_text())
        assert plan["origin_task"] == "big task"
        assert plan["features"][1]["depends_on"] == [0]


class TestNextFeature:
    def test_returns_first_pending(self, fm):
        fm.create_plan("task", [
            {"title": "A", "task": "a"},
            {"title": "B", "task": "b"},
        ])
        result = fm.next_feature()
        assert result["ok"] is True
        assert result["data"]["feature"]["title"] == "A"
        assert result["data"]["feature"]["status"] == "in_progress"

    def test_dependency_blocking(self, fm):
        fm.create_plan("task", [
            {"title": "A", "task": "a"},
            {"title": "B", "task": "b", "depends_on": [0]},
        ])
        # Mark A as in_progress (next_feature does this)
        fm.next_feature()
        # B depends on A (which is in_progress, not done), so blocked
        result = fm.next_feature()
        assert result["ok"] is True
        assert result["data"]["feature"] is None
        assert result["data"]["status"] == "blocked"

    def test_no_plan(self, ws_dir):
        fm = FeatureManager(str(ws_dir))
        result = fm.next_feature()
        assert result["ok"] is False
        assert "no feature plan" in result["error"]


class TestMarkDone:
    def test_mark_done_with_progress(self, fm):
        fm.create_plan("task", [
            {"title": "A", "task": "a"},
            {"title": "B", "task": "b"},
        ])
        result = fm.mark_done(0, branch="fix/a", pr="https://pr/1")
        assert result["ok"] is True
        assert result["data"]["completed"] == 1
        assert result["data"]["remaining"] == 1

    def test_out_of_range(self, fm):
        fm.create_plan("task", [{"title": "A", "task": "a"}])
        result = fm.mark_done(5)
        assert result["ok"] is False
        assert "out of range" in result["error"]


class TestUpdate:
    def test_skip_feature(self, fm):
        fm.create_plan("task", [
            {"title": "A", "task": "a"},
            {"title": "B", "task": "b"},
        ])
        result = fm.update(1, status="skipped")
        assert result["ok"] is True
        assert result["data"]["updated"]["status"] == "skipped"

    def test_out_of_range(self, fm):
        fm.create_plan("task", [{"title": "A", "task": "a"}])
        result = fm.update(99, status="done")
        assert result["ok"] is False


class TestAllComplete:
    def test_all_done(self, fm):
        fm.create_plan("task", [
            {"title": "A", "task": "a"},
            {"title": "B", "task": "b"},
        ])
        fm.mark_done(0)
        fm.mark_done(1)
        result = fm.next_feature()
        assert result["ok"] is True
        assert result["data"]["status"] == "all_complete"

    def test_mixed_done_and_skipped(self, fm):
        fm.create_plan("task", [
            {"title": "A", "task": "a"},
            {"title": "B", "task": "b"},
        ])
        fm.mark_done(0)
        fm.update(1, status="skipped")
        result = fm.next_feature()
        assert result["ok"] is True
        assert result["data"]["status"] == "all_complete"


class TestListAll:
    def test_list(self, fm):
        fm.create_plan("task", [
            {"title": "A", "task": "a"},
            {"title": "B", "task": "b"},
        ])
        fm.mark_done(0, branch="fix/a")
        result = fm.list_all()
        assert result["ok"] is True
        assert result["data"]["total"] == 2
        assert result["data"]["completed"] == 1
        assert result["data"]["features"][0]["branch"] == "fix/a"

    def test_list_no_plan(self, ws_dir):
        fm = FeatureManager(str(ws_dir))
        result = fm.list_all()
        assert result["ok"] is False
