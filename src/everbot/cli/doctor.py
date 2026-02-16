"""
Doctor checks for EverBot runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import os
import re

import yaml

from ..infra.user_data import UserDataManager


@dataclass(frozen=True)
class DoctorItem:
    """A single doctor finding."""

    level: str  # "OK" | "WARN" | "ERROR"
    title: str
    details: str
    hint: Optional[str] = None


def resolve_dolphin_config_path(user_data: UserDataManager, project_root: Path) -> Tuple[str, Optional[Path]]:
    """
    Resolve which Dolphin config file is effectively used.

    Returns:
        (source_label, path or None)
    """
    candidates = [
        ("alfred", user_data.dolphin_config_path),
        ("project", (project_root / "config" / "dolphin.yaml").resolve()),
        ("cwd", Path("./config/dolphin.yaml").resolve()),
    ]

    for label, path in candidates:
        if path.exists():
            return (label, path)
    return ("default", None)


def parse_yaml_file(path: Path) -> Dict[str, Any]:
    """Parse YAML file to dict; returns empty dict if not parsable."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def dolphin_has_system_skillkit(config: Dict[str, Any]) -> bool:
    """Check if system_skillkit is enabled in Dolphin config."""
    skill = config.get("skill", {}) if isinstance(config.get("skill", {}), dict) else {}
    enabled = skill.get("enabled_skills", [])
    if not isinstance(enabled, list):
        return False
    return any(str(x).strip() == "system_skillkit" for x in enabled)


def detect_agent_dph_format(agent_dph_content: str) -> str:
    """Detect whether agent.dph looks like DPH or legacy YAML."""
    raw = agent_dph_content or ""
    if "->" in raw or ">>" in raw:
        return "dph"
    # Very rough legacy detection
    if re.search(r"^\s*system_prompt\s*:\s*\|", raw, flags=re.M) or re.search(r"^\s*model\s*:\s*$", raw, flags=re.M):
        return "legacy_yaml"
    return "unknown"


def dph_declares_read_file_tool(agent_dph_content: str) -> bool:
    """Heuristic: check if _read_file is declared in tools=[...] in DPH."""
    raw = agent_dph_content or ""
    m = re.search(r"tools\s*=\s*\[([^\]]*)\]", raw)
    if not m:
        return False
    tools = m.group(1)
    return "_read_file" in tools


