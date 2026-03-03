"""Unit tests for workflow session ID helpers."""

from src.everbot.core.session.session_ids import infer_session_type
from src.everbot.core.workflow.session_ids import create_workflow_session_id


class TestCreateWorkflowSessionId:
    def test_format(self):
        sid = create_workflow_session_id("myagent", "bugfix")
        assert sid.startswith("workflow_myagent_bugfix_")
        parts = sid.split("_")
        # workflow_myagent_bugfix_YYYYMMDDHHMMSS_uuid8
        assert len(parts) == 5
        assert len(parts[3]) == 14  # timestamp
        assert len(parts[4]) == 8   # uuid8

    def test_uniqueness(self):
        ids = {create_workflow_session_id("a", "b") for _ in range(100)}
        assert len(ids) == 100


class TestInferSessionType:
    def test_workflow_type(self):
        sid = create_workflow_session_id("agent", "bugfix")
        assert infer_session_type(sid) == "workflow"

    def test_workflow_prefix_raw(self):
        assert infer_session_type("workflow_something") == "workflow"

    def test_other_types_unchanged(self):
        assert infer_session_type("heartbeat_session_x") == "heartbeat"
        assert infer_session_type("job_123") == "job"
        assert infer_session_type("web_session_x__sub") == "sub"
        assert infer_session_type("web_session_x") == "primary"
