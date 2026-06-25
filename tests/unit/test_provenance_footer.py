"""#130 T1 — 投递时机械给报告每条信号附 top1 原文链接。

footer 由确定性函数从 milkie run 事件里的 PROVENANCE 块构建,独立于 LLM 散文
(LLM 可能把链接摘掉,机械追加不依赖它)。纯函数,无 IO。"""

import json

from src.everbot.core.runtime.provenance_footer import (
    extract_provenance_block,
    append_provenance_footer,
    extract_signals_from_events,
    render_provenance_footer,
)


def test_extract_block_happy_path():
    """text 里的 <PROVENANCE>{...}</PROVENANCE> 被解析成 signals 列表。"""
    text = (
        "# 报告正文……\n"
        '<PROVENANCE>{"signals":[{"title":"Trump on Hormuz","url":"https://cnbc.com/x"}]}</PROVENANCE>\n'
    )
    signals = extract_provenance_block(text)
    assert signals == [{"title": "Trump on Hormuz", "url": "https://cnbc.com/x"}]


def test_extract_no_block_returns_empty():
    """没有 PROVENANCE 块(老脚本/纯散文)→ [] ,绝不抛。"""
    assert extract_provenance_block("just prose, no block") == []


def test_extract_malformed_json_returns_empty():
    """块在但 JSON 坏 → [] ,绝不抛(投递路径不能因此崩)。"""
    assert extract_provenance_block("<PROVENANCE>{not json}</PROVENANCE>") == []


def test_extract_filters_signals_missing_title_or_url():
    """缺 title 或 url 的条目被丢弃,只留两者俱全的。"""
    text = (
        "<PROVENANCE>" + json.dumps({"signals": [
            {"title": "good", "url": "https://u"},
            {"title": "no url"},
            {"url": "https://only-url"},
            {"title": "", "url": "https://empty-title"},
        ]}) + "</PROVENANCE>"
    )
    assert extract_provenance_block(text) == [{"title": "good", "url": "https://u"}]


def test_extract_empty_signals_returns_empty():
    assert extract_provenance_block('<PROVENANCE>{"signals":[]}</PROVENANCE>') == []


def test_render_empty_signals_returns_empty_string():
    """无 signals → 不追加任何 footer(空串),投递正文不变。"""
    assert render_provenance_footer([]) == ""


def test_render_lists_title_and_url_per_signal():
    """每条信号渲染一行 标题 — url,带可辨识表头。"""
    footer = render_provenance_footer([
        {"title": "Trump on Hormuz", "url": "https://cnbc.com/x"},
        {"title": "Crimea power", "url": "https://bbc.co.uk/y"},
    ])
    assert "Trump on Hormuz" in footer and "https://cnbc.com/x" in footer
    assert "Crimea power" in footer and "https://bbc.co.uk/y" in footer
    # 两条信号 → footer 里有两条链接行
    assert footer.count("https://") == 2
    # footer 与正文有可视分隔(避免和报告糊在一起)
    assert footer.startswith("\n")


def _responded(stdout):
    """milkie run 事件:run_command 的 tool.responded(output.stdout 带工具 stdout)。"""
    return {"type": "tool.responded",
            "payload": {"toolName": "run_command",
                        "output": {"stdout": stdout, "exitCode": 0}}}


def test_signals_from_events_picks_block_in_tool_stdout():
    """从 run 事件里 run_command 的 stdout 取出 PROVENANCE 块。"""
    report = (
        "灰犀牛报告正文……\n"
        '<PROVENANCE>{"signals":[{"title":"Hormuz","url":"https://cnbc.com/x"}]}</PROVENANCE>'
    )
    events = [
        {"type": "tool.requested", "payload": {"toolName": "run_command"}},
        _responded("SKILL.md 内容,无块"),   # 加载技能那步,无 PROVENANCE
        _responded(report),                  # 报告脚本那步,有块
    ]
    assert extract_signals_from_events(events) == [
        {"title": "Hormuz", "url": "https://cnbc.com/x"}]


def test_signals_from_events_no_block_anywhere_returns_empty():
    events = [_responded("纯文本"), {"type": "llm.responded", "payload": {}}]
    assert extract_signals_from_events(events) == []


def test_signals_from_events_tolerates_nonstring_output():
    """output 非 dict / stdout 非 str / 缺 payload —— 不抛。"""
    events = [
        {"type": "tool.responded", "payload": {"output": None}},
        {"type": "tool.responded", "payload": {"output": {"stdout": 123}}},
        {"type": "tool.responded"},
    ]
    assert extract_signals_from_events(events) == []


def test_append_adds_footer_when_evidence_present():
    """有 evidence → result 末尾追加 footer(原文链接进推送)。"""
    report = '<PROVENANCE>{"signals":[{"title":"Hormuz","url":"https://cnbc.com/x"}]}</PROVENANCE>'
    events = [_responded(report)]
    out = append_provenance_footer("# 报告\n内容", events)
    assert out.startswith("# 报告\n内容")
    assert "https://cnbc.com/x" in out and "Hormuz" in out


def test_append_noop_when_no_evidence():
    """无 evidence(老脚本/未带块)→ 原样返回,不动正文。"""
    result = "# 报告\n内容"
    assert append_provenance_footer(result, [_responded("无块")]) == result


def test_append_strips_echoed_block_from_result():
    """LLM 若把脚本的 <PROVENANCE> 机器块抄进散文,投递前剥掉,只留干净 footer。"""
    block = '<PROVENANCE>{"signals":[{"title":"H","url":"https://u"}]}</PROVENANCE>'
    result = "# 报告\n正文\n" + block          # LLM 把机器块抄进了报告
    out = append_provenance_footer(result, [_responded(block)])
    assert "<PROVENANCE>" not in out            # 原始机器块不外泄给用户
    assert "https://u" in out                   # 链接以 footer 形式在场
    assert out.count("https://u") == 1          # 不重复


def test_append_strips_echoed_block_even_when_no_evidence_events():
    """即便事件里取不到 evidence,也要把 result 里抄来的裸块清掉(不外泄标签)。"""
    block = '<PROVENANCE>{"signals":[{"title":"H","url":"https://u"}]}</PROVENANCE>'
    out = append_provenance_footer("# 报告\n" + block, [_responded("无块")])
    assert "<PROVENANCE>" not in out
