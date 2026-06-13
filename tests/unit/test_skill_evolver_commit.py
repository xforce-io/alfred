"""Unit tests for skill-evolver commit.py."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "skills" / "skill-evolver" / "scripts" / "commit.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("skill_evolver_commit", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["skill_evolver_commit"] = module
    spec.loader.exec_module(module)
    return module


class TestValidateContent:
    def test_valid_frontmatter_matches_expected_version(self):
        m = _load_module()
        content = (
            '---\n'
            'name: foo\n'
            'version: "2.0.0-userevolve-202605101630"\n'
            '---\n\n# Foo\n'
        )
        # No exception raised
        m._validate_content(content, expected_version="2.0.0-userevolve-202605101630")

    def test_missing_frontmatter_raises(self):
        m = _load_module()
        with pytest.raises(ValueError, match="frontmatter"):
            m._validate_content("# Just a body\n", expected_version="x")

    def test_missing_version_field_raises(self):
        m = _load_module()
        content = '---\nname: foo\n---\n\n# Foo\n'
        with pytest.raises(ValueError, match="version"):
            m._validate_content(content, expected_version="x")

    def test_version_mismatch_raises(self):
        m = _load_module()
        content = (
            '---\n'
            'name: foo\n'
            'version: "1.0.0"\n'
            '---\n'
        )
        with pytest.raises(ValueError, match="version mismatch"):
            m._validate_content(content, expected_version="2.0.0-userevolve-202605101630")


def _seed_workspace(tmp_path: Path, agent_name: str, skill_id: str, baseline_content: str) -> Path:
    """Create a minimal alfred home + agent workspace + writable skill dir."""
    workspace = tmp_path / "agents" / agent_name
    writable_skills = workspace / "skills" / skill_id
    writable_skills.mkdir(parents=True)
    (writable_skills / "SKILL.md").write_text(baseline_content, encoding="utf-8")
    # Eval dir gets created lazily by VersionManager.publish.
    return workspace


class TestCommitCli:
    def test_publish_writes_new_version_and_updates_pointer(self, tmp_path: Path):
        baseline = (
            '---\n'
            'name: target-skill\n'
            'version: "1.0.0"\n'
            '---\n\n# Target\nbaseline body\n'
        )
        workspace = _seed_workspace(tmp_path, "test_agent", "target-skill", baseline)

        new_version = "1.0.0-userevolve-202605101630"
        new_content = (
            '---\n'
            'name: target-skill\n'
            f'version: "{new_version}"\n'
            '---\n\n# Target\nrewritten body\n'
        )
        content_file = tmp_path / "rewritten.md"
        content_file.write_text(new_content, encoding="utf-8")

        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH),
             "--workspace", str(workspace),
             "--skill", "target-skill",
             "--version", new_version,
             "--content-file", str(content_file)],
            capture_output=True, text=True, env={"ALFRED_HOME": str(tmp_path)},
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["status"] == "ok"
        assert payload["version"] == new_version
        assert payload["current_pointer"] == new_version

        # SKILL.md is overwritten in the writable layer
        live = workspace / "skills" / "target-skill" / "SKILL.md"
        assert "rewritten body" in live.read_text(encoding="utf-8")

        # Snapshot and pointer exist in skill_eval
        snapshot = workspace / "skill_eval" / "target-skill" / "versions" / f"v{new_version}" / "skill.md"
        assert snapshot.exists()
        pointer = json.loads(
            (workspace / "skill_eval" / "target-skill" / "current.json").read_text(encoding="utf-8")
        )
        assert pointer["current_version"] == new_version
        assert pointer["consecutive_evolve_count"] == 0

    def test_missing_content_file_returns_error(self, tmp_path: Path):
        workspace = _seed_workspace(
            tmp_path, "test_agent", "target-skill",
            '---\nname: target-skill\nversion: "1.0.0"\n---\n',
        )
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH),
             "--workspace", str(workspace),
             "--skill", "target-skill",
             "--version", "1.0.0-userevolve-202605101630",
             "--content-file", str(tmp_path / "does-not-exist.md")],
            capture_output=True, text=True, env={"ALFRED_HOME": str(tmp_path)},
        )
        assert result.returncode != 0
        payload = json.loads(result.stdout)
        assert payload["status"] == "error"
        assert "content file not found" in payload["error"]

    def test_version_mismatch_returns_error(self, tmp_path: Path):
        baseline = '---\nname: target-skill\nversion: "1.0.0"\n---\n'
        workspace = _seed_workspace(tmp_path, "test_agent", "target-skill", baseline)
        bad_content = (
            '---\nname: target-skill\nversion: "1.5.0"\n---\n\nbody\n'
        )
        content_file = tmp_path / "bad.md"
        content_file.write_text(bad_content, encoding="utf-8")

        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH),
             "--workspace", str(workspace),
             "--skill", "target-skill",
             "--version", "1.0.0-userevolve-202605101630",
             "--content-file", str(content_file)],
            capture_output=True, text=True, env={"ALFRED_HOME": str(tmp_path)},
        )
        assert result.returncode != 0
        payload = json.loads(result.stdout)
        assert payload["status"] == "error"
        assert "mismatch" in payload["error"]