def collect_doctor_report(
    *,
    project_root: Path,
    alfred_home: Optional[Path] = None,
) -> List[DoctorItem]:
    """Collect doctor report items."""
    user_data = UserDataManager(alfred_home=alfred_home)
    items: List[DoctorItem] = []

    # EverBot config
    if user_data.config_path.exists():
        items.append(
            DoctorItem(
                level="OK",
                title="EverBot config",
                details=f"Found config: {user_data.config_path}",
            )
        )
        everbot_cfg = parse_yaml_file(user_data.config_path)
        enabled = bool((everbot_cfg.get("everbot", {}) or {}).get("enabled", True))
        if not enabled:
            items.append(
                DoctorItem(
                    level="WARN",
                    title="EverBot enabled",
                    details="everbot.enabled is false.",
                    hint="Edit ~/.alfred/config.yaml and set everbot.enabled: true",
                )
            )
    else:
        items.append(
            DoctorItem(
                level="WARN",
                title="EverBot config",
                details=f"Config not found: {user_data.config_path}",
                hint="Create ~/.alfred/config.yaml (copy from config/everbot.example.yaml).",
            )
        )

    # Dolphin config
    source, dolphin_path = resolve_dolphin_config_path(user_data, project_root)
    if dolphin_path is None:
        items.append(
            DoctorItem(
                level="WARN",
                title="Dolphin config",
                details="No Dolphin YAML config found; using Dolphin defaults.",
                hint="Create ~/.alfred/dolphin.yaml (copy from config/dolphin.yaml).",
            )
        )
        dolphin_cfg: Dict[str, Any] = {}
    else:
        items.append(
            DoctorItem(
                level="OK",
                title="Dolphin config",
                details=f"Using {source} config: {dolphin_path}",
            )
        )
        dolphin_cfg = parse_yaml_file(dolphin_path)

    if dolphin_path is not None and not dolphin_has_system_skillkit(dolphin_cfg):
        items.append(
            DoctorItem(
                level="WARN",
                title="system_skillkit",
                details="system_skillkit is not enabled in Dolphin config.",
                hint='Add "system_skillkit" under skill.enabled_skills in dolphin.yaml.',
            )
        )
    else:
        items.append(
            DoctorItem(
                level="OK",
                title="system_skillkit",
                details="system_skillkit enabled (or Dolphin defaults assumed).",
            )
        )

    # Web dependencies (optional)
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
        items.append(
            DoctorItem(
                level="OK",
                title="Web deps",
                details="fastapi/uvicorn installed.",
            )
        )
    except Exception:
        items.append(
            DoctorItem(
                level="WARN",
                title="Web deps",
                details="fastapi/uvicorn not installed; Web UI may not start.",
                hint="Run: pip install fastapi uvicorn",
            )
        )

    # Agent workspaces
    if not user_data.agents_dir.exists():
        items.append(
            DoctorItem(
                level="WARN",
                title="Agents dir",
                details=f"Agents dir missing: {user_data.agents_dir}",
                hint="Run: bin/everbot init <agent_name>",
            )
        )
        return items

    agent_dirs = sorted([p for p in user_data.agents_dir.iterdir() if p.is_dir()])
    if not agent_dirs:
        items.append(
            DoctorItem(
                level="WARN",
                title="Agents",
                details="No agents found under ~/.alfred/agents.",
                hint="Run: bin/everbot init <agent_name>",
            )
        )
        return items

    for agent_dir in agent_dirs:
        agent_name = agent_dir.name
        agent_dph = agent_dir / "agent.dph"
        if not agent_dph.exists():
            items.append(
                DoctorItem(
                    level="WARN",
                    title=f"Agent {agent_name}",
                    details=f"Missing agent.dph: {agent_dph}",
                    hint=f"Run: bin/everbot init {agent_name}",
                )
            )
            continue

        content = ""
        try:
            content = agent_dph.read_text(encoding="utf-8")
        except Exception as e:
            items.append(
                DoctorItem(
                    level="ERROR",
                    title=f"Agent {agent_name}",
                    details=f"Failed to read agent.dph: {e}",
                )
            )
            continue

        fmt = detect_agent_dph_format(content)
        if fmt == "legacy_yaml":
            items.append(
                DoctorItem(
                    level="WARN",
                    title=f"Agent {agent_name} agent.dph",
                    details="Legacy YAML-style agent.dph detected.",
                    hint=f"Run: bin/everbot migrate-agent --agent {agent_name}",
                )
            )
        elif fmt == "unknown":
            items.append(
                DoctorItem(
                    level="WARN",
                    title=f"Agent {agent_name} agent.dph",
                    details="Unknown agent.dph format; Dolphin may fail to parse it.",
                    hint=f"Open: {agent_dph} and ensure it contains DPH blocks with '->'.",
                )
            )
        else:
            items.append(
                DoctorItem(
                    level="OK",
                    title=f"Agent {agent_name} agent.dph",
                    details="DPH format detected.",
                )
            )

        if dolphin_path is not None and dolphin_has_system_skillkit(dolphin_cfg):
            if not dph_declares_read_file_tool(content):
                items.append(
                    DoctorItem(
                        level="WARN",
                        title=f"Agent {agent_name} tools",
                        details="agent.dph does not declare _read_file in tools=[...].",
                        hint="Add _read_file/_read_folder to tools list in agent.dph.",
                    )
                )

        # Basic env check for Aliyun model config
        if dolphin_path is not None:
            clouds = dolphin_cfg.get("clouds", {}) if isinstance(dolphin_cfg.get("clouds", {}), dict) else {}
            default_cloud = (clouds.get("default") if isinstance(clouds, dict) else None) or ""
            if str(default_cloud).strip() == "aliyun":
                if not os.getenv("ALIYUN_API_KEY"):
                    items.append(
                        DoctorItem(
                            level="WARN",
                            title="ALIYUN_API_KEY",
                            details="Environment variable ALIYUN_API_KEY is not set.",
                            hint="Export ALIYUN_API_KEY before starting EverBot.",
                        )
                    )
                break

    return items
