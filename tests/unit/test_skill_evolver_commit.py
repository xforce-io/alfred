"""Unit tests for skill-evolver commit.py."""
from __future__ import annotations

import importlib.util
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
