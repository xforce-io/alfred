"""Workflow YAML configuration loader with validation."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Union

import yaml

from .exceptions import ConfigValidationError
from .models import (
    PhaseConfig,
    PhaseGroupConfig,
    TaskSessionConfig,
    VerificationCmdConfig,
)

logger = logging.getLogger(__name__)


def load_workflow_config(skill_dir: str, workflow_name: str) -> TaskSessionConfig:
    """Load and validate a workflow YAML from a skill directory.

    Resolves ``{skill_dir}/workflows/{workflow_name}.yaml``.
    """
    yaml_path = os.path.join(skill_dir, "workflows", f"{workflow_name}.yaml")
    if not os.path.isfile(yaml_path):
        raise ConfigValidationError(
            f"Workflow file not found: {yaml_path}", path=yaml_path
        )
    with open(yaml_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ConfigValidationError(
            "Workflow YAML must be a mapping", path=yaml_path
        )
    return _parse_config(raw, yaml_path)


def _parse_config(raw: Dict[str, Any], yaml_path: str) -> TaskSessionConfig:
    """Parse raw YAML dict into TaskSessionConfig with validation."""
    raw_phases = raw.get("phases")
    if not raw_phases or not isinstance(raw_phases, list):
        raise ConfigValidationError(
            "'phases' must be a non-empty list", path=yaml_path
        )

    phases: List[Union[PhaseConfig, PhaseGroupConfig]] = []
    for i, item in enumerate(raw_phases):
        if not isinstance(item, dict):
            raise ConfigValidationError(
                f"phases[{i}] must be a mapping", path=yaml_path
            )
        if "group" in item:
            phases.append(_parse_phase_group(item, yaml_path, i))
        else:
            phases.append(_parse_phase(item, yaml_path, f"phases[{i}]"))

    config = TaskSessionConfig(
        name=raw.get("name", ""),
        description=raw.get("description", ""),
        phases=phases,
        total_timeout_seconds=int(raw.get("total_timeout_seconds", 1800)),
        total_max_tool_calls=int(raw.get("total_max_tool_calls", 200)),
        max_rollback_retries=int(raw.get("max_rollback_retries", 2)),
    )

    _validate_top_level(config, yaml_path)
    return config


def _parse_phase(
    raw: Dict[str, Any], yaml_path: str, ctx: str
) -> PhaseConfig:
    """Parse a single phase dict."""
    name = raw.get("name")
    if not name:
        raise ConfigValidationError(f"{ctx}: 'name' is required", path=yaml_path)

    verification_cmd = None
    raw_vcmd = raw.get("verification_cmd")
    if raw_vcmd:
        if isinstance(raw_vcmd, dict):
            verification_cmd = VerificationCmdConfig(
                cmd=raw_vcmd["cmd"],
                timeout_seconds=int(raw_vcmd.get("timeout_seconds", 120)),
                working_dir=raw_vcmd.get("working_dir"),
                env=raw_vcmd.get("env", {}),
            )
        elif isinstance(raw_vcmd, str):
            verification_cmd = VerificationCmdConfig(cmd=raw_vcmd)

    phase = PhaseConfig(
        name=name,
        instruction_ref=raw.get("instruction_ref"),
        max_turns=int(raw.get("max_turns", 10)),
        max_tool_calls=int(raw.get("max_tool_calls", 50)),
        timeout_seconds=int(raw.get("timeout_seconds", 300)),
        checkpoint=bool(raw.get("checkpoint", False)),
        completion_signal=raw.get("completion_signal", "llm_decision"),
        input_artifacts=raw.get("input_artifacts", []),
        allowed_tools=raw.get("allowed_tools"),
        on_failure=raw.get("on_failure", "abort"),
        max_retries=int(raw.get("max_retries", 1)),
        verification_cmd=verification_cmd,
        verify_protocol=raw.get("verify_protocol"),
    )

    # Rule 4: Mode mutual exclusion
    if phase.verification_cmd and phase.instruction_ref:
        logger.warning(
            "workflow.config.mode_conflict: phase '%s' has both verification_cmd "
            "and instruction_ref — LLM fields will be ignored",
            name,
        )

    # Rule 5: verification_cmd / verify_protocol mutually exclusive
    if phase.verification_cmd and phase.verify_protocol:
        raise ConfigValidationError(
            f"{ctx}: phase '{name}' cannot have both verification_cmd and verify_protocol",
            path=yaml_path,
        )

    # At least one mode must be set (unless it's only used as a cmd phase)
    if not phase.verification_cmd and not phase.instruction_ref:
        raise ConfigValidationError(
            f"{ctx}: phase '{name}' must have either instruction_ref or verification_cmd",
            path=yaml_path,
        )

    return phase


def _parse_phase_group(
    raw: Dict[str, Any], yaml_path: str, index: int
) -> PhaseGroupConfig:
    """Parse a PhaseGroup dict with inner phases."""
    ctx = f"phases[{index}]"
    group_name = raw.get("group")
    if not group_name:
        raise ConfigValidationError(
            f"{ctx}: 'group' name is required", path=yaml_path
        )

    action_phase = raw.get("action_phase")
    verify_phase = raw.get("verify_phase")
    if not action_phase or not verify_phase:
        raise ConfigValidationError(
            f"{ctx}: PhaseGroup '{group_name}' requires 'action_phase' and 'verify_phase'",
            path=yaml_path,
        )

    inner_phases_raw = raw.get("phases", [])
    if not isinstance(inner_phases_raw, list):
        raise ConfigValidationError(
            f"{ctx}: PhaseGroup '{group_name}' 'phases' must be a list",
            path=yaml_path,
        )

    inner_phases = []
    for j, p in enumerate(inner_phases_raw):
        inner_phases.append(
            _parse_phase(p, yaml_path, f"{ctx}.phases[{j}]")
        )

    group = PhaseGroupConfig(
        name=group_name,
        action_phase=action_phase,
        verify_phase=verify_phase,
        setup_phase=raw.get("setup_phase"),
        max_iterations=int(raw.get("max_iterations", 5)),
        phases=inner_phases,
        on_exhausted=raw.get("on_exhausted", "rollback"),
        rollback_target=raw.get("rollback_target"),
    )

    _validate_phase_group(group, yaml_path, ctx)
    return group


def _validate_phase_group(
    group: PhaseGroupConfig, yaml_path: str, ctx: str
) -> None:
    """Validate all 8 rules for a PhaseGroup."""
    phase_names = {p.name for p in group.phases}

    # Rule 1: action_phase and verify_phase must reference existing phases
    if group.action_phase not in phase_names:
        raise ConfigValidationError(
            f"{ctx}: action_phase '{group.action_phase}' not found in group phases",
            path=yaml_path,
        )
    if group.verify_phase not in phase_names:
        raise ConfigValidationError(
            f"{ctx}: verify_phase '{group.verify_phase}' not found in group phases",
            path=yaml_path,
        )

    # Rule 2: verify_phase must have verification_cmd or verify_protocol
    verify_cfg = next(p for p in group.phases if p.name == group.verify_phase)
    if not verify_cfg.verification_cmd and not verify_cfg.verify_protocol:
        raise ConfigValidationError(
            f"{ctx}: verify_phase '{group.verify_phase}' must have "
            "verification_cmd or verify_protocol",
            path=yaml_path,
        )

    # Rule 3: at least 2 phases
    if len(group.phases) < 2:
        raise ConfigValidationError(
            f"{ctx}: PhaseGroup '{group.name}' must have at least 2 phases",
            path=yaml_path,
        )

    # Rule 7: setup_phase validation
    if group.setup_phase:
        if group.setup_phase not in phase_names:
            raise ConfigValidationError(
                f"{ctx}: setup_phase '{group.setup_phase}' not found in group phases",
                path=yaml_path,
            )
        if group.setup_phase in (group.action_phase, group.verify_phase):
            raise ConfigValidationError(
                f"{ctx}: setup_phase cannot be the same as action_phase or verify_phase",
                path=yaml_path,
            )

    # Rule: on_exhausted=rollback requires rollback_target
    if group.on_exhausted == "rollback" and not group.rollback_target:
        raise ConfigValidationError(
            f"{ctx}: on_exhausted='rollback' requires rollback_target",
            path=yaml_path,
        )


def _validate_top_level(config: TaskSessionConfig, yaml_path: str) -> None:
    """Top-level cross-phase validation."""
    # Collect all top-level phase names for rollback_target validation
    top_level_names: List[str] = []
    for i, step in enumerate(config.phases):
        if isinstance(step, PhaseConfig):
            top_level_names.append(step.name)
        elif isinstance(step, PhaseGroupConfig):
            top_level_names.append(step.name)

    # Rule 6: rollback_target must reference a valid top-level Phase before current group
    for i, step in enumerate(config.phases):
        if isinstance(step, PhaseGroupConfig) and step.rollback_target:
            # Find rollback target among top-level items before this group
            found = False
            for j in range(i):
                prev = config.phases[j]
                if isinstance(prev, PhaseConfig) and prev.name == step.rollback_target:
                    found = True
                    break
            if not found:
                raise ConfigValidationError(
                    f"PhaseGroup '{step.name}' rollback_target '{step.rollback_target}' "
                    f"must reference a top-level Phase defined before this group",
                    path=yaml_path,
                )
