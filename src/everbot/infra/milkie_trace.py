"""#47: 带外 trace 留证 chokepoint —— alfred 侧消费 milkie 已落盘 trace 的唯一入口。

shell `milkie trace report --data-dir <D> <runId>`(milkie#144 的对称契约),把它产
出的 HTML 写到 ``traces_dir/<runId>.html``。设计取向:

* **浅耦合**:只调 milkie CLI、消费其产物;**绝不解析** ``<D>/runs/*.jsonl`` 或
  milkie 的事件 schema(那是 milkie 的大脑,不复制)。``data_dir`` 即给 ``serve
  --data-dir`` 的同一目录,alfred 不需知道 ``runs/<runId>.jsonl`` 文件布局。
* **带外 + best-effort**:任何失败(milkie 缺失 / 非零退出 / 写盘失败)返回 None、
  不抛异常、不留半截文件 —— 它服务于失败留证等路径,绝不能反过来拖垮调用方。

runId 由 Provider 从 milkie#140 的终止帧捕获(``MilkieAgentHandle.last_run_id``),
是 milkie 私有标识;本模块只把它当不透明字符串传给 CLI。
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Callable, Optional, Sequence

logger = logging.getLogger(__name__)


def _default_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def capture_trace_report(
    run_id: Optional[str],
    *,
    traces_dir: Path,
    data_dir: str,
    milkie_cmd: Sequence[str] = ("milkie",),
    runner: Callable[[Sequence[str]], subprocess.CompletedProcess] = _default_runner,
) -> Optional[Path]:
    """渲染 ``run_id`` 的 trace HTML 报告到 ``traces_dir/<run_id>.html``。

    ``data_dir`` 须为该 agent 的 milkie sidecar 数据目录(即传给 ``serve
    --data-dir`` 的那个,``<data_dir>/runs/`` 下有落盘的 run)。返回写出的路径,
    或 None(留证失败,调用方应静默继续)。
    """
    if not run_id:
        return None
    cmd = [*milkie_cmd, "trace", "report", "--data-dir", str(data_dir), run_id]
    try:
        proc = runner(cmd)
    except Exception as exc:  # milkie 缺失 / OSError 等 —— 带外留证不该拖垮调用方
        logger.debug("milkie trace report 执行失败(run=%s):%s", run_id, exc)
        return None
    if proc.returncode != 0 or not proc.stdout:
        logger.debug(
            "milkie trace report 非零退出(run=%s, rc=%s):%s",
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
        logger.debug("写 trace 报告失败(run=%s):%s", run_id, exc)
        return None
