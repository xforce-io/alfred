"""Tests for gray-rhino per-source fetch reporting and degradation disclosure.

Covers:
- NewsFetcher.fetch_report captures per-source success / failure / degraded(empty).
- rhino_report's main report path carries `sources` (dict) and the text report
  header discloses degraded fetches, with backward-compat when `sources` absent.

Offline: _fetch_rss is monkeypatched; the report path stubs NewsFetcher so no
network is touched.
"""
import importlib.util
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "gray-rhino" / "scripts"

# scripts dir on sys.path so rhino_report's sibling imports (from news_fetcher
# import ...) resolve during exec_module.
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # register so sibling `from <name> import ...` works
    spec.loader.exec_module(mod)
    return mod


nf = _load("news_fetcher")
rr = _load("rhino_report")


# --- NewsFetcher.fetch_report (regression for the P2 data layer) -------------

def _fetcher(stub):
    f = nf.NewsFetcher(sources={"GoodA": "u1", "BadB": "u2", "EmptyC": "u3"}, timeout=1)
    f._fetch_rss = stub  # none of these names are JSON_API_SOURCES, so all go via _fetch_rss
    return f


def _mixed(name, url):
    if name == "GoodA":
        return [nf.NewsItem(title="alpha event", summary="s", source=name, url="u")]
    if name == "BadB":
        raise RuntimeError("HTTP 403 Forbidden")
    return []  # EmptyC: source alive but returned nothing (degraded, not failed)


def test_fetch_report_counts_failures_and_degraded():
    f = _fetcher(_mixed)
    f.fetch_all(max_age_hours=0)
    rep = f.fetch_report
    assert rep["attempted"] == 3
    assert rep["succeeded"] == 2          # GoodA + EmptyC (succeeded = no error)
    assert rep["empty"] == 1              # EmptyC: alive but degraded
    assert rep["contributing"] == 1       # only GoodA actually carried data
    assert rep["failed"] == 1
    fd = rep["failed_detail"]
    assert len(fd) == 1 and fd[0]["source"] == "BadB" and "403" in fd[0]["error"]
    per = {p["source"]: p for p in rep["per_source"]}
    assert per["GoodA"]["status"] == "ok" and per["GoodA"]["items"] == 1
    assert per["EmptyC"]["status"] == "empty" and per["EmptyC"]["items"] == 0
    assert per["BadB"]["status"] == "failed" and "403" in per["BadB"]["error"]


def test_fetch_report_all_fail():
    def all_fail(name, url):
        raise ConnectionError("network down")
    f = _fetcher(all_fail)
    f.fetch_all(max_age_hours=0)
    rep = f.fetch_report
    assert rep["attempted"] == 3 and rep["succeeded"] == 0 and rep["failed"] == 3
    assert len(rep["failed_detail"]) == 3


def test_fetch_report_all_succeed():
    def all_ok(name, url):
        return [nf.NewsItem(title=name + " headline", summary="", source=name, url="u")]
    f = _fetcher(all_ok)
    f.fetch_all(max_age_hours=0)
    rep = f.fetch_report
    assert rep["failed"] == 0 and rep["succeeded"] == 3 and rep["failed_detail"] == []
    assert rep["empty"] == 0 and rep["contributing"] == 3


# --- format_text_report degradation disclosure (link B, new) -----------------

def _report(sources=None, signals=None):
    r = {
        "ok": True,
        "generated_at": "2026-06-24T00:00:00",
        "news_count": 100,
        "total_clusters": 50,
        "history_days": 6,
        "signals": signals or [],
    }
    if sources is not None:
        r["sources"] = sources
    return r


def test_text_discloses_failed_sources():
    sources = {
        "attempted": 3, "succeeded": 2, "failed": 1,
        "failed_detail": [{"source": "OilPrice", "error": "TimeoutError: read timed out"}],
        "per_source": [],
    }
    text = rr.format_text_report(_report(sources=sources))
    assert "OilPrice" in text          # the failed source is named
    assert "失败" in text              # disclosed as a failure
    assert "2/3" in text               # succeeded/attempted shown


def test_text_shows_all_sources_ok():
    sources = {
        "attempted": 20, "succeeded": 20, "failed": 0,
        "failed_detail": [], "per_source": [],
    }
    text = rr.format_text_report(_report(sources=sources))
    assert "20/20" in text             # positive confirmation, even with no failures
    assert "失败" not in text


def test_text_error_branch_still_discloses_sources():
    # P1: when ok=False (all sources failed -> no items), the text report must
    # STILL name the failed sources, not collapse to a generic error line.
    sources = {
        "attempted": 2, "succeeded": 0, "failed": 2, "empty": 0, "contributing": 0,
        "failed_detail": [{"source": "BBC", "error": "ConnectionError: down"},
                          {"source": "OilPrice", "error": "TimeoutError: read timed out"}],
        "per_source": [],
    }
    report = {"ok": False, "error": "No news items fetched. Check network connectivity.",
              "sources": sources}
    text = rr.format_text_report(report)
    assert "Error:" in text            # the generic error is still surfaced
    assert "BBC" in text and "OilPrice" in text   # but failed sources are named
    assert "0/2" in text and "失败" in text


