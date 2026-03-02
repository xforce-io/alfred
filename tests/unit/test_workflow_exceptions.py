"""Unit tests for workflow exception hierarchy."""

from src.everbot.core.workflow.exceptions import (
    BudgetExhaustedError,
    CheckpointPauseError,
    ConfigValidationError,
    PhaseGroupExhaustedError,
    WorkflowError,
)


class TestExceptionHierarchy:
    def test_all_exceptions_inherit_workflow_error(self):
        assert issubclass(ConfigValidationError, WorkflowError)
        assert issubclass(PhaseGroupExhaustedError, WorkflowError)
        assert issubclass(BudgetExhaustedError, WorkflowError)
        assert issubclass(CheckpointPauseError, WorkflowError)

    def test_config_validation_error_with_path(self):
        e = ConfigValidationError("bad field", path="workflow.yaml")
        assert "workflow.yaml" in str(e)
        assert "bad field" in str(e)
        assert e.path == "workflow.yaml"

    def test_config_validation_error_without_path(self):
        e = ConfigValidationError("bad field")
        assert str(e) == "bad field"
        assert e.path == ""

    def test_phase_group_exhausted_error(self):
        e = PhaseGroupExhaustedError(
            group="impl_verify",
            iterations=5,
            failure_summary="test failed x5",
            failure_history=["fail1", "fail2", "fail3"],
        )
        assert e.group == "impl_verify"
        assert e.iterations == 5
        assert e.failure_summary == "test failed x5"
        assert len(e.failure_history) == 3
        assert "impl_verify" in str(e)

    def test_budget_exhausted_error(self):
        e = BudgetExhaustedError(budget_type="total_tool_calls", used=210, limit=200)
        assert e.budget_type == "total_tool_calls"
        assert e.used == 210
        assert e.limit == 200
        assert "210" in str(e)

    def test_checkpoint_pause_error(self):
        e = CheckpointPauseError(phase_name="plan", artifact="my plan")
        assert e.phase_name == "plan"
        assert e.artifact == "my plan"
        assert "plan" in str(e)

    def test_checkpoint_pause_error_no_artifact(self):
        e = CheckpointPauseError(phase_name="plan")
        assert e.artifact is None
