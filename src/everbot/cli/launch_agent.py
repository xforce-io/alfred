"""macOS LaunchAgent helpers for running EverBot as a persistent user service."""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path
from typing import Any, Dict

from ..infra.user_data import get_user_data_manager


LAUNCH_AGENT_LABEL = "com.alfred.everbot"
# Common local proxy ports to probe at launch time (order = priority).
_PROXY_PROBE_PORTS = [6478, 7897, 7890, 1080, 8118, 1087]


def get_launch_agent_path() -> Path:
    """Return the user LaunchAgent plist path."""
    return Path("~/Library/LaunchAgents").expanduser() / f"{LAUNCH_AGENT_LABEL}.plist"


def _quote_shell(path: str) -> str:
    """Quote a string for safe single-token shell embedding."""
    return "'" + path.replace("'", "'\"'\"'") + "'"


def detect_local_proxy_url() -> str | None:
    """Probe common local proxy ports and return the first reachable one."""
    import socket
    for port in _PROXY_PROBE_PORTS:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return f"http://127.0.0.1:{port}"
        except OSError:
            continue
    return None


def build_launch_agent_plist(*, project_root: Path, alfred_home: Path) -> Dict[str, Any]:
    """Build the LaunchAgent plist payload."""
    project_root = project_root.expanduser().resolve()
    alfred_home = alfred_home.expanduser().resolve()
    logs_dir = alfred_home / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    repo_activate = project_root / ".venv" / "bin" / "activate"
    env_secrets = Path("~/.env.secrets").expanduser()

    probe_ports = " ".join(str(p) for p in _PROXY_PROBE_PORTS)

    shell_parts = [
        "set -euo pipefail",
        # Ensure common user-level bin dirs are on PATH so tools like
        # claude, codex, node, brew utilities are reachable from launchd.
        "export PATH=\"$HOME/.local/bin:/opt/homebrew/bin:$PATH\"",
        f"export PYTHONPATH={_quote_shell(str(project_root))}",
        f"export ALFRED_PROJECT_ROOT={_quote_shell(str(project_root))}",
        f"export ALFRED_HOME={_quote_shell(str(alfred_home))}",
        # Runtime proxy detection — probe common local proxy ports on every
        # launch so it works regardless of which proxy tool is running.
        f"for _port in {probe_ports}; do",
        "  if (echo >/dev/tcp/127.0.0.1/$_port) 2>/dev/null; then",
        "    export HTTP_PROXY=\"http://127.0.0.1:${_port}\"",
        "    export HTTPS_PROXY=\"http://127.0.0.1:${_port}\"",
        "    export ALL_PROXY=\"http://127.0.0.1:${_port}\"",
        "    break",
        "  fi",
        "done",
    ]
    if env_secrets.exists():
        shell_parts.extend([
            f"if [[ -f {_quote_shell(str(env_secrets))} ]]; then",
            "  set -a",
            f"  source {_quote_shell(str(env_secrets))}",
            "  set +a",
            "fi",
        ])
    if repo_activate.exists():
        shell_parts.append(f"source {_quote_shell(str(repo_activate))}")
    shell_parts.append("exec python -m src.everbot.cli start")
    shell_command = "\n".join(shell_parts)

    environment = {
        "ALFRED_HOME": str(alfred_home),
        "ALFRED_PROJECT_ROOT": str(project_root),
        "PYTHONPATH": str(project_root),
    }

    return {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": ["/bin/bash", "-lc", shell_command],
        "WorkingDirectory": str(project_root),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(logs_dir / "everbot.out"),
        "StandardErrorPath": str(logs_dir / "everbot.err"),
        "EnvironmentVariables": environment,
        "ProcessType": "Interactive",
    }


def write_launch_agent_plist(*, project_root: Path, alfred_home: Path) -> Path:
    """Write the LaunchAgent plist and return its path."""
    plist_path = get_launch_agent_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_launch_agent_plist(project_root=project_root, alfred_home=alfred_home)
    with plist_path.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=False)
    return plist_path


def _launchctl_domain() -> str:
    """Return the current user launchctl domain."""
    return f"gui/{os.getuid()}"


def _run_launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run launchctl and capture text output."""
    return subprocess.run(
        ["launchctl", *args],
        check=check,
        capture_output=True,
        text=True,
    )


def cmd_service_install(args) -> None:
    """Install and start EverBot via LaunchAgent."""
    user_data = get_user_data_manager()
    user_data.ensure_directories()
    project_root = Path(__file__).resolve().parents[3]
    plist_path = write_launch_agent_plist(project_root=project_root, alfred_home=user_data.alfred_home)

    domain_target = f"{_launchctl_domain()}/{LAUNCH_AGENT_LABEL}"
    _run_launchctl("bootout", domain_target, check=False)
    _run_launchctl("bootstrap", _launchctl_domain(), str(plist_path))
    _run_launchctl("kickstart", "-k", domain_target)

    print(f"LaunchAgent installed: {plist_path}")
    print(f"Label: {LAUNCH_AGENT_LABEL}")


def cmd_service_uninstall(args) -> None:
    """Unload and remove the EverBot LaunchAgent."""
    plist_path = get_launch_agent_path()
    domain_target = f"{_launchctl_domain()}/{LAUNCH_AGENT_LABEL}"
    _run_launchctl("bootout", domain_target, check=False)
    try:
        plist_path.unlink()
    except FileNotFoundError:
        pass
    print(f"LaunchAgent removed: {plist_path}")


def cmd_service_status(args) -> None:
    """Print LaunchAgent installation and runtime status."""
    plist_path = get_launch_agent_path()
    print(f"Plist: {plist_path}")
    print(f"Installed: {'yes' if plist_path.exists() else 'no'}")

    domain_target = f"{_launchctl_domain()}/{LAUNCH_AGENT_LABEL}"
    result = _run_launchctl("print", domain_target, check=False)
    if result.returncode == 0:
        print("launchctl: loaded")
        if result.stdout.strip():
            print(result.stdout.strip())
        return

    print("launchctl: not loaded")
    stderr = (result.stderr or result.stdout).strip()
    if stderr:
        print(stderr)
