"""#124 L2 样板:报告逐条信号保留来源证据(title+source+url),让被 cite 的
报告 object 真带逐条链接 —— 否则 cite 到的只是一坨无 url 的聚合文本。

证据在 rhino_analyzer 聚类时(NewsCluster)就该保留,经 trend_tracker(TrendSignal)
穿到 rhino_report 输出。离线:不触网,直接喂带 url 的 NewsItem。
"""
import importlib.util
import sys
from dataclasses import asdict
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "gray-rhino" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


nf = _load("news_fetcher")
ra = _load("rhino_analyzer")
rr = _load("rhino_report")


def _items():
    return [
        nf.NewsItem(title="长江有色：24日铜价四连跌 下游畏高情绪仍浓",
                    summary="铜价四连跌", source="Sina Finance",
                    url="https://finance.sina/cu1", published="2026-06-24T08:00:00"),
        nf.NewsItem(title="长江有色：24日铝价大跌 整体成交偏淡",
                    summary="铝价大跌", source="Sina Finance",
                    url="https://finance.sina/al2", published="2026-06-24T08:05:00"),
    ]


def _to_dicts(items):
    out = [asdict(i) for i in items]
    for d in out:
        d.pop("_hash", None)
    return out


def test_analyzer_cluster_retains_per_item_evidence():
    items = _to_dicts(_items())
    clusters = ra.RhinoAnalyzer(min_cluster_size=1).analyze(items)
    assert clusters

    all_ev = [e for c in clusters for e in c.get("evidence", [])]
    assert all_ev, "每个 cluster 必须保留贡献条目的 evidence"
    # 每条 evidence 带可追溯的 title+source+url
    for e in all_ev:
        assert e.get("url") and e.get("title") and e.get("source")
    # 原始 url 不丢:聚类后 evidence 的 url 全集 == 输入 url 全集
    assert {e["url"] for e in all_ev} == {"https://finance.sina/cu1", "https://finance.sina/al2"}


def _fake_fetcher_cls(items):
    rep = {"attempted": 1, "succeeded": 1, "failed": 0,
           "failed_detail": [], "per_source": []}

    class FakeFetcher:
        def __init__(self, *a, **k):
            self.fetch_report = rep

        def fetch_all(self, max_age_hours=48):
            return items

    return FakeFetcher


def test_report_json_signals_carry_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(rr, "NewsFetcher", _fake_fetcher_cls(_items()))
    result = rr.generate_report(save_snapshot=False, history_dir=str(tmp_path))
    assert result["ok"] is True

    signals = result["signals"]
    assert signals
    ev_urls = {e["url"] for s in signals for e in s.get("evidence", [])}
    assert ev_urls, "每条信号必须带 evidence(来源条目含 url)"
    assert ev_urls <= {"https://finance.sina/cu1", "https://finance.sina/al2"}
    # 至少一条信号的 evidence 非空且每条带 url
    assert any(s.get("evidence") for s in signals)
    for s in signals:
        for e in s.get("evidence", []):
            assert e.get("url")


def test_text_report_lists_source_link_for_signal(tmp_path, monkeypatch):
    monkeypatch.setattr(rr, "NewsFetcher", _fake_fetcher_cls(_items()))
    result = rr.generate_report(save_snapshot=False, history_dir=str(tmp_path))
    text = rr.format_text_report(result)
    # text 报告里逐条信号应能看到来源链接(粗粒度 cite 的节点据此可下钻)
    assert "https://finance.sina/cu1" in text or "https://finance.sina/al2" in text


def test_text_report_appends_parseable_provenance_block(tmp_path, monkeypatch):
    """#130 T1:text 报告末尾机械追加 PROVENANCE 块,且能被投递侧提取器解析。
    每条信号 top1 {title,url},url 取自真实 evidence。"""
    from src.everbot.core.runtime.provenance_footer import extract_provenance_block

    monkeypatch.setattr(rr, "NewsFetcher", _fake_fetcher_cls(_items()))
    result = rr.generate_report(save_snapshot=False, history_dir=str(tmp_path))
    text = rr.format_text_report(result)

    signals = extract_provenance_block(text)
    assert signals, "text 报告末尾应有可解析的 PROVENANCE 块"
    for s in signals:
        assert s["title"] and s["url"]
        assert s["url"] in {"https://finance.sina/cu1", "https://finance.sina/al2"}
