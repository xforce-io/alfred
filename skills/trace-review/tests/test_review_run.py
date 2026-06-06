"""trace-review skill 测试 —— 覆盖 runId 选取(显式/缺省/排除 in-flight/无 run)、
配置回退、CLI 失败降级、投影解析与 digest 渲染。"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import review_run as rr  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _write_run(root: Path, agent: str, run_id: str, *, completed: bool, mtime: float) -> Path:
    runs = root / agent / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    f = runs / f"{run_id}.jsonl"
    lines = [json.dumps({"type": "agent.run.started", "payload": {"input": "hi"}})]
    if completed:
        lines.append(json.dumps({"type": "agent.run.completed", "payload": {"lastTextOutput": "bye"}}))
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.utime(f, (mtime, mtime))
    return f


def _proc(stdout="", stderr="", rc=0):
    return subprocess.CompletedProcess(args=["x"], returncode=rc, stdout=stdout, stderr=stderr)


def _runner_ok(payload: dict):
    def _r(cmd, timeout):
        return _proc(stdout=json.dumps(payload))
    return _r


# ---------------------------------------------------------------------------
# resolve_paths / config
# ---------------------------------------------------------------------------
def test_resolve_paths_defaults_when_no_config(tmp_path):
    p = rr.resolve_paths(config_path=tmp_path / "nope.yaml")
    assert p.dist == rr.DEFAULT_DIST
    assert p.data_dir_root == rr.DEFAULT_DATA_DIR_ROOT
    assert p.node_bin == rr.DEFAULT_NODE_BIN


def test_resolve_paths_cli_overrides_win(tmp_path):
    p = rr.resolve_paths(dist="/x/dist.js", data_dir_root="/d", node_bin="mynode")
    assert p.dist == Path("/x/dist.js")
    assert p.data_dir_root == Path("/d")
    assert p.node_bin == "mynode"


def test_resolve_paths_reads_everbot_milkie(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "everbot:\n  milkie:\n    dist_path: /from/cfg.js\n    node_bin: nodejs\n",
        encoding="utf-8",
    )
    p = rr.resolve_paths(config_path=cfg)
    assert p.dist == Path("/from/cfg.js")
    assert p.node_bin == "nodejs"
    # 未在 config 设的回退默认
    assert p.data_dir_root == rr.DEFAULT_DATA_DIR_ROOT


def test_load_cfg_malformed_yaml_falls_back(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("everbot: [this is: not valid: mapping", encoding="utf-8")
    assert rr._load_milkie_cfg(cfg) == {}


# ---------------------------------------------------------------------------
# is_completed
# ---------------------------------------------------------------------------
def test_is_completed_true(tmp_path):
    f = _write_run(tmp_path, "a", "r1", completed=True, mtime=1000)
    assert rr.is_completed(f) is True


def test_is_completed_false_when_no_marker(tmp_path):
    f = _write_run(tmp_path, "a", "r1", completed=False, mtime=1000)
    assert rr.is_completed(f) is False


def test_is_completed_false_when_missing(tmp_path):
    assert rr.is_completed(tmp_path / "ghost.jsonl") is False


def test_is_completed_scans_only_tail(tmp_path):
    # marker 在末尾,即便前面塞大量噪声也应命中(tail 窗口覆盖结尾)
    runs = tmp_path / "a" / "runs"
    runs.mkdir(parents=True)
    f = runs / "big.jsonl"
    noise = "\n".join(json.dumps({"type": "clock.read", "i": i}) for i in range(5000))
    f.write_text(noise + "\n" + json.dumps({"type": "agent.run.completed"}) + "\n", encoding="utf-8")
    assert rr.is_completed(f, tail_bytes=4096) is True


# ---------------------------------------------------------------------------
# discover_runs / pick_run
# ---------------------------------------------------------------------------
def test_discover_runs_across_agents(tmp_path):
    _write_run(tmp_path, "a", "r1", completed=True, mtime=1000)
    _write_run(tmp_path, "b", "r2", completed=True, mtime=2000)
    runs = rr.discover_runs(tmp_path)
    assert {(a, rid) for a, rid, _, _ in runs} == {("a", "r1"), ("b", "r2")}


def test_discover_runs_agent_narrows(tmp_path):
    _write_run(tmp_path, "a", "r1", completed=True, mtime=1000)
    _write_run(tmp_path, "b", "r2", completed=True, mtime=2000)
    runs = rr.discover_runs(tmp_path, agent="a")
    assert [(a, rid) for a, rid, _, _ in runs] == [("a", "r1")]


def test_discover_runs_missing_root(tmp_path):
    assert rr.discover_runs(tmp_path / "nope") == []


def test_pick_run_latest_completed(tmp_path):
    _write_run(tmp_path, "a", "old", completed=True, mtime=1000)
    _write_run(tmp_path, "a", "new", completed=True, mtime=3000)
    runs = rr.discover_runs(tmp_path)
    picked = rr.pick_run(runs)
    assert picked is not None and picked[1] == "new"


def test_pick_run_excludes_inflight(tmp_path):
    # 最新文件是 in-flight(无 completed 标记) → 应跳过取上一轮 completed
    _write_run(tmp_path, "a", "done", completed=True, mtime=2000)
    _write_run(tmp_path, "a", "inflight", completed=False, mtime=9000)
    runs = rr.discover_runs(tmp_path)
    picked = rr.pick_run(runs)
    assert picked is not None and picked[1] == "done"


def test_pick_run_explicit_id(tmp_path):
    _write_run(tmp_path, "a", "r1", completed=True, mtime=1000)
    _write_run(tmp_path, "a", "r2", completed=False, mtime=2000)
    runs = rr.discover_runs(tmp_path)
    # 显式指定即便未完成也返回
    picked = rr.pick_run(runs, run_id="r2")
    assert picked is not None and picked[1] == "r2"


def test_pick_run_explicit_id_not_found(tmp_path):
    _write_run(tmp_path, "a", "r1", completed=True, mtime=1000)
    runs = rr.discover_runs(tmp_path)
    assert rr.pick_run(runs, run_id="ghost") is None


def test_pick_run_none_when_all_inflight(tmp_path):
    _write_run(tmp_path, "a", "r1", completed=False, mtime=1000)
    runs = rr.discover_runs(tmp_path)
    assert rr.pick_run(runs) is None


# ---------------------------------------------------------------------------
# fetch_execution(CLI 消费 + 降级)
# ---------------------------------------------------------------------------
def test_fetch_execution_ok():
    paths = rr.MilkiePaths(dist=Path("/d.js"), data_dir_root=Path("/d"), node_bin="node")
    proj = rr.fetch_execution(paths, Path("/d/a"), "r1", runner=_runner_ok({"steps": []}))
    assert proj == {"steps": []}


def test_fetch_execution_nonzero_degrades():
    paths = rr.MilkiePaths(dist=Path("/d.js"), data_dir_root=Path("/d"), node_bin="node")
    with pytest.raises(rr.TraceReviewError):
        rr.fetch_execution(paths, Path("/d/a"), "r1", runner=lambda c, t: _proc(stderr="boom", rc=1))


def test_fetch_execution_bad_json_degrades():
    paths = rr.MilkiePaths(dist=Path("/d.js"), data_dir_root=Path("/d"), node_bin="node")
    with pytest.raises(rr.TraceReviewError):
        rr.fetch_execution(paths, Path("/d/a"), "r1", runner=lambda c, t: _proc(stdout="not json"))


def test_fetch_execution_timeout_degrades():
    paths = rr.MilkiePaths(dist=Path("/d.js"), data_dir_root=Path("/d"), node_bin="node")

    def _timeout(cmd, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout)

    with pytest.raises(rr.TraceReviewError):
        rr.fetch_execution(paths, Path("/d/a"), "r1", runner=_timeout)


def test_fetch_execution_node_missing_degrades():
    paths = rr.MilkiePaths(dist=Path("/d.js"), data_dir_root=Path("/d"), node_bin="node")

    def _nofile(cmd, timeout):
        raise FileNotFoundError("node")

    with pytest.raises(rr.TraceReviewError):
        rr.fetch_execution(paths, Path("/d/a"), "r1", runner=_nofile)


# ---------------------------------------------------------------------------
# extract_io
# ---------------------------------------------------------------------------
def test_extract_io_list_and_string_content():
    proj = {
        "steps": [
            {
                "kind": "llm",
                "prompt": {"messages": [{"role": "user", "content": "问题A"}]},
                "response": {"content": [{"type": "text", "text": "答案A"}]},
            }
        ]
    }
    q, a = rr.extract_io(proj)
    assert q == "问题A" and a == "答案A"


def test_extract_io_picks_last_user_and_last_response():
    proj = {
        "steps": [
            {
                "kind": "llm",
                "prompt": {"messages": [
                    {"role": "user", "content": "旧"},
                    {"role": "assistant", "content": "x"},
                    {"role": "user", "content": "最新问题"},
                ]},
                "response": {"content": [{"type": "text", "text": "中间答"}]},
            },
            {"kind": "tool", "tool": {"name": "run_command"}},
            {"kind": "llm", "prompt": {"messages": []}, "response": {"content": [{"type": "text", "text": "最终答"}]}},
        ]
    }
    q, a = rr.extract_io(proj)
    assert q == "最新问题" and a == "最终答"


def test_extract_io_missing_gracefully():
    assert rr.extract_io({"steps": []}) == ("", "")
    assert rr.extract_io({}) == ("", "")


# ---------------------------------------------------------------------------
# render_digest
# ---------------------------------------------------------------------------
def _projection_with_signals():
    return {
        "steps": [
            {
                "kind": "llm",
                "cacheHealth": {"tier": "cold", "hitRate": 0, "readTokens": 0, "totalInputTokens": 5000},
                "regionGroups": [{"stability": "immutable", "regions": [{}]}],
                "prompt": {"messages": [{"role": "user", "content": "帮我推送"}]},
                "response": {"content": [{"type": "text", "text": "好的"}]},
            },
            {
                "kind": "tool",
                "tool": {
                    "name": "run_command",
                    "input": {"command": "python main.py"},
                    "output": {"stdout": "", "stderr": "No such file", "exitCode": 2},
                    "status": "ok",
                },
            },
            {
                "kind": "llm",
                "cacheHealth": {"tier": "cold", "hitRate": 0, "readTokens": 0, "totalInputTokens": 5200},
                "prompt": {"messages": []},
                "response": {"content": [{"type": "text", "text": "最终答案"}]},
            },
        ]
    }


def test_render_digest_detailed_contains_core():
    md = rr.render_digest("demo_agent", "run-x", _projection_with_signals())
    assert "demo_agent" in md and "run-x" in md
    assert "帮我推送" in md          # 问题
    assert "run_command" in md        # 工具
    assert "cache=cold" in md         # cacheHealth
    assert "region:" in md            # 详尽模式带 region
    # 可疑信号:第 2 个 LLM 冷启 + 工具 exitCode!=0/stderr
    assert "缓存冷启" in md
    assert "exitCode=2" in md
    assert "stderr" in md


def test_render_digest_brief_omits_region_and_truncates():
    proj = _projection_with_signals()
    proj["steps"][2]["response"]["content"][0]["text"] = "X" * 2000
    md = rr.render_digest("a", "r", proj, brief=True)
    assert "region:" not in md
    assert "截断" in md  # 答案被截断
    assert "模式: 精简" in md


def test_render_digest_empty_signals():
    proj = {
        "steps": [
            {
                "kind": "llm",
                "cacheHealth": {"tier": "hot", "hitRate": 0.9, "readTokens": 100, "totalInputTokens": 110},
                "prompt": {"messages": [{"role": "user", "content": "q"}]},
                "response": {"content": [{"type": "text", "text": "a"}]},
            }
        ]
    }
    md = rr.render_digest("a", "r", proj)
    assert "无明显异常" in md


def test_render_digest_with_report_path():
    md = rr.render_digest("a", "r", {"steps": []}, report_path=Path("/x/r.html"))
    assert "/x/r.html" in md


def test_summarize_tool_output_empty_stdout_flagged():
    evidence, bad = rr._summarize_tool_output({"stdout": "", "stderr": "", "exitCode": 0})
    assert "为空" in bad


# ---------------------------------------------------------------------------
# run()  端到端(注入 runner + 真实文件系统)
# ---------------------------------------------------------------------------
def test_run_end_to_end(tmp_path):
    root = tmp_path / "milkie"
    _write_run(root, "demo", "good", completed=True, mtime=2000)
    _write_run(root, "demo", "inflight", completed=False, mtime=9000)
    dist = tmp_path / "dist.js"
    dist.write_text("//", encoding="utf-8")

    proj = {
        "steps": [
            {
                "kind": "llm",
                "cacheHealth": {"tier": "hot", "hitRate": 0.8, "readTokens": 80, "totalInputTokens": 100},
                "prompt": {"messages": [{"role": "user", "content": "复盘问题"}]},
                "response": {"content": [{"type": "text", "text": "回答"}]},
            }
        ]
    }
    code, md = rr.run(
        ["--data-dir-root", str(root), "--dist", str(dist), "--config", str(tmp_path / "x.yaml")],
        runner=_runner_ok(proj),
    )
    assert code == 0
    assert "good" in md          # 选了 completed 的上一轮,排除 inflight
    assert "复盘问题" in md


def test_run_dist_missing(tmp_path):
    code, md = rr.run(
        ["--data-dir-root", str(tmp_path), "--dist", str(tmp_path / "ghost.js"),
         "--config", str(tmp_path / "x.yaml")]
    )
    assert code == 4 and "dist 不存在" in md


def test_run_no_data_dir(tmp_path):
    dist = tmp_path / "dist.js"
    dist.write_text("//", encoding="utf-8")
    code, md = rr.run(
        ["--data-dir-root", str(tmp_path / "nope"), "--dist", str(dist),
         "--config", str(tmp_path / "x.yaml")]
    )
    assert code == 2 and "data 目录不存在" in md


def test_run_no_completed_run(tmp_path):
    root = tmp_path / "milkie"
    _write_run(root, "demo", "inflight", completed=False, mtime=1000)
    dist = tmp_path / "dist.js"
    dist.write_text("//", encoding="utf-8")
    code, md = rr.run(
        ["--data-dir-root", str(root), "--dist", str(dist), "--config", str(tmp_path / "x.yaml")]
    )
    assert code == 2 and "没有**已完成**的 run" in md


def test_run_explicit_run_id_not_found(tmp_path):
    root = tmp_path / "milkie"
    _write_run(root, "demo", "r1", completed=True, mtime=1000)
    dist = tmp_path / "dist.js"
    dist.write_text("//", encoding="utf-8")
    code, md = rr.run(
        ["--data-dir-root", str(root), "--dist", str(dist), "--run-id", "ghost",
         "--config", str(tmp_path / "x.yaml")]
    )
    assert code == 2 and "找不到 runId" in md


def test_run_cli_failure_degrades(tmp_path):
    root = tmp_path / "milkie"
    _write_run(root, "demo", "r1", completed=True, mtime=1000)
    dist = tmp_path / "dist.js"
    dist.write_text("//", encoding="utf-8")
    code, md = rr.run(
        ["--data-dir-root", str(root), "--dist", str(dist), "--config", str(tmp_path / "x.yaml")],
        runner=lambda c, t: _proc(stderr="kaboom", rc=1),
    )
    assert code == 3 and "复盘失败" in md


# ---------------------------------------------------------------------------
# #61: 按内容定位 run(run_matches / pick_run match=)
# ---------------------------------------------------------------------------
def _write_run_content(root: Path, agent: str, run_id: str, *, mtime: float,
                       inp: str = "hi", out: str = "bye") -> Path:
    runs = root / agent / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    f = runs / f"{run_id}.jsonl"
    f.write_text(
        json.dumps({"type": "agent.run.started", "payload": {"input": inp}}) + "\n"
        + json.dumps({"type": "agent.run.completed", "payload": {"lastTextOutput": out}}) + "\n",
        encoding="utf-8",
    )
    os.utime(f, (mtime, mtime))
    return f


def test_run_matches_input_and_output(tmp_path):
    f = _write_run_content(tmp_path, "a", "r1", mtime=1, inp="分析推文", out="报告里提到 $SIVE 强烈看多")
    assert rr.run_matches(f, "SIVE") is True          # 命中 output(大小写不敏感)
    assert rr.run_matches(f, "分析推文") is True       # 命中 input
    assert rr.run_matches(f, "不存在的内容") is False
    assert rr.run_matches(f, "") is True               # 空 match 视为不过滤


def test_run_matches_missing_file(tmp_path):
    assert rr.run_matches(tmp_path / "ghost.jsonl", "x") is False


def test_pick_run_match_selects_matching_over_latest(tmp_path):
    # 更新的 run 不含关键词;较老的 run 含 —— match 应越过"最近"选中含关键词的那个
    _write_run_content(tmp_path, "a", "r_new", mtime=200, inp="闲聊", out="好的")
    _write_run_content(tmp_path, "a", "r_old", mtime=100, inp="分析推文", out="$SIVE 报告")
    runs = rr.discover_runs(tmp_path)
    picked = rr.pick_run(runs, match="SIVE")
    assert picked is not None
    assert picked[1] == "r_old"


def test_pick_run_match_picks_most_recent_among_matches(tmp_path):
    _write_run_content(tmp_path, "a", "r1", mtime=100, inp="x", out="$SIVE 旧报告")
    _write_run_content(tmp_path, "a", "r2", mtime=300, inp="y", out="$SIVE 新报告")
    runs = rr.discover_runs(tmp_path)
    picked = rr.pick_run(runs, match="SIVE")
    assert picked[1] == "r2"   # 多个命中取最近


def test_pick_run_match_none_when_no_match(tmp_path):
    _write_run_content(tmp_path, "a", "r1", mtime=100, inp="x", out="y")
    runs = rr.discover_runs(tmp_path)
    assert rr.pick_run(runs, match="无此内容") is None


def test_pick_run_no_match_keeps_latest_completed(tmp_path):
    # 不传 match → 既有行为不变(最近已完成)
    _write_run_content(tmp_path, "a", "r_old", mtime=100, inp="x", out="y")
    _write_run_content(tmp_path, "a", "r_new", mtime=200, inp="x", out="y")
    runs = rr.discover_runs(tmp_path)
    assert rr.pick_run(runs)[1] == "r_new"


def test_run_match_selects_matching_run_end_to_end(tmp_path):
    """--match:端到端定位到内容命中的那轮(而非最近),并对它跑复盘。"""
    root = tmp_path / "milkie"
    _write_run_content(root, "demo", "r_new", mtime=9000, inp="闲聊", out="好的")
    _write_run_content(root, "demo", "r_old", mtime=1000, inp="分析推文", out="$SIVE 报告")
    dist = tmp_path / "dist.js"
    dist.write_text("//", encoding="utf-8")
    seen: dict = {}

    def _runner(cmd, timeout):
        seen["run_id"] = cmd[-1]
        return _proc(stdout=json.dumps({"steps": []}))

    code, md = rr.run(
        ["--data-dir-root", str(root), "--dist", str(dist),
         "--config", str(tmp_path / "x.yaml"), "--match", "SIVE"],
        runner=_runner,
    )
    assert code == 0
    assert seen["run_id"] == "r_old"


def test_run_match_no_match_reports_clearly(tmp_path):
    root = tmp_path / "milkie"
    _write_run_content(root, "demo", "r1", mtime=1000, inp="x", out="y")
    dist = tmp_path / "dist.js"
    dist.write_text("//", encoding="utf-8")
    code, md = rr.run(
        ["--data-dir-root", str(root), "--dist", str(dist),
         "--config", str(tmp_path / "x.yaml"), "--match", "无此内容"],
    )
    assert code == 2 and "没有内容含" in md


# ---------------------------------------------------------------------------
# #61 part1: SKILL.md 触发描述须覆盖口语化溯源追问(原 bug:『上面内容从哪来的』不激活)
# ---------------------------------------------------------------------------
def _routing_description(skill_md: str, max_chars: int = 150) -> str:
    """复刻 everbot discover_skills 的 description 提取:# 标题后首段正文,截断到 max_chars。
    这是 skill_list **路由**实际看到的文本(触发词必须在此 —— frontmatter/何时用 影响不到路由)。"""
    tm = re.search(r"^#\s+(.+)$", skill_md, re.MULTILINE)
    after = skill_md[tm.end():] if tm else skill_md
    para: list[str] = []
    for line in after.splitlines():
        s = line.strip()
        if not para:
            if not s:
                continue
            if s.startswith("#"):
                break
        elif not s or s.startswith("#"):
            break
        para.append(s)
    desc = " ".join(para).strip()
    return desc if len(desc) <= max_chars else desc[:max_chars]


def test_skill_md_covers_colloquial_provenance_triggers():
    skill_md = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text(encoding="utf-8")
    routed = _routing_description(skill_md)  # 路由实际看到的(截断后)文本
    for phrase in ["上面", "哪来", "怎么来", "复盘", "这些内容"]:
        assert phrase in routed, f"路由 description 须含『{phrase}』,否则 skill_list 选不中(#61)"


def test_skill_md_documents_match_option():
    skill_md = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text(encoding="utf-8")
    assert "--match" in skill_md, "SKILL.md 应文档化 --match(按内容定位 run)"
