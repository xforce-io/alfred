"""#47: Best-effort Milkie trace report capture chokepoint.

Shell out to ``milkie trace report --data-dir <D> <runId>`` (milkie#144)
and write the generated HTML to ``traces_dir/<runId>.html``.

This module only consumes Milkie's CLI output. It does not parse
``<D>/runs/*.jsonl`` or depend on Milkie event internals. The ``data_dir`` value
must be the same directory passed to ``serve --data-dir``.

Trace capture runs on failure paths, so it must never block or mask the
original error. Any CLI, timeout, or filesystem failure returns ``None``.
"""
from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional, Sequence

logger = logging.getLogger(__name__)

DEFAULT_TRACE_TIMEOUT_SECONDS = 5.0


def _default_runner(cmd: Sequence[str], timeout: float) -> subprocess.CompletedProcess:
    """跑 milkie CLI,stdout 重定向到临时文件再读回。

    milkie/Node 经**管道**输出大内容时,``process.exit`` 可能在 stdout 未 drain 完就退出
    → 截断(实测 execution JSON 约 45KB 处断;``trace report`` 的 HTML 可达 ~558KB,必断)。
    用 ``capture_output=True``(pipe)会拿到残缺 HTML。重定向到文件(同步 fd 写)绕开该
    bug。stderr 量小,仍走 pipe。详见 #55。
    """
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as out:
        proc = subprocess.run(
            list(cmd), stdout=out, stderr=subprocess.PIPE, text=True, timeout=timeout
        )
        out.seek(0)
        stdout = out.read()
    return subprocess.CompletedProcess(list(cmd), proc.returncode, stdout, proc.stderr)


def capture_trace_report(
    run_id: Optional[str],
    *,
    traces_dir: Path,
    data_dir: str,
    milkie_cmd: Sequence[str] = ("milkie",),
    timeout_seconds: float = DEFAULT_TRACE_TIMEOUT_SECONDS,
    runner: Callable[[Sequence[str], float], subprocess.CompletedProcess] = _default_runner,
) -> Optional[Path]:
    """Render ``run_id`` into ``traces_dir/<run_id>.html`` via Milkie CLI.

    ``data_dir`` must be the agent sidecar data directory used by
    ``serve --data-dir``. Returns the written path, or ``None`` when best-effort
    capture fails.
    """
    if not run_id:
        return None
    cmd = [*milkie_cmd, "trace", "report", "--data-dir", str(data_dir), run_id]
    try:
        proc = runner(cmd, timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        logger.debug("milkie trace report timed out(run=%s):%s", run_id, exc)
        return None
    except Exception as exc:
        logger.debug("milkie trace report failed(run=%s):%s", run_id, exc)
        return None
    if proc.returncode != 0 or not proc.stdout:
        logger.debug(
            "milkie trace report exited unsuccessfully(run=%s, rc=%s):%s",
            run_id, proc.returncode, getattr(proc, "stderr", ""),
        )
        return None
    try:
        out_dir = Path(traces_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{run_id}.html"
        out.write_text(proc.stdout, encoding="utf-8")
        return out
    except OSError as exc:
        logger.debug("failed to write trace report(run=%s):%s", run_id, exc)
        return None
