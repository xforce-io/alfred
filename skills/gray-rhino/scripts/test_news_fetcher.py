"""P2: news_fetcher 源级画像（失败源进结构化输出）的测试。

覆盖:部分失败、全失败、全成功、退化(源活着但 0 条)。
不联网——monkeypatch `_fetch_rss`。
"""
import importlib.util
import os

_here = os.path.dirname(__file__)
_spec = importlib.util.spec_from_file_location(
    "news_fetcher", os.path.join(_here, "news_fetcher.py"))
nf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nf)


def _fetcher(stub):
    f = nf.NewsFetcher(sources={"GoodA": "u1", "BadB": "u2", "EmptyC": "u3"}, timeout=1)
    f._fetch_rss = stub  # 所有源名都不在 JSON_API_SOURCES，走 _fetch_rss
    return f


def _mixed(name, url):
    if name == "GoodA":
        return [nf.NewsItem(title="alpha event", summary="s", source=name, url="u")]
    if name == "BadB":
        raise RuntimeError("HTTP 403 Forbidden")
    if name == "EmptyC":
        return []  # 源活着但没返回（退化，非失败）
    return []


def test_report_counts_failures_and_degraded():
    f = _fetcher(_mixed)
    f.fetch_all(max_age_hours=0)
    rep = f.fetch_report
    assert rep["attempted"] == 3
    assert rep["succeeded"] == 2          # GoodA + EmptyC（空也算成功）
    assert rep["failed"] == 1
    fd = rep["failed_detail"]
    assert len(fd) == 1 and fd[0]["source"] == "BadB" and "403" in fd[0]["error"]
    per = {p["source"]: p for p in rep["per_source"]}
    assert per["GoodA"]["status"] == "ok" and per["GoodA"]["items"] == 1
    assert per["EmptyC"]["status"] == "ok" and per["EmptyC"]["items"] == 0
    assert per["BadB"]["status"] == "failed" and "403" in per["BadB"]["error"]


def test_all_sources_fail():
    def all_fail(name, url):
        raise ConnectionError("network down")
    f = _fetcher(all_fail)
    f.fetch_all(max_age_hours=0)
    rep = f.fetch_report
    assert rep["attempted"] == 3 and rep["succeeded"] == 0 and rep["failed"] == 3
    assert len(rep["failed_detail"]) == 3


def test_all_succeed():
    def all_ok(name, url):
        return [nf.NewsItem(title=name + " headline", summary="", source=name, url="u")]
    f = _fetcher(all_ok)
    f.fetch_all(max_age_hours=0)
    rep = f.fetch_report
    assert rep["failed"] == 0 and rep["succeeded"] == 3 and rep["failed_detail"] == []


if __name__ == "__main__":
    import sys
    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    sys.exit(1 if failed else 0)
