"""#47: `infra/milkie_trace.py` —— 带外 trace 留证 chokepoint。

shell `milkie trace report --data-dir <D> <runId>`(milkie#144 的对称契约),把
HTML 写到 traces_dir。best-effort:任何失败返回 None、不抛、不留半截文件,绝不解析
jsonl(只消费 milkie CLI 产物)。注入 fake runner 验证命令构造与退化行为,不依赖
真实 milkie 二进制。
"""
from src.everbot.infra.milkie_trace import capture_trace_report


class _FakeProc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_capture_trace_report_writes_html_and_returns_path(tmp_path):
    calls: dict = {}

    def runner(cmd):
        calls["cmd"] = cmd
        return _FakeProc(0, stdout="<html>trace</html>")

    out = capture_trace_report(
        "run-abc", traces_dir=tmp_path, data_dir="/sidecar/data", runner=runner
    )

    assert out == tmp_path / "run-abc.html"
    assert out.read_text(encoding="utf-8") == "<html>trace</html>"
    # 对称契约 milkie#144:`milkie trace report --data-dir <D> <runId>`,传给 serve 的同一 data-dir
    assert calls["cmd"][-5:] == ["trace", "report", "--data-dir", "/sidecar/data", "run-abc"]


def test_capture_trace_report_none_for_falsy_runid(tmp_path):
    def runner(cmd):
        raise AssertionError("空 runId 不应执行任何命令")

    assert capture_trace_report("", traces_dir=tmp_path, data_dir="/d", runner=runner) is None
    assert capture_trace_report(None, traces_dir=tmp_path, data_dir="/d", runner=runner) is None


def test_capture_trace_report_returns_none_on_command_failure(tmp_path):
    """退化:milkie trace 非零退出 → 返回 None、不抛、不留半截文件
    (不能拖垮调用它的失败分支)。"""
    def runner(cmd):
        return _FakeProc(1, stdout="", stderr="no run found")

    out = capture_trace_report("run-x", traces_dir=tmp_path, data_dir="/d", runner=runner)
    assert out is None
    assert list(tmp_path.iterdir()) == []


def test_capture_trace_report_tolerates_runner_exception(tmp_path):
    """异常:runner 抛异常(milkie 不存在 / OSError)也吞掉返回 None,带外留证不崩。"""
    def runner(cmd):
        raise FileNotFoundError("milkie not found")

    assert capture_trace_report("run-x", traces_dir=tmp_path, data_dir="/d", runner=runner) is None
