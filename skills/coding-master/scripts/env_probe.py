#!/usr/bin/env python3
"""Environment probing for local and SSH targets."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config_manager import ConfigManager

# ── Security ────────────────────────────────────────────────

COMMAND_WHITELIST = [
    "cat", "tail", "head", "grep",
    "journalctl", "docker logs",
    "ps", "uptime", "df", "free",
    "systemctl status", "docker ps",
    "printenv", "env",
    "ls", "wc",
]

SENSITIVE_PATTERN = re.compile(
    r"(SECRET|PASSWORD|TOKEN|KEY|CREDENTIAL|PRIVATE)(\s*[=:]\s*)\S+",
    re.IGNORECASE,
)

# Path sandbox — deny access to these system-sensitive paths
DENIED_PATHS = [
    "/etc/shadow", "/etc/passwd", "/etc/sudoers",
    "/.ssh/", "/id_rsa", "/id_ed25519",
    "/.gnupg/", "/.aws/credentials",
]

# Log sanitization limits (prompt injection defense)
MAX_LOG_LINE_LEN = 500
MAX_LOG_TOTAL_BYTES = 10 * 1024  # 10KB

CMD_TIMEOUT = 30
OVERALL_TIMEOUT = 120


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  EnvProber
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class EnvProber:
    def __init__(self, config: ConfigManager | None = None):
        self.config = config or ConfigManager()

    def probe(
        self,
        env_name: str,
        extra_commands: list[str] | None = None,
    ) -> dict:
        """Auto-probe an env + optional directed commands."""
        env = self.config.get_env(env_name)
        if env is None:
            return {"ok": False, "error": f"env '{env_name}' not found in config"}

        start = datetime.now(timezone.utc)
        result: dict[str, Any] = {
            "env": {"name": env_name, "type": env["type"], "connect": env["connect"]},
            "probed_at": start.isoformat(),
            "modules": [],
            "custom_probes": {},
        }

        runner = self._make_runner(env)

        # ── Auto-discover modules ───────────────────────────
        try:
            modules = self._auto_discover_modules(env, runner)
            for mod in modules:
                self._probe_module(mod, env, runner)
            result["modules"] = modules
        except _Timeout:
            result["warning"] = "overall timeout reached during module discovery"
            return {"ok": True, "data": result}

        # ── General info ────────────────────────────────────
        result["uptime"] = runner("uptime")
        env_path = env.get("local_path") or env.get("remote_path", "~")
        result["disk_usage"] = runner(f"df -h {shlex.quote(env_path)}")

        # ── Extra commands ──────────────────────────────────
        if extra_commands:
            for cmd in extra_commands:
                safe = self._sanitize_command(cmd, env_root=env_path)
                if safe is None:
                    result["custom_probes"][cmd] = "BLOCKED: not in whitelist or path denied"
                else:
                    result["custom_probes"][cmd] = runner(safe)

        return {"ok": True, "data": result}

    # ── Runner factory ──────────────────────────────────────

    def _make_runner(self, env: dict):
        if env["type"] == "local":
            path = env["local_path"]
            def run_local(cmd: str) -> str:
                return self._exec_local(path, cmd)
            return run_local
        else:
            user_host = env["user_host"]
            remote_path = env.get("remote_path", "~")
            def run_ssh(cmd: str) -> str:
                return self._exec_ssh(user_host, remote_path, cmd)
            return run_ssh

    # ── Module discovery ────────────────────────────────────

    def _auto_discover_modules(self, env: dict, runner) -> list[dict]:
        env_path = env.get("local_path") or env.get("remote_path", "~")

        # docker-compose?
        dc = runner(f"cat {shlex.quote(env_path + '/docker-compose.yml')}")
        if dc and "services:" in dc:
            return self._parse_docker_compose(dc, env_path)

        # Procfile?
        pf = runner(f"cat {shlex.quote(env_path + '/Procfile')}")
        if pf and pf.strip():
            return self._parse_procfile(pf, env_path)

        # Single module
        name = Path(env_path).name or env["name"]
        return [{"name": name, "path": env_path}]

    def _parse_docker_compose(self, content: str, base_path: str) -> list[dict]:
        modules = []
        in_services = False
        indent = 0
        for line in content.splitlines():
            stripped = line.strip()
            if stripped == "services:":
                in_services = True
                indent = len(line) - len(line.lstrip()) + 2
                continue
            if in_services and stripped and not stripped.startswith("#"):
                line_indent = len(line) - len(line.lstrip())
                if line_indent == indent and stripped.endswith(":"):
                    svc_name = stripped[:-1].strip()
                    modules.append({"name": svc_name, "path": base_path})
                elif line_indent < indent and not stripped.startswith("-"):
                    break
        return modules or [{"name": "default", "path": base_path}]

    def _parse_procfile(self, content: str, base_path: str) -> list[dict]:
        modules = []
        for line in content.strip().splitlines():
            if ":" in line:
                name = line.split(":")[0].strip()
                if name:
                    modules.append({"name": name, "path": base_path})
        return modules or [{"name": "default", "path": base_path}]

    # ── Per-module probing ──────────────────────────────────

    def _probe_module(self, module: dict, env: dict, runner) -> None:
        name = module["name"]

        # process status
        ps_out = runner(f"ps aux | grep {shlex.quote(name)}")
        ps_lines = [
            l for l in (ps_out or "").splitlines()
            if name in l and "grep" not in l
        ]
        module["process"] = {
            "running": len(ps_lines) > 0,
            "count": len(ps_lines),
        }

        # log — configured or heuristic
        log_path = env.get("log")
        if not log_path:
            module["log_tail"] = ""
            module["recent_errors"] = []
            return

        # tail recent logs (sanitize for prompt injection defense)
        tail_out = runner(f"tail -50 {shlex.quote(log_path)}")
        module["log_tail"] = _sanitize_log_output(
            self._filter_sensitive(tail_out)
        )

        # grep errors in last 1000 lines
        error_out = runner(
            f"tail -1000 {shlex.quote(log_path)} | grep -i error"
        )
        errors = [
            l.strip()[:MAX_LOG_LINE_LEN]
            for l in (error_out or "").splitlines()
            if l.strip()
        ][:20]
        module["recent_errors"] = [self._filter_sensitive(e) for e in errors]

    # ── Security ────────────────────────────────────────────

    def _sanitize_command(self, cmd: str, env_root: str | None = None) -> str | None:
        """Return the command if it passes whitelist + path sandbox, else None."""
        cmd_stripped = cmd.strip()

        # Check path sandbox — deny access to sensitive system paths
        if any(denied in cmd_stripped for denied in DENIED_PATHS):
            return None

        # If env_root is set, check that file-type commands reference paths under it
        FILE_COMMANDS = {"cat", "tail", "head", "grep"}
        if env_root:
            parts = shlex.split(cmd_stripped)
            if parts and parts[0] in FILE_COMMANDS:
                for arg in parts[1:]:
                    if arg.startswith("-"):
                        continue
                    # Resolve absolute path for sandbox check
                    if arg.startswith("/") and not arg.startswith(env_root):
                        return None

        # Whitelist check
        def _is_whitelisted(segment: str) -> bool:
            segment = segment.strip()
            return any(
                segment == p or segment.startswith(p + " ")
                for p in COMMAND_WHITELIST
            )

        if "|" in cmd_stripped:
            segments = cmd_stripped.split("|")
            if not all(_is_whitelisted(seg) for seg in segments):
                return None
            return cmd_stripped

        if _is_whitelisted(cmd_stripped):
            return cmd_stripped
        return None

    def _filter_sensitive(self, text: str) -> str:
        if not text:
            return text
        return SENSITIVE_PATTERN.sub(r"\1\2***", text)

    # ── Execution ───────────────────────────────────────────

    def _exec_local(self, cwd: str, cmd: str) -> str:
        try:
            r = subprocess.run(
                cmd, shell=True, cwd=cwd,
                capture_output=True, text=True, timeout=CMD_TIMEOUT,
            )
            return self._filter_sensitive(r.stdout)
        except subprocess.TimeoutExpired:
            return "<timeout>"
        except Exception as e:
            return f"<error: {e}>"

    def _exec_ssh(self, user_host: str, remote_path: str, cmd: str) -> str:
        full_cmd = f"cd {shlex.quote(remote_path)} && {cmd}"
        try:
            r = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                 user_host, full_cmd],
                capture_output=True, text=True, timeout=CMD_TIMEOUT,
            )
            return self._filter_sensitive(r.stdout)
        except subprocess.TimeoutExpired:
            return "<timeout>"
        except Exception as e:
            return f"<error: {e}>"


    # ── Verify (Phase 7) ───────────────────────────────────

    def verify(
        self,
        env_name: str,
        baseline_snapshot_path: str,
    ) -> dict:
        """Compare current env state against a baseline snapshot.

        Args:
            env_name: Name of the env to verify.
            baseline_snapshot_path: Path to the Phase 1 env_snapshot.json (baseline).

        Returns:
            Verification report with before/after error comparison.
        """
        # Load baseline
        baseline_path = Path(baseline_snapshot_path)
        if not baseline_path.exists():
            return {"ok": False, "error": "baseline env_snapshot not found",
                    "error_code": "PATH_NOT_FOUND"}

        try:
            baseline = json.loads(baseline_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            return {"ok": False, "error": f"failed to load baseline: {e}"}

        # Probe current state
        current = self.probe(env_name)
        if not current.get("ok"):
            return current

        current_data = current["data"]

        # Extract errors from baseline and current
        baseline_errors = _extract_all_errors(baseline)
        current_errors = _extract_all_errors(current_data)

        # Compute diff
        resolved_errors = [e for e in baseline_errors if e not in current_errors]
        remaining_errors = [e for e in baseline_errors if e in current_errors]
        new_errors = [e for e in current_errors if e not in baseline_errors]

        resolved = len(remaining_errors) == 0 and len(new_errors) == 0

        report = {
            "env": env_name,
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "baseline_errors": baseline_errors,
            "current_errors": current_errors,
            "resolved_errors": resolved_errors,
            "remaining_errors": remaining_errors,
            "new_errors": new_errors,
            "resolved": resolved,
            "modules": current_data.get("modules", []),
            "summary": _build_verify_summary(
                resolved_errors, remaining_errors, new_errors
            ),
        }

        return {"ok": True, "data": report}


def _extract_all_errors(snapshot: dict) -> list[str]:
    """Extract all recent_errors from all modules in a snapshot."""
    errors = []
    for module in snapshot.get("modules", []):
        errors.extend(module.get("recent_errors", []))
    return errors


def _build_verify_summary(
    resolved: list[str], remaining: list[str], new: list[str]
) -> str:
    """Build a human-readable verification summary."""
    parts = []
    if resolved:
        parts.append(f"Resolved {len(resolved)} error(s)")
    if remaining:
        parts.append(f"{len(remaining)} error(s) still present")
    if new:
        parts.append(f"{len(new)} new error(s) appeared")
    if not parts:
        parts.append("No errors in baseline or current state")
    return "; ".join(parts)


class _Timeout(Exception):
    pass


def _sanitize_log_output(text: str) -> str:
    """Truncate per-line and total size to limit prompt injection surface."""
    if not text:
        return text
    lines = text.splitlines()
    sanitized = []
    total = 0
    for line in lines:
        line = line[:MAX_LOG_LINE_LEN]
        if total + len(line) > MAX_LOG_TOTAL_BYTES:
            sanitized.append("... (log output truncated for safety)")
            break
        sanitized.append(line)
        total += len(line) + 1
    return "\n".join(sanitized)


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "\n... (truncated)"
