"""E2E test configuration for ops skill against a real Alfred environment."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

# ---------------------------------------------------------------------------
# Default environment registry
# ---------------------------------------------------------------------------

E2E_ENV = {
    "env0": {
        "project_root": "~/lab/env0",
        "alfred_home": "~/lab/env0/.alfred",
    },
}


# ---------------------------------------------------------------------------
# pytest CLI options
# ---------------------------------------------------------------------------

def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--e2e-env", default="env0",
        help="E2E environment name from E2E_ENV registry (default: env0)",
    )
    parser.addoption(
        "--e2e-project-root", default=None,
        help="Override project_root (path containing bin/everbot)",
    )
    parser.addoption(
        "--e2e-alfred-home", default=None,
        help="Override ALFRED_HOME path",
    )
    parser.addoption(
        "--run-destructive", action="store_true", default=False,
        help="Enable destructive lifecycle tests (start/stop/restart)",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "destructive: marks tests that mutate daemon state (start/stop/restart)",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: List[pytest.Item],
) -> None:
    if config.getoption("--run-destructive"):
        return
    skip = pytest.mark.skip(reason="needs --run-destructive option to run")
    for item in items:
        if "destructive" in item.keywords:
            item.add_marker(skip)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def e2e_env(request: pytest.FixtureRequest) -> Dict[str, Path]:
    """Resolve and validate the E2E environment paths.

    Returns dict with keys ``project_root`` and ``alfred_home`` as Paths.
    Skips the entire session if the environment is not available.
    """
    env_name = request.config.getoption("--e2e-env")
    env_cfg = E2E_ENV.get(env_name, E2E_ENV["env0"])

    project_root = request.config.getoption("--e2e-project-root")
    if project_root is None:
        project_root = env_cfg["project_root"]
    project_root = Path(project_root).expanduser().resolve()

    alfred_home = request.config.getoption("--e2e-alfred-home")
    if alfred_home is None:
        alfred_home = env_cfg["alfred_home"]
    alfred_home = Path(alfred_home).expanduser().resolve()

    everbot_bin = project_root / "bin" / "everbot"
    if not everbot_bin.exists():
        pytest.skip(
            f"E2E environment not available: {everbot_bin} does not exist",
        )

    return {
        "project_root": project_root,
        "alfred_home": alfred_home,
    }


@pytest.fixture(scope="session")
def everbot_bin(e2e_env: Dict[str, Path]) -> Path:
    """Absolute path to ``bin/everbot``."""
    return e2e_env["project_root"] / "bin" / "everbot"


@pytest.fixture(scope="session")
def alfred_home(e2e_env: Dict[str, Path]) -> Path:
    """Absolute path to ALFRED_HOME."""
    return e2e_env["alfred_home"]


@pytest.fixture(scope="session")
def ops_cli_path() -> Path:
    """Absolute path to ``ops_cli.py``."""
    return (
        Path(__file__).resolve().parent.parent.parent
        / "skills" / "ops" / "scripts" / "ops_cli.py"
    )


@pytest.fixture(scope="session")
def run_everbot(everbot_bin: Path, e2e_env: Dict[str, Path]):
    """Factory fixture: run ``bin/everbot <args>`` and return (rc, stdout, stderr)."""

    def _run(args: List[str], timeout: int = 30) -> Tuple[int, str, str]:
        cmd = [str(everbot_bin)] + args
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(e2e_env["project_root"]),
        )
        return proc.returncode, proc.stdout, proc.stderr

    return _run


@pytest.fixture(scope="session")
def run_ops(ops_cli_path: Path, alfred_home: Path):
    """Factory fixture: run ``ops_cli.py --alfred-home <path> <args>`` and return parsed JSON."""

    def _run(args: List[str], timeout: int = 30) -> Dict[str, Any]:
        cmd = [
            sys.executable, str(ops_cli_path),
            "--alfred-home", str(alfred_home),
        ] + args
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            return {
                "ok": False,
                "error": f"non-JSON output (rc={proc.returncode})",
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            }

    return _run
