"""Unit tests for workflow data models."""

from datetime import datetime

from src.everbot.core.workflow.models import (
    CmdResult,
    PhaseConfig,
    PhaseGroupConfig,
    PhaseResult,
    PhaseTraceEntry,
    TaskSessionConfig,
    TaskSessionEvent,
    TaskSessionState,
    VerificationCmdConfig,
    VerifyTraceEntry,
    WorkflowReport,
)


class TestVerificationCmdConfig:
    def test_defaults(self):
        cfg = VerificationCmdConfig(cmd="pytest")
        assert cfg.cmd == "pytest"
        assert cfg.timeout_seconds == 120
        assert cfg.working_dir is None
        assert cfg.env == {}

    def test_custom_values(self):
        cfg = VerificationCmdConfig(
            cmd="make test", timeout_seconds=60,
            working_dir="/tmp", env={"CI": "1"},
        )
        assert cfg.timeout_seconds == 60
        assert cfg.env["CI"] == "1"


class TestPhaseConfig:
    def test_defaults(self):
        cfg = PhaseConfig(name="research")
        assert cfg.name == "research"
        assert cfg.max_turns == 10
        assert cfg.max_tool_calls == 50
        assert cfg.timeout_seconds == 300
        assert cfg.checkpoint is False
        assert cfg.completion_signal == "llm_decision"
        assert cfg.input_artifacts == []
        assert cfg.allowed_tools is None
        assert cfg.on_failure == "abort"
        assert cfg.max_retries == 1
        assert cfg.verification_cmd is None
        assert cfg.verify_protocol is None

    def test_with_verification_cmd(self):
        cfg = PhaseConfig(
            name="verify",
            verification_cmd=VerificationCmdConfig(cmd="pytest"),
        )
        assert cfg.verification_cmd.cmd == "pytest"

    def test_input_artifacts_mutable_default(self):
        """Each instance should get its own list."""
        a = PhaseConfig(name="a")
        b = PhaseConfig(name="b")
        a.input_artifacts.append("research")
        assert b.input_artifacts == []


class TestPhaseGroupConfig:
    def test_defaults(self):
        cfg = PhaseGroupConfig(
            name="impl_verify",
            action_phase="implement",
            verify_phase="verify",
        )
        assert cfg.max_iterations == 5
        assert cfg.on_exhausted == "rollback"
        assert cfg.rollback_target is None
        assert cfg.setup_phase is None
        assert cfg.phases == []


class TestTaskSessionConfig:
    def test_defaults(self):
        cfg = TaskSessionConfig()
        assert cfg.total_timeout_seconds == 1800
        assert cfg.total_max_tool_calls == 200
        assert cfg.max_rollback_retries == 2


class TestTaskSessionState:
    def test_defaults(self):
        state = TaskSessionState(session_id="wf_123")
        assert state.session_id == "wf_123"
        assert state.current_phase_index == 0
        assert state.status == "pending"
        assert state.artifacts == {}
        assert state.total_tool_calls_used == 0

    def test_to_dict_and_from_dict_roundtrip(self):
        state = TaskSessionState(
            session_id="wf_123",
            task_id="task_1",
            current_phase_index=2,
            rollback_retry_count=1,
            status="running",
            artifacts={"research": "found X"},
            total_tool_calls_used=42,
            start_time=datetime(2026, 3, 1, 12, 0, 0),
            git_start_commit="abc123",
        )
        d = state.to_dict()
        restored = TaskSessionState.from_dict(d)
        assert restored.session_id == "wf_123"
        assert restored.task_id == "task_1"
        assert restored.current_phase_index == 2
        assert restored.rollback_retry_count == 1
        assert restored.status == "running"
        assert restored.artifacts == {"research": "found X"}
        assert restored.total_tool_calls_used == 42
        assert restored.start_time == datetime(2026, 3, 1, 12, 0, 0)
        assert restored.git_start_commit == "abc123"

    def test_from_dict_minimal(self):
        restored = TaskSessionState.from_dict({"session_id": "wf_min"})
        assert restored.session_id == "wf_min"
        assert restored.status == "pending"
        assert restored.start_time is None


class TestCmdResult:
    def test_fields(self):
        r = CmdResult(exit_code=0, output="OK")
        assert r.exit_code == 0
        assert r.output == "OK"


class TestTaskSessionEvent:
    def test_creation(self):
        ev = TaskSessionEvent(
            event_type="phase_start",
            session_id="wf_1",
            phase_name="research",
        )
        assert ev.event_type == "phase_start"
        assert ev.session_id == "wf_1"
        assert ev.phase_name == "research"
        assert ev.data == {}
        assert ev.timestamp  # auto-generated


class TestPhaseResult:
    def test_defaults(self):
        r = PhaseResult()
        assert r.artifact == ""
        assert r.tool_calls_used == 0
        assert r.duration_seconds == 0.0


class TestWorkflowReport:
    def test_to_dict(self):
        report = WorkflowReport(
            session_id="wf_1",
            workflow_name="bugfix",
            status="done",
            total_duration_seconds=100.0,
            total_tool_calls=30,
            phase_traces=[
                PhaseTraceEntry(
                    phase_name="research",
                    status="completed",
                    tool_calls_used=10,
                ),
            ],
            files_modified=["src/foo.py"],
            final_artifact="fixed the bug",
        )
        d = report.to_dict()
        assert d["session_id"] == "wf_1"
        assert d["status"] == "done"
        assert len(d["phase_traces"]) == 1
        assert d["phase_traces"][0]["phase_name"] == "research"
        assert d["files_modified"] == ["src/foo.py"]

    def test_to_dict_with_verify_traces(self):
        report = WorkflowReport(
            session_id="wf_1",
            phase_traces=[
                PhaseTraceEntry(
                    phase_name="impl_verify",
                    phase_type="phase_group",
                    verify_traces=[
                        VerifyTraceEntry(iteration=1, passed=False, exit_code=1, output="fail"),
                        VerifyTraceEntry(iteration=2, passed=True, exit_code=0, output="pass"),
                    ],
                ),
            ],
        )
        d = report.to_dict()
        vt = d["phase_traces"][0]["verify_traces"]
        assert len(vt) == 2
        assert vt[0]["passed"] is False
        assert vt[1]["passed"] is True
