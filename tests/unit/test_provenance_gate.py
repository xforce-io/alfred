"""#127 L1 provenance gate — assess whether a report run is backed by real
tool execution + cite edges. observe-first: pure assessment, no delivery change."""

from src.everbot.core.runtime.provenance_gate import (
    assess_report_backing,
    BackingVerdict,
)


def _ev(etype, **payload):
    return {"type": etype, "payload": payload}


def test_no_tool_no_cite_is_unbacked():
    """纯 LLM 轮、零工具、零 cite —— 正是 F2「不跑就编」的形态。"""
    events = [_ev("llm.requested"), _ev("llm.responded")]
    v = assess_report_backing(events)
    assert isinstance(v, BackingVerdict)
    assert v.tool_calls == 0 and v.cites == 0
    assert v.has_tool_backing is False
    assert v.has_cites is False


def test_data_tools_counted_cite_think_lineage_excluded():
    """只数真数据工具;cite/think/get_lineage 等非取数工具不计入 tool_calls。"""
    events = [
        _ev("tool.requested", toolName="run_command", input={"command": "python rhino_report.py"}),
        _ev("tool.requested", toolName="think", input={}),
        _ev("tool.requested", toolName="cite", input={"claim": "x", "objectId": "o"}),
        _ev("tool.requested", toolName="get_lineage", input={}),
    ]
    v = assess_report_backing(events)
    assert v.tool_calls == 1
    assert v.commands and "rhino_report.py" in v.commands[0]


def test_cites_relations_counted_derives_from_not():
    events = [
        _ev("relation.created", type="cites", fromObjectId="a", toObjectId="b"),
        _ev("relation.created", type="cites", fromObjectId="a", toObjectId="c"),
        _ev("relation.created", type="derives_from", fromObjectId="a", toObjectId="d"),
    ]
    v = assess_report_backing(events)
    assert v.cites == 2


def test_realistic_backed_report_passes():
    """跑了脚本 + 逐条 cite —— 合格报告。"""
    events = [
        _ev("tool.requested", toolName="skill_request", input={"name": "gray-rhino"}),
        _ev("tool.requested", toolName="run_command", input={"command": "python scripts/rhino_report.py"}),
        _ev("relation.created", type="cites", fromObjectId="c1", toObjectId="o"),
        _ev("relation.created", type="cites", fromObjectId="c2", toObjectId="o"),
    ]
    v = assess_report_backing(events)
    assert v.has_tool_backing and v.has_cites
    assert v.tool_calls == 2 and v.cites == 2


def test_command_ran_matches_declared_must_run():
    """must_run 匹配:声明的必经脚本到底跑没跑(后续强制判据的基础)。"""
    events = [
        _ev("tool.requested", toolName="run_command", input={"command": "cd x && python scripts/rhino_report.py --format text"}),
    ]
    v = assess_report_backing(events)
    assert v.ran_command_matching("rhino_report.py") is True
    assert v.ran_command_matching("news_fetcher.py") is False
