"""Unit tests for workflow config_loader with all 8 validation rules."""

import os
import tempfile

import pytest
import yaml

from src.everbot.core.workflow.config_loader import (
    _parse_config,
    load_workflow_config,
)
from src.everbot.core.workflow.exceptions import ConfigValidationError
from src.everbot.core.workflow.models import (
    PhaseConfig,
    PhaseGroupConfig,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _valid_group_raw(
    group_name="impl_verify",
    action="implement",
    verify="verify",
    on_exhausted="abort",
    rollback_target=None,
    setup_phase=None,
    extra_phases=None,
):
    """Return a valid PhaseGroup dict for testing."""
    phases = [
        {"name": action, "instruction_ref": "sop.md"},
        {"name": verify, "verification_cmd": {"cmd": "pytest"}},
    ]
    if extra_phases:
        phases.extend(extra_phases)
    d = {
        "group": group_name,
        "action_phase": action,
        "verify_phase": verify,
        "on_exhausted": on_exhausted,
        "phases": phases,
    }
    if rollback_target:
        d["rollback_target"] = rollback_target
    if setup_phase:
        d["setup_phase"] = setup_phase
    return d


def _write_yaml(tmp_dir, workflow_name, data):
    """Write a workflow YAML file and return skill_dir."""
    wf_dir = os.path.join(tmp_dir, "workflows")
    os.makedirs(wf_dir, exist_ok=True)
    path = os.path.join(wf_dir, f"{workflow_name}.yaml")
    with open(path, "w") as f:
        yaml.dump(data, f)
    return tmp_dir


# ---------------------------------------------------------------------------
# Basic loading
# ---------------------------------------------------------------------------

class TestLoadWorkflowConfig:
    def test_load_valid_yaml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data = {
                "name": "bugfix",
                "description": "Fix bugs",
                "phases": [
                    {"name": "research", "instruction_ref": "sop.md"},
                    {"name": "plan", "instruction_ref": "sop.md", "checkpoint": True},
                ],
                "total_timeout_seconds": 600,
                "total_max_tool_calls": 100,
            }
            skill_dir = _write_yaml(tmpdir, "bugfix", data)
            config = load_workflow_config(skill_dir, "bugfix")
            assert config.name == "bugfix"
            assert len(config.phases) == 2
            assert config.total_timeout_seconds == 600

    def test_file_not_found(self):
        with pytest.raises(ConfigValidationError, match="not found"):
            load_workflow_config("/nonexistent", "missing")

    def test_invalid_yaml_structure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            wf_dir = os.path.join(tmpdir, "workflows")
            os.makedirs(wf_dir)
            path = os.path.join(wf_dir, "bad.yaml")
            with open(path, "w") as f:
                f.write("- just a list\n")
            with pytest.raises(ConfigValidationError, match="mapping"):
                load_workflow_config(tmpdir, "bad")

    def test_missing_phases(self):
        with pytest.raises(ConfigValidationError, match="phases"):
            _parse_config({"name": "no_phases"}, "test.yaml")

    def test_empty_phases(self):
        with pytest.raises(ConfigValidationError, match="non-empty"):
            _parse_config({"phases": []}, "test.yaml")


class TestParsePhase:
    def test_parse_llm_phase(self):
        config = _parse_config({
            "phases": [{"name": "research", "instruction_ref": "sop.md", "max_turns": 3}]
        }, "test.yaml")
        phase = config.phases[0]
        assert isinstance(phase, PhaseConfig)
        assert phase.name == "research"
        assert phase.max_turns == 3

    def test_parse_cmd_phase(self):
        config = _parse_config({
            "phases": [{"name": "verify", "verification_cmd": {"cmd": "pytest", "timeout_seconds": 60}}]
        }, "test.yaml")
        phase = config.phases[0]
        assert phase.verification_cmd.cmd == "pytest"
        assert phase.verification_cmd.timeout_seconds == 60

    def test_parse_cmd_string_shorthand(self):
        config = _parse_config({
            "phases": [{"name": "verify", "verification_cmd": "pytest"}]
        }, "test.yaml")
        assert config.phases[0].verification_cmd.cmd == "pytest"

    def test_missing_name(self):
        with pytest.raises(ConfigValidationError, match="name"):
            _parse_config({"phases": [{"instruction_ref": "sop.md"}]}, "test.yaml")

    def test_parse_phase_with_all_fields(self):
        config = _parse_config({
            "phases": [{
                "name": "implement",
                "instruction_ref": "sop.md",
                "max_turns": 15,
                "max_tool_calls": 80,
                "timeout_seconds": 600,
                "checkpoint": True,
                "completion_signal": "max_turns",
                "input_artifacts": ["plan"],
                "allowed_tools": ["_bash", "_read_file"],
                "on_failure": "retry",
                "max_retries": 3,
            }]
        }, "test.yaml")
        p = config.phases[0]
        assert p.max_turns == 15
        assert p.max_tool_calls == 80
        assert p.checkpoint is True
        assert p.input_artifacts == ["plan"]
        assert p.allowed_tools == ["_bash", "_read_file"]
        assert p.on_failure == "retry"
        assert p.max_retries == 3


class TestParsePhaseGroup:
    def test_parse_valid_group(self):
        config = _parse_config({
            "phases": [_valid_group_raw()]
        }, "test.yaml")
        group = config.phases[0]
        assert isinstance(group, PhaseGroupConfig)
        assert group.name == "impl_verify"
        assert group.action_phase == "implement"
        assert group.verify_phase == "verify"

    def test_group_with_setup_phase(self):
        raw = _valid_group_raw(setup_phase="setup", extra_phases=[
            {"name": "setup", "instruction_ref": "setup.md"},
        ])
        config = _parse_config({"phases": [raw]}, "test.yaml")
        group = config.phases[0]
        assert group.setup_phase == "setup"


# ---------------------------------------------------------------------------
# Validation rules
# ---------------------------------------------------------------------------

class TestValidationRule1:
    """action_phase and verify_phase must reference phases list."""

    def test_action_phase_not_found(self):
        with pytest.raises(ConfigValidationError, match="action_phase.*not_exist.*not found"):
            _parse_config({
                "phases": [{
                    "group": "g1",
                    "action_phase": "not_exist",
                    "verify_phase": "verify",
                    "phases": [
                        {"name": "implement", "instruction_ref": "sop.md"},
                        {"name": "verify", "verification_cmd": "pytest"},
                    ],
                }]
            }, "test.yaml")

    def test_verify_phase_not_found(self):
        with pytest.raises(ConfigValidationError, match="verify_phase.*not_exist.*not found"):
            _parse_config({
                "phases": [{
                    "group": "g1",
                    "action_phase": "implement",
                    "verify_phase": "not_exist",
                    "phases": [
                        {"name": "implement", "instruction_ref": "sop.md"},
                        {"name": "verify", "verification_cmd": "pytest"},
                    ],
                }]
            }, "test.yaml")


class TestValidationRule2:
    """verify_phase must have verification_cmd or verify_protocol."""

    def test_verify_phase_no_verification(self):
        with pytest.raises(ConfigValidationError, match="verify_phase.*must have"):
            _parse_config({
                "phases": [{
                    "group": "g1",
                    "action_phase": "implement",
                    "verify_phase": "check",
                    "phases": [
                        {"name": "implement", "instruction_ref": "sop.md"},
                        {"name": "check", "instruction_ref": "check.md"},
                    ],
                }]
            }, "test.yaml")

    def test_verify_phase_with_verify_protocol(self):
        """verify_protocol is acceptable."""
        config = _parse_config({
            "phases": [{
                "group": "g1",
                "action_phase": "implement",
                "verify_phase": "check",
                "on_exhausted": "abort",
                "phases": [
                    {"name": "implement", "instruction_ref": "sop.md"},
                    {"name": "check", "instruction_ref": "check.md", "verify_protocol": "structured_tag"},
                ],
            }]
        }, "test.yaml")
        assert isinstance(config.phases[0], PhaseGroupConfig)


class TestValidationRule3:
    """phases list must have at least 2 elements."""

    def test_too_few_phases(self):
        with pytest.raises(ConfigValidationError, match="at least 2"):
            _parse_config({
                "phases": [{
                    "group": "g1",
                    "action_phase": "implement",
                    "verify_phase": "implement",  # same as action, but catches rule 3 first
                    "phases": [
                        {"name": "implement", "instruction_ref": "sop.md", "verify_protocol": "structured_tag"},
                    ],
                }]
            }, "test.yaml")


class TestValidationRule4:
    """Mode mutual exclusion: verification_cmd + instruction_ref logs warning."""

    def test_both_modes_logs_warning(self, caplog):
        """Should log a warning but not raise."""
        import logging
        with caplog.at_level(logging.WARNING):
            _parse_config({
                "phases": [{
                    "name": "v",
                    "instruction_ref": "sop.md",
                    "verification_cmd": {"cmd": "pytest"},
                }]
            }, "test.yaml")
        assert "mode_conflict" in caplog.text or "verification_cmd" in caplog.text


class TestValidationRule5:
    """verification_cmd and verify_protocol are mutually exclusive."""

    def test_both_raises(self):
        with pytest.raises(ConfigValidationError, match="cannot have both"):
            _parse_config({
                "phases": [{
                    "name": "v",
                    "instruction_ref": "sop.md",
                    "verification_cmd": {"cmd": "pytest"},
                    "verify_protocol": "structured_tag",
                }]
            }, "test.yaml")


class TestValidationRule6:
    """rollback_target must reference a Phase before current group."""

    def test_rollback_target_after_group(self):
        with pytest.raises(ConfigValidationError, match="rollback_target.*before"):
            _parse_config({
                "phases": [
                    _valid_group_raw(on_exhausted="rollback", rollback_target="later"),
                    {"name": "later", "instruction_ref": "sop.md"},
                ]
            }, "test.yaml")

    def test_rollback_target_nonexistent(self):
        with pytest.raises(ConfigValidationError, match="rollback_target.*before"):
            _parse_config({
                "phases": [
                    _valid_group_raw(on_exhausted="rollback", rollback_target="ghost"),
                ]
            }, "test.yaml")

    def test_rollback_target_before_group_ok(self):
        config = _parse_config({
            "phases": [
                {"name": "plan", "instruction_ref": "sop.md"},
                _valid_group_raw(on_exhausted="rollback", rollback_target="plan"),
            ]
        }, "test.yaml")
        group = config.phases[1]
        assert isinstance(group, PhaseGroupConfig)
        assert group.rollback_target == "plan"

    def test_rollback_target_to_group_rejected(self):
        """rollback_target must point to a Phase, not another PhaseGroup."""
        with pytest.raises(ConfigValidationError, match="rollback_target.*before"):
            _parse_config({
                "phases": [
                    _valid_group_raw(group_name="g1", on_exhausted="abort"),
                    _valid_group_raw(group_name="g2", on_exhausted="rollback", rollback_target="g1"),
                ]
            }, "test.yaml")


class TestValidationRule7:
    """setup_phase must exist in phases and differ from action/verify."""

    def test_setup_phase_not_found(self):
        with pytest.raises(ConfigValidationError, match="setup_phase.*not found"):
            raw = _valid_group_raw(setup_phase="ghost")
            _parse_config({"phases": [raw]}, "test.yaml")

    def test_setup_phase_same_as_action(self):
        with pytest.raises(ConfigValidationError, match="setup_phase cannot be"):
            raw = _valid_group_raw(setup_phase="implement")
            _parse_config({"phases": [raw]}, "test.yaml")

    def test_setup_phase_same_as_verify(self):
        with pytest.raises(ConfigValidationError, match="setup_phase cannot be"):
            raw = _valid_group_raw(setup_phase="verify")
            _parse_config({"phases": [raw]}, "test.yaml")


class TestValidationRollbackRequiresTarget:
    """on_exhausted=rollback requires rollback_target."""

    def test_rollback_without_target(self):
        with pytest.raises(ConfigValidationError, match="requires rollback_target"):
            _parse_config({
                "phases": [{
                    "group": "g1",
                    "action_phase": "implement",
                    "verify_phase": "verify",
                    "on_exhausted": "rollback",
                    # missing rollback_target
                    "phases": [
                        {"name": "implement", "instruction_ref": "sop.md"},
                        {"name": "verify", "verification_cmd": "pytest"},
                    ],
                }]
            }, "test.yaml")


class TestValidationNoMode:
    """Phase must have either instruction_ref or verification_cmd."""

    def test_no_mode(self):
        with pytest.raises(ConfigValidationError, match="must have either"):
            _parse_config({
                "phases": [{"name": "empty"}]
            }, "test.yaml")