def test_text_error_branch_no_sources_backward_compat():
    # ok=False without a sources field must not crash and stays generic.
    text = rr.format_text_report({"ok": False, "error": "boom"})
    assert text == "Error: boom"


def test_text_empty_source_excluded_from_signal_count():
    # P2: 1 contributing + 1 empty + 1 failed. The "signal rests on N sources"
    # claim must say 1 (contributing), not 2 (succeeded), and disclose the empty.
    sources = {
        "attempted": 3, "succeeded": 2, "failed": 1, "empty": 1, "contributing": 1,
        "failed_detail": [{"source": "OilPrice", "error": "TimeoutError: x"}],
        "per_source": [],
    }
    text = rr.format_text_report(_report(sources=sources))
    assert "信号基于 1 个有效源" in text   # contributing, not succeeded(=2)
    assert "1 个返回空" in text            # the empty source is disclosed
    assert "OilPrice" in text


def test_text_legacy_sources_without_contributing_falls_back():
    # Old report dicts lacking `empty`/`contributing` must still render, basing
    # the count on succeeded (no empty info available).
    sources = {
        "attempted": 3, "succeeded": 2, "failed": 1,
        "failed_detail": [{"source": "OilPrice", "error": "TimeoutError: x"}],
        "per_source": [],
    }
    text = rr.format_text_report(_report(sources=sources))
    assert "信号基于 2 个有效源" in text   # succeeded - empty(=0) = 2
    assert "OilPrice" in text


def test_text_no_sources_backward_compat():
    # Old report dicts without `sources` must still render and not claim failures.
    text = rr.format_text_report(_report(sources=None))
    assert "灰犀牛趋势预警报告" in text
    assert "失败" not in text


# --- main report path carries sources (link B, new) --------------------------

_SRCREP = {
    "attempted": 2, "succeeded": 1, "failed": 1,
    "failed_detail": [{"source": "OilPrice", "error": "TimeoutError: read timed out"}],
    "per_source": [{"source": "BBC", "status": "ok", "items": 2},
                   {"source": "OilPrice", "status": "failed", "items": 0,
                    "error": "TimeoutError: read timed out"}],
}


def _fake_fetcher_cls(items):
    rep = _SRCREP

    class FakeFetcher:
        def __init__(self, *a, **k):
            self.fetch_report = rep

        def fetch_all(self, max_age_hours=48):
            self.fetch_report = rep
            return items

    return FakeFetcher


def test_generate_report_carries_sources(tmp_path, monkeypatch):
    items = [
        nf.NewsItem(title="Oil tankers disrupted in the Strait of Hormuz",
                    summary="s", source="BBC", url="u1"),
        nf.NewsItem(title="Gulf shipping faces new disruption risk",
                    summary="s2", source="CNBC", url="u2"),
    ]
    monkeypatch.setattr(rr, "NewsFetcher", _fake_fetcher_cls(items))
    result = rr.generate_report(save_snapshot=False, history_dir=str(tmp_path))
    assert result["ok"] is True
    assert "sources" in result
    assert result["sources"]["failed"] == 1
    assert result["sources"]["failed_detail"][0]["source"] == "OilPrice"


def test_generate_report_no_items_still_carries_sources(tmp_path, monkeypatch):
    # All sources failing -> no items -> early return must STILL disclose sources
    # (this is the highest-value disclosure case: signal based on zero sources).
    monkeypatch.setattr(rr, "NewsFetcher", _fake_fetcher_cls([]))
    result = rr.generate_report(save_snapshot=False, history_dir=str(tmp_path))
    assert result["ok"] is False
    assert "sources" in result
    assert result["sources"]["failed"] == 1


def test_fetch_only_json_carries_sources(monkeypatch, capsys):
    import json as _json
    items = [nf.NewsItem(title="x", summary="", source="BBC", url="u")]
    monkeypatch.setattr(rr, "NewsFetcher", _fake_fetcher_cls(items))
    # NewsFetcher is stubbed, so the real feedparser/requests deps are never
    # touched; skip the import guard that would sys.exit when they're absent
    # (feedparser is a skill-local dep, not installed in CI).
    monkeypatch.setattr(rr, "_check_dependencies", lambda: None)
    monkeypatch.setattr(sys, "argv", ["rhino_report.py", "--fetch-only", "--format", "json"])
    rr.main()
    data = _json.loads(capsys.readouterr().out)
    assert "sources" in data
    assert data["sources"]["failed"] == 1
