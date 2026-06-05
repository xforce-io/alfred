"""#47: Best-effort trace capture chokepoint tests."""

import subprocess
import sys

from src.everbot.infra.milkie_trace import _default_runner, capture_trace_report


class _FakeProc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_capture_trace_report_writes_html_and_returns_path(tmp_path):
    calls: dict = {}

    def runner(cmd, timeout):
        calls["cmd"] = cmd
        calls["timeout"] = timeout
        return _FakeProc(0, stdout="<html>trace</html>")

    out = capture_trace_report(
        "run-abc",
        traces_dir=tmp_path,
        data_dir="/sidecar/data",
        timeout_seconds=1.5,
        runner=runner,
    )

    assert out == tmp_path / "run-abc.html"
    assert out.read_text(encoding="utf-8") == "<html>trace</html>"
    # Milkie#144 contract: use the same data-dir passed to serve.
    assert calls["cmd"][-5:] == ["trace", "report", "--data-dir", "/sidecar/data", "run-abc"]
    assert calls["timeout"] == 1.5


def test_capture_trace_report_none_for_falsy_runid(tmp_path):
    def runner(cmd, timeout):
        raise AssertionError("runner should not be called for an empty run id")

    assert capture_trace_report("", traces_dir=tmp_path, data_dir="/d", runner=runner) is None
    assert capture_trace_report(None, traces_dir=tmp_path, data_dir="/d", runner=runner) is None


def test_capture_trace_report_returns_none_on_command_failure(tmp_path):
    """A non-zero CLI exit returns None without writing a partial report."""
    def runner(cmd, timeout):
        return _FakeProc(1, stdout="", stderr="no run found")

    out = capture_trace_report("run-x", traces_dir=tmp_path, data_dir="/d", runner=runner)
    assert out is None
    assert list(tmp_path.iterdir()) == []


def test_capture_trace_report_tolerates_runner_exception(tmp_path):
    """Runner exceptions are swallowed because trace capture is best-effort."""
    def runner(cmd, timeout):
        raise FileNotFoundError("milkie not found")

    assert capture_trace_report("run-x", traces_dir=tmp_path, data_dir="/d", runner=runner) is None


def test_default_runner_captures_large_stdout_without_truncation():
    """#55: 真实 runner 经文件重定向捕获大 stdout,不被管道 drain 截断。

    用真实子进程输出远超管道缓冲(>45KB,实测截断点)的内容,断言完整拿回。
    """
    size = 600_000  # ~600KB,模拟 trace report HTML 量级
    code = f"import sys; sys.stdout.write('A' * {size})"
    proc = _default_runner([sys.executable, "-c", code], 30.0)

    assert proc.returncode == 0
    assert len(proc.stdout) == size
    assert proc.stdout == "A" * size


def test_default_runner_propagates_returncode_and_stderr():
    """#55: 非零退出码与 stderr 仍正确透出(stderr 走 pipe)。"""
    code = "import sys; sys.stderr.write('boom'); sys.exit(3)"
    proc = _default_runner([sys.executable, "-c", code], 30.0)

    assert proc.returncode == 3
    assert "boom" in proc.stderr
    assert proc.stdout == ""


def test_capture_trace_report_returns_none_on_timeout(tmp_path):
    """A hung CLI times out and degrades to None."""
    def runner(cmd, timeout):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    out = capture_trace_report(
        "run-slow",
        traces_dir=tmp_path,
        data_dir="/d",
        timeout_seconds=0.01,
        runner=runner,
    )

    assert out is None
    assert list(tmp_path.iterdir()) == []
