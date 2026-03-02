"""Unit tests for workflow report generation."""

import json
from datetime import datetime

from src.everbot.core.workflow.models import (
    PhaseTraceEntry,
    TaskSessionConfig,
    TaskSessionState,
    VerifyTraceEntry,
    WorkflowReport,
)
from src.everbot.core.workflow.report import (
    generate_report,
    render_report_json,
    render_report_markdown,
)


def _sample_state(status="done", **kwargs):
    return TaskSessionState(
        session_id="wf_test_123",
        start_time=datetime(2026, 3, 1, 12, 0, 0),
        status=status,
        artifacts={"research": "found X", "plan": "fix Y"},
        total_tool_calls_used=45,
        **kwargs,
    )


def _sample_traces():
    return [
        PhaseTraceEntry(
            phase_name="research",
            status="completed",
            tool_calls_used=10,
            duration_seconds=30.0,
            artifact_preview="found X",
        ),
        PhaseTraceEntry(
            phase_name="plan",
            status="completed",
            tool_calls_used=5,
            duration_seconds=15.0,
        ),
        PhaseTraceEntry(
            phase_name="impl_verify",
            phase_type="phase_group",
            status="completed",
            iterations=2,
            verify_traces=[
                VerifyTraceEntry(
                    iteration=1, passed=False, exit_code=1,
                    output="FAILED: test_x", duration_seconds=5.0,
                ),
                VerifyTraceEntry(
                    iteration=2, passed=True, exit_code=0,
                    output="OK", duration_seconds=3.0,
                ),
            ],
        ),
    ]


class TestGenerateReport:
    def test_basic_report(self):
        report = generate_report(
            state=_sample_state(),
            phase_traces=_sample_traces(),
            config=TaskSessionConfig(name="bugfix"),
            git_start_commit=None,
            project_dir="/tmp",
        )
        assert report.session_id == "wf_test_123"
        assert report.workflow_name == "bugfix"
        assert report.status == "done"
        assert report.total_tool_calls == 45
        assert len(report.phase_traces) == 3
        assert report.error == ""  # done → no error

    def test_failed_report_has_error(self):
        report = generate_report(
            state=_sample_state(status="failed"),
            phase_traces=[],
            config=TaskSessionConfig(name="test"),
            git_start_commit=None,
            project_dir="/tmp",
        )
        assert "failed" in report.error

    def test_final_artifact_from_last_non_internal(self):
        state = _sample_state()
        state.artifacts["__retry_context"] = "internal"
        state.artifacts["plan"] = "the plan"
        report = generate_report(
            state=state,
            phase_traces=[],
            config=TaskSessionConfig(),
            git_start_commit=None,
            project_dir="/tmp",
        )
        assert report.final_artifact == "the plan"


class TestRenderReportMarkdown:
    def test_contains_key_sections(self):
        report = WorkflowReport(
            session_id="wf_123",
            workflow_name="bugfix",
            status="done",
            total_duration_seconds=120.5,
            total_tool_calls=30,
            phase_traces=_sample_traces(),
            files_modified=["src/a.py"],
        )
        md = render_report_markdown(report)
        assert "# Workflow Report: bugfix" in md
        assert "OK" in md
        assert "120.5s" in md
        assert "Phase Traces" in md
        assert "research" in md
        assert "impl_verify" in md
        assert "FAIL" in md
        assert "PASS" in md
        assert "Files Modified" in md
        assert "src/a.py" in md

    def test_failed_report_shows_error(self):
        report = WorkflowReport(
            session_id="wf_123",
            status="failed",
            error="Budget exhausted",
        )
        md = render_report_markdown(report)
        assert "FAILED" in md
        assert "Budget exhausted" in md

    def test_empty_report(self):
        report = WorkflowReport(session_id="wf_empty", status="done")
        md = render_report_markdown(report)
        assert "Workflow Report" in md

    def test_rollback_trace(self):
        report = WorkflowReport(
            session_id="wf_1",
            phase_traces=[
                PhaseTraceEntry(
                    phase_name="g1",
                    phase_type="phase_group",
                    status="exhausted",
                    rollback_triggered=True,
                ),
            ],
        )
        md = render_report_markdown(report)
        assert "Rollback triggered" in md


class TestRenderReportJson:
    def test_valid_json(self):
        report = WorkflowReport(
            session_id="wf_123",
            workflow_name="test",
            status="done",
            phase_traces=_sample_traces(),
        )
        json_str = render_report_json(report)
        parsed = json.loads(json_str)
        assert parsed["session_id"] == "wf_123"
        assert len(parsed["phase_traces"]) == 3
        vt = parsed["phase_traces"][2]["verify_traces"]
        assert len(vt) == 2
        assert vt[0]["passed"] is False
        assert vt[1]["passed"] is True
