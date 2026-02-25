"""Shared fixtures for coding-master skill tests."""

import sys
from pathlib import Path

import pytest

# Add scripts/ to sys.path so tests can import skill modules directly
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from config_manager import ConfigManager  # noqa: E402

import yaml  # noqa: E402


@pytest.fixture
def ws_dir(tmp_path):
    """Create a temporary workspace directory with a .git folder."""
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    return tmp_path


@pytest.fixture
def config_file(tmp_path, ws_dir):
    """Create a temporary config.yaml with a preset workspace and env."""
    cfg_path = tmp_path / "config.yaml"
    data = {
        "coding_master": {
            "workspaces": {
                "test-ws": str(ws_dir),
            },
            "envs": {
                "test-ws-local": str(ws_dir),
            },
            "default_engine": "claude",
            "max_turns": 10,
        }
    }
    cfg_path.write_text(yaml.dump(data))
    return cfg_path


@pytest.fixture
def config_manager(config_file):
    """ConfigManager backed by the temporary config file."""
    return ConfigManager(config_path=config_file)
