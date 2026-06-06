#!/usr/bin/env python3
"""trace-review skill —— 对话式自省一次 milkie run(落地 issue #53 / #47 §五)。

milkie 每轮已把完整事件溯源 trace 落盘到 ``<data_dir_root>/<agent>/runs/<runId>.jsonl``。
本脚本**只消费** milkie 现成的诊断产物 —— shell 调 ``node <dist> trace execution/report
<runId>`` 并把投影渲染成 markdown digest(承接 #47 §六:不解析 jsonl、不重算 projection、
不在 Python 重建诊断逻辑)。run 选取需要判断"是否完成",仅 tail 扫一个事件标记,不属诊断逻辑。

agent 经内建 ``run_command`` 调用:

    python skills/trace-review/scripts/review_run.py [--agent NAME] [--run-id ID] [--brief] [--full]

缺省复盘"全局最近一个**已完成** run"(当前 in-flight 那轮尚未 completed,天然被排除 →
正好命中上一轮)。配置(dist / data-dir-root / node-bin)读 ``~/.alfred/config.yaml`` 的
``everbot.milkie``,缺省回退 launcher 默认;CLI flag 最高优先(便于测试)。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# skills/trace-review/scripts/review_run.py → parents[3] = alfred 仓根
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATA_DIR_ROOT = Path("~/.alfred/milkie").expanduser()
DEFAULT_DIST = REPO_ROOT.parent / "milkie" / "dist" / "cli" / "index.js"
DEFAULT_NODE_BIN = "node"
DEFAULT_CONFIG = Path("~/.alfred/config.yaml").expanduser()
TRACES_DIR = Path("~/.alfred/logs/traces").expanduser()

COMPLETION_MARKER = "agent.run.completed"
DEFAULT_CLI_TIMEOUT = 20.0
_TAIL_BYTES = 131072  # 完成标记在末尾;只扫尾部,避免读大文件全文

Runner = Callable[[Sequence[str], float], subprocess.CompletedProcess]


class TraceReviewError(Exception):
    """带 markdown 话术的可控失败 —— main 捕获后打印给 agent 转述,绝不抛栈。"""


def _default_runner(cmd: Sequence[str], timeout: float) -> subprocess.CompletedProcess:
    """跑 CLI,stdout 重定向到临时文件再读回。

    milkie/Node 经**管道**输出大 JSON 时,``process.exit`` 可能在 stdout 未 drain 完就退出
    → 截断(实测约 45KB 处)。重定向到文件(同步 fd 写)绕开该 bug。stderr 量小,仍走 pipe。
    """
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as out:
        proc = subprocess.run(
            list(cmd), stdout=out, stderr=subprocess.PIPE, text=True, timeout=timeout
        )
        out.seek(0)
        stdout = out.read()
    return subprocess.CompletedProcess(list(cmd), proc.returncode, stdout, proc.stderr)


# ---------------------------------------------------------------------------
# 配置解析
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MilkiePaths:
    dist: Path
    data_dir_root: Path
    node_bin: str


def _load_milkie_cfg(config_path: Optional[Path] = None) -> Dict[str, Any]:
    """读 alfred config 的 ``everbot.milkie`` 段。缺省/无 yaml/解析失败 → {}(回退默认)。"""
    path = Path(config_path) if config_path else DEFAULT_CONFIG
    if not path.is_file():
        return {}
    try:
        import yaml  # 可选依赖;缺失则全部走默认
    except Exception:
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    everbot = data.get("everbot") if isinstance(data.get("everbot"), dict) else {}
    milkie = (everbot or {}).get("milkie")
    return milkie if isinstance(milkie, dict) else {}


def resolve_paths(
    *,
    dist: Optional[str] = None,
    data_dir_root: Optional[str] = None,
    node_bin: Optional[str] = None,
    config_path: Optional[Path] = None,
) -> MilkiePaths:
    """优先级:CLI 显式 > config everbot.milkie > launcher 默认。"""
    cfg = _load_milkie_cfg(config_path)
    return MilkiePaths(
        dist=Path(dist or cfg.get("dist_path") or DEFAULT_DIST).expanduser(),
        data_dir_root=Path(data_dir_root or cfg.get("data_dir_root") or DEFAULT_DATA_DIR_ROOT).expanduser(),
        node_bin=str(node_bin or cfg.get("node_bin") or DEFAULT_NODE_BIN),
    )


# ---------------------------------------------------------------------------
# run 发现与选取
# ---------------------------------------------------------------------------
def discover_runs(
    data_dir_root: Path, agent: Optional[str] = None
) -> List[Tuple[str, str, Path, float]]:
    """返回 ``[(agent, run_id, jsonl_path, mtime), ...]``。``agent`` 收窄到单个。"""
    root = Path(data_dir_root)
    if not root.is_dir():
        return []
    if agent:
        agent_names: List[str] = [agent]
    else:
        agent_names = sorted(p.name for p in root.iterdir() if (p / "runs").is_dir())
    out: List[Tuple[str, str, Path, float]] = []
    for name in agent_names:
        runs_dir = root / name / "runs"
        if not runs_dir.is_dir():
            continue
        for f in runs_dir.glob("*.jsonl"):
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            out.append((name, f.stem, f, mtime))
    return out


def is_completed(jsonl_path: Path, tail_bytes: int = _TAIL_BYTES) -> bool:
    """run 是否跑完 —— tail 扫 ``agent.run.completed`` 标记。文件缺失/读失败 → False。"""
    try:
        size = jsonl_path.stat().st_size
        with open(jsonl_path, "rb") as fh:
            if size > tail_bytes:
                fh.seek(size - tail_bytes)
            data = fh.read()
    except OSError:
        return False
    return COMPLETION_MARKER.encode("utf-8") in data


def run_matches(jsonl_path: Path, text: str) -> bool:
    """#61:该 run 的用户输入(``agent.run.started.input/goal``)或最终答案
    (``agent.run.completed.lastTextOutput/output``)是否含 ``text``(子串,大小写不敏感)。
    用于按"那篇报告/那条内容"定位产出它的 run,而非盲取最近一轮。空 text → True(不过滤);
    文件缺失/读失败 → False。"""
    if not text:
        return True
    needle = text.lower()
    try:
        with open(jsonl_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                p = e.get("payload") or {}
                if e.get("type") == "agent.run.started":
                    s = str(p.get("input") or p.get("goal") or "")
                elif e.get("type") == "agent.run.completed":
                    s = str(p.get("lastTextOutput") or p.get("output") or "")
                else:
                    continue
                if needle in s.lower():
                    return True
    except OSError:
        return False
    return False


def pick_run(
    runs: List[Tuple[str, str, Path, float]],
    *,
    run_id: Optional[str] = None,
    match: Optional[str] = None,
) -> Optional[Tuple[str, str, Path]]:
    """显式 run_id 优先;否则按 mtime 降序取第一个**已完成**的 run。

    ``match`` 给定时(#61):在已完成 run 中,按 mtime 降序取第一个**内容命中** ``match``
    的(用户追问"上面那篇报告从哪来的"时定位产出它的 run,而非盲取最近);无命中 → None。"""
    if run_id:
        for name, rid, path, _ in runs:
            if rid == run_id:
                return (name, rid, path)
        return None
    for name, rid, path, _ in sorted(runs, key=lambda r: r[3], reverse=True):
        if not is_completed(path):
            continue
        if match and not run_matches(path, match):
            continue
        return (name, rid, path)
    return None


# ---------------------------------------------------------------------------
# milkie CLI 调用(消费产物,不重写逻辑)
# ---------------------------------------------------------------------------
def _run_cli(
    paths: MilkiePaths,
    subcmd: Sequence[str],
    data_dir: Path,
    run_id: str,
    *,
    runner: Runner,
    timeout: float,
) -> subprocess.CompletedProcess:
    cmd = [paths.node_bin, str(paths.dist), "trace", *subcmd, "--data-dir", str(data_dir), run_id]
    try:
        return runner(cmd, timeout)
    except subprocess.TimeoutExpired as exc:
        raise TraceReviewError(f"milkie trace {' '.join(subcmd)} 超时({timeout}s):{exc}")
    except FileNotFoundError as exc:
        raise TraceReviewError(f"无法执行 node/milkie(检查 node_bin/dist):{exc}")
    except Exception as exc:  # noqa: BLE001 —— best-effort,统一降级
        raise TraceReviewError(f"milkie trace {' '.join(subcmd)} 执行失败:{exc}")


def fetch_execution(
    paths: MilkiePaths,
    data_dir: Path,
    run_id: str,
    *,
    runner: Runner = _default_runner,
    timeout: float = DEFAULT_CLI_TIMEOUT,
) -> Dict[str, Any]:
    """``trace execution`` → 执行投影 dict。非零退出/空输出/非 JSON → TraceReviewError。"""
    proc = _run_cli(paths, ["execution"], data_dir, run_id, runner=runner, timeout=timeout)
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        raise TraceReviewError(
            f"trace execution 失败(rc={proc.returncode}):{(proc.stderr or '').strip()[:300]}"
        )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise TraceReviewError(f"trace execution 输出非 JSON:{exc}")
    if not isinstance(data, dict):
        raise TraceReviewError("trace execution 投影格式异常(非对象)")
    return data


def render_report_html(
    paths: MilkiePaths,
    data_dir: Path,
    run_id: str,
    *,
    runner: Runner = _default_runner,
    timeout: float = DEFAULT_CLI_TIMEOUT,
    traces_dir: Path = TRACES_DIR,
) -> Optional[Path]:
    """``trace report`` → 自包含 HTML 落 ``traces_dir/<run_id>.html``。best-effort:失败 None。"""
    try:
        proc = _run_cli(paths, ["report"], data_dir, run_id, runner=runner, timeout=timeout)
    except TraceReviewError:
        return None
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return None
    try:
        out_dir = Path(traces_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{run_id}.html"
        out.write_text(proc.stdout, encoding="utf-8")
        return out
    except OSError:
        return None


# ---------------------------------------------------------------------------
# 投影 → markdown digest
# ---------------------------------------------------------------------------
def _text_of(content: Any) -> str:
    """把 message/response 的 content 规整成纯文本(string 或 [{type:text,text}])。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            c.get("text", "")
            for c in content
            if isinstance(c, dict) and c.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p)
    return ""


