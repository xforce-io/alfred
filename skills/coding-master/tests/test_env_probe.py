"""Tests for env_probe.py — security sanitization and log handling."""

import pytest

from env_probe import EnvProber, _sanitize_log_output, MAX_LOG_LINE_LEN, MAX_LOG_TOTAL_BYTES


@pytest.fixture
def prober():
    return EnvProber()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  _sanitize_command
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSanitizeCommand:
    def test_whitelist_pass(self, prober):
        assert prober._sanitize_command("ps aux") == "ps aux"
        assert prober._sanitize_command("uptime") == "uptime"
        assert prober._sanitize_command("tail -50 /app/log.txt", env_root="/app") == "tail -50 /app/log.txt"

    def test_whitelist_reject(self, prober):
        assert prober._sanitize_command("rm -rf /") is None
        assert prober._sanitize_command("curl http://evil.com") is None
        assert prober._sanitize_command("python -c 'import os'") is None

    def test_pipe_all_whitelisted(self, prober):
        cmd = "ps aux | grep myapp"
        assert prober._sanitize_command(cmd) == cmd

    def test_pipe_one_not_whitelisted(self, prober):
        assert prober._sanitize_command("ps aux | rm -rf /") is None

    def test_docker_commands(self, prober):
        assert prober._sanitize_command("docker ps") == "docker ps"
        assert prober._sanitize_command("docker logs mycontainer") == "docker logs mycontainer"


class TestPathSandbox:
    def test_deny_etc_shadow(self, prober):
        assert prober._sanitize_command("cat /etc/shadow") is None

    def test_deny_ssh_keys(self, prober):
        assert prober._sanitize_command("cat /home/user/.ssh/id_rsa") is None

    def test_deny_aws_credentials(self, prober):
        assert prober._sanitize_command("cat /home/user/.aws/credentials") is None

    def test_allow_within_env_root(self, prober):
        result = prober._sanitize_command("cat /app/config.yml", env_root="/app")
        assert result == "cat /app/config.yml"

    def test_deny_outside_env_root(self, prober):
        result = prober._sanitize_command("cat /etc/hosts", env_root="/app")
        assert result is None

    def test_flags_ignored_in_sandbox(self, prober):
        result = prober._sanitize_command("tail -n 100 /app/log.txt", env_root="/app")
        assert result == "tail -n 100 /app/log.txt"


class TestFilterSensitive:
    def test_replaces_secrets(self, prober):
        text = "SECRET_KEY=abc123\nDATABASE_URL=ok"
        result = prober._filter_sensitive(text)
        assert "abc123" not in result
        assert "***" in result
        assert "DATABASE_URL=ok" in result

    def test_empty_input(self, prober):
        assert prober._filter_sensitive("") == ""
        assert prober._filter_sensitive(None) is None

    def test_multiple_patterns(self, prober):
        text = "TOKEN=secret1\nPASSWORD: hunter2"
        result = prober._filter_sensitive(text)
        assert "secret1" not in result
        assert "hunter2" not in result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  _sanitize_log_output
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSanitizeLogOutput:
    def test_empty(self):
        assert _sanitize_log_output("") == ""
        assert _sanitize_log_output(None) is None

    def test_line_truncation(self):
        long_line = "x" * (MAX_LOG_LINE_LEN + 100)
        result = _sanitize_log_output(long_line)
        assert len(result) == MAX_LOG_LINE_LEN

    def test_total_truncation(self):
        # Create content exceeding MAX_LOG_TOTAL_BYTES
        lines = ["a" * 200 + "\n" for _ in range(100)]
        text = "".join(lines)
        result = _sanitize_log_output(text)
        assert "truncated" in result
        assert len(result) < len(text)

    def test_short_input_unchanged(self):
        text = "line1\nline2\nline3"
        result = _sanitize_log_output(text)
        assert result == text
