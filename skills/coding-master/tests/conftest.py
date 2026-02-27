"""Shared fixtures for coding-master skill tests."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


# ── Live test support ────────────────────────────────────

def pytest_addoption(parser):
    parser.addoption("--run-live", action="store_true", default=False,
                     help="Run live tests that call real engines (costs API credits)")


def pytest_configure(config):
    config.addinivalue_line("markers", "live: mark test as live (needs real engine)")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-live"):
        skip_live = pytest.mark.skip(reason="need --run-live to run")
        for item in items:
            if "live" in item.keywords:
                item.add_marker(skip_live)

# Add scripts/ to sys.path so tests can import skill modules directly
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from config_manager import ConfigManager  # noqa: E402

import yaml  # noqa: E402

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "t@t",
}


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


@pytest.fixture
def bare_repo(tmp_path):
    """Create a bare git repo that can be cloned from (acts as a 'remote')."""
    repo = tmp_path / "bare_origin"
    repo.mkdir()
    subprocess.run(["git", "init", "--bare"], cwd=str(repo), env=_GIT_ENV,
                    capture_output=True, check=True)
    # Create a temporary clone to push an initial commit
    clone = tmp_path / "init_clone"
    subprocess.run(["git", "clone", str(repo), str(clone)], env=_GIT_ENV,
                    capture_output=True, check=True)
    (clone / "README.md").write_text("# test repo\n")
    (clone / "pyproject.toml").write_text("[project]\nname = \"test\"\n")
    subprocess.run(["git", "add", "-A"], cwd=str(clone), env=_GIT_ENV,
                    capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=str(clone), env=_GIT_ENV,
                    capture_output=True, check=True)
    subprocess.run(["git", "push"], cwd=str(clone), env=_GIT_ENV,
                    capture_output=True, check=True)
    return repo


@pytest.fixture
def repo_config_manager(tmp_path, bare_repo):
    """ConfigManager with a repo and workspace slots configured."""
    ws0 = tmp_path / "workspaces" / "env0"
    ws1 = tmp_path / "workspaces" / "env1"
    ws0.mkdir(parents=True)
    ws1.mkdir(parents=True)

    cfg_path = tmp_path / "config.yaml"
    data = {
        "coding_master": {
            "repos": {
                "myrepo": str(bare_repo),
            },
            "workspaces": {
                "env0": str(ws0),
                "env1": str(ws1),
            },
            "envs": {},
            "default_engine": "claude",
            "max_turns": 10,
        }
    }
    cfg_path.write_text(yaml.dump(data))
    return ConfigManager(config_path=cfg_path)
