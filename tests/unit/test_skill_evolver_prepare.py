"""Unit tests for skill-evolver prepare.py."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "skills" / "skill-evolver" / "scripts" / "prepare.py"


def _load_module():
    """Import prepare.py as a module without running its CLI."""
    spec = importlib.util.spec_from_file_location("skill_evolver_prepare", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["skill_evolver_prepare"] = module
    spec.loader.exec_module(module)
    return module


class TestExtractBase:
    def test_plain_version(self):
        m = _load_module()
        assert m._extract_base("2.0.0") == "2.0.0"

    def test_strips_evolve_suffix(self):
        m = _load_module()
        assert m._extract_base("2.0.0-evolve-202604260331") == "2.0.0"

    def test_strips_userevolve_suffix(self):
        m = _load_module()
        assert m._extract_base("2.0.0-userevolve-202605101630") == "2.0.0"

    def test_baseline_passthrough(self):
        m = _load_module()
        assert m._extract_base("baseline") == "baseline"


class TestNewVersion:
    def test_format(self):
        m = _load_module()
        # Patch datetime by passing an explicit timestamp arg
        v = m._new_version("2.0.0", ts="202605101630")
        assert v == "2.0.0-userevolve-202605101630"

    def test_strips_existing_suffix_first(self):
        m = _load_module()
        v = m._new_version("2.0.0-evolve-202604260331", ts="202605101630")
        assert v == "2.0.0-userevolve-202605101630"


import json
import subprocess


PYTHON = "/Users/xupeng/dev/github/alfred/.venv/bin/python"


def _seed_skill(skills_dir: Path, skill_id: str, content: str) -> Path:
    skill_dir = skills_dir / skill_id
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    return skill_dir


class TestPrepareCli:
    def test_returns_json_with_required_keys(self, tmp_path: Path):
        # Workspace at tmp_path/agents/test_agent
        workspace = tmp_path / "agents" / "test_agent"
        writable_skills = workspace / "skills"
        skill_md_content = (
            "---\n"
            'name: target-skill\n'
            'version: "1.5.0"\n'
            "---\n\n"
            "# Target Skill\n\nBody text.\n"
        )
        _seed_skill(writable_skills, "target-skill", skill_md_content)

        result = subprocess.run(
            [PYTHON, str(SCRIPT_PATH),
             "--workspace", str(workspace),
             "--skill", "target-skill"],
            capture_output=True, text=True, env={"ALFRED_HOME": str(tmp_path)},
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)

        assert payload["current_skill_md"] == skill_md_content
        assert payload["new_version"].startswith("1.5.0-userevolve-")
        assert payload["tmp_file"].endswith(".md")
        assert "skill-evolver-target-skill-" in payload["tmp_file"]