def extract_io(projection: Dict[str, Any]) -> Tuple[str, str]:
    """从执行投影取(用户问题, 最终答案)。问题=首个 LLM step 的末条 user 消息;答案=末个有
    response 的 LLM step。"""
    steps = projection.get("steps") or []
    llms = [s for s in steps if isinstance(s, dict) and s.get("kind") == "llm"]
    question = ""
    if llms:
        messages = (llms[0].get("prompt") or {}).get("messages") or []
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                question = _text_of(msg.get("content"))
                if question:
                    break
    final = ""
    for step in reversed(llms):
        resp = step.get("response")
        if isinstance(resp, dict):
            final = _text_of(resp.get("content"))
            if final:
                break
    return question, final


def _trunc(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"…(截断,共 {len(text)} 字)"


def _compact(value: Any, limit: int) -> str:
    if isinstance(value, str):
        return _trunc(value, limit)
    try:
        return _trunc(json.dumps(value, ensure_ascii=False), limit)
    except (TypeError, ValueError):
        return _trunc(str(value), limit)


def _summarize_tool_output(output: Any) -> Tuple[str, str]:
    """返回 (命中证据摘要, 异常说明)。异常说明非空 → 计入可疑信号。"""
    if isinstance(output, dict) and (
        "stdout" in output or "stderr" in output or "exitCode" in output
    ):
        stdout = (output.get("stdout") or "").strip()
        stderr = (output.get("stderr") or "").strip()
        code = output.get("exitCode")
        evidence = stdout or "(stdout 为空)"
        bad = ""
        if code not in (0, None):
            bad = f"exitCode={code}"
        if stderr:
            bad = (bad + "; " if bad else "") + f"stderr: {_trunc(stderr, 200)}"
        if not stdout and not bad:
            bad = "stdout 为空(可能无召回/无产出)"
        return evidence, bad
    if output is None:
        return "(无输出)", ""
    return _compact(output, 4000), ""


def _summarize_regions(region_groups: Any) -> str:
    if not isinstance(region_groups, list):
        return ""
    parts = []
    for g in region_groups:
        if not isinstance(g, dict):
            continue
        stability = g.get("stability")
        n = len(g.get("regions") or [])
        if stability:
            parts.append(f"{stability}×{n}")
    return ", ".join(parts)


def render_digest(
    agent: str,
    run_id: str,
    projection: Dict[str, Any],
    *,
    brief: bool = False,
    report_path: Optional[Path] = None,
) -> str:
    steps = projection.get("steps") or []
    question, final = extract_io(projection)
    ans_limit = 800 if brief else 100000
    query_limit = 200 if brief else 2000
    evid_limit = 200 if brief else 4000

    lines: List[str] = [
        f"# Run 复盘 · {agent} · `{run_id}`",
        "",
        f"**步骤数**: {len(steps)}" + ("  ·  模式: 精简" if brief else "  ·  模式: 详尽"),
    ]
    if report_path:
        lines.append(f"**HTML 报告**: `{report_path}`")
    lines += [
        "",
        "## 用户问题",
        question.strip() or "_(未捕获)_",
        "",
        "## 最终答案",
        _trunc(final.strip(), ans_limit) or "_(无)_",
        "",
        "## 执行步骤",
    ]

    suspicions: List[str] = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            lines.append(f"- **[{i}]** _(异常步骤)_")
            continue
        kind = step.get("kind")
        if kind == "llm":
            ch = step.get("cacheHealth") or {}
            tier = ch.get("tier")
            hit = ch.get("hitRate") or 0
            lines.append(
                f"- **[{i}] LLM** · cache={tier} 命中率={hit:.0%} "
                f"(read {ch.get('readTokens', 0)}/{ch.get('totalInputTokens', 0)} tok)"
            )
            if not brief:
                regions = _summarize_regions(step.get("regionGroups"))
                if regions:
                    lines.append(f"    - region: {regions}")
            if i > 0 and tier == "cold":
                suspicions.append(f"步骤[{i}] LLM 缓存冷启(命中率 0),可能拖慢/费 token")
        elif kind == "tool":
            tool = step.get("tool") or {}
            name = tool.get("name")
            status = tool.get("status")
            lines.append(f"- **[{i}] 工具 `{name}`** · status={status}")
            lines.append(f"    - query: {_compact(tool.get('input'), query_limit)}")
            evidence, bad = _summarize_tool_output(tool.get("output"))
            lines.append(f"    - 命中: {_trunc(evidence, evid_limit)}")
            if bad:
                suspicions.append(f"步骤[{i}] 工具 `{name}` {bad}")
        else:
            lines.append(f"- **[{i}] {step.get('label') or kind}**")

    lines += ["", "## 可疑信号"]
    lines += [f"- ⚠️ {s}" for s in suspicions] if suspicions else ["- 无明显异常"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="复盘一次 milkie run 的执行过程(求证/自省),输出 markdown。"
    )
    p.add_argument("--agent", default=None, help="收窄到指定 agent(缺省扫所有 agent)")
    p.add_argument("--run-id", default=None, help="显式指定 runId(缺省取最近已完成 run)")
    p.add_argument(
        "--match",
        default=None,
        help="按内容定位 run:取最近一个其用户输入或最终答案含此子串的已完成 run "
        "(追问『上面那篇报告/那条内容从哪来的』时用,避免盲取最近一轮)",
    )
    p.add_argument("--brief", action="store_true", help="精简输出(默认详尽)")
    p.add_argument("--full", action="store_true", help="额外渲染 HTML 报告到 ~/.alfred/logs/traces/")
    p.add_argument("--dist", default=None, help="覆盖 milkie dist 路径")
    p.add_argument("--data-dir-root", default=None, help="覆盖 milkie data-dir 根")
    p.add_argument("--node-bin", default=None, help="覆盖 node 可执行")
    p.add_argument("--config", default=None, help="覆盖 alfred config.yaml 路径")
    return p


def run(argv: Optional[Sequence[str]] = None, *, runner: Runner = _default_runner) -> Tuple[int, str]:
    """返回 (exit_code, markdown)。runner 可注入便于测试。"""
    args = build_parser().parse_args(argv)
    paths = resolve_paths(
        dist=args.dist,
        data_dir_root=args.data_dir_root,
        node_bin=args.node_bin,
        config_path=Path(args.config) if args.config else None,
    )

    if not paths.dist.is_file():
        return 4, (
            f"# 复盘失败\n\nmilkie dist 不存在:`{paths.dist}`\n\n"
            "请确认 milkie 已构建,或经 `everbot.milkie.dist_path` / `--dist` 指定。"
        )

    if not paths.data_dir_root.is_dir():
        return 2, (
            f"# 复盘失败\n\nmilkie data 目录不存在:`{paths.data_dir_root}`\n\n"
            "可能还没有任何 milkie run 落盘。"
        )

    runs = discover_runs(paths.data_dir_root, args.agent)
    picked = pick_run(runs, run_id=args.run_id, match=args.match)
    if picked is None:
        scope = f"agent `{args.agent}`" if args.agent else "所有 agent"
        if args.run_id:
            return 2, f"# 复盘失败\n\n在 {scope} 下找不到 runId `{args.run_id}`。"
        if args.match:
            return 2, f"# 复盘失败\n\n在 {scope} 下没有内容含 `{args.match}` 的已完成 run。"
        return 2, f"# 复盘失败\n\n{scope} 下没有**已完成**的 run 可复盘。"

    agent, run_id, _ = picked
    data_dir = paths.data_dir_root / agent

    try:
        projection = fetch_execution(paths, data_dir, run_id, runner=runner)
    except TraceReviewError as exc:
        return 3, f"# 复盘失败\n\n{exc}"

    report_path: Optional[Path] = None
    if args.full:
        report_path = render_report_html(paths, data_dir, run_id, runner=runner)

    digest = render_digest(
        agent, run_id, projection, brief=args.brief, report_path=report_path
    )
    return 0, digest


def main(argv: Optional[Sequence[str]] = None) -> int:
    code, text = run(argv)
    print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
