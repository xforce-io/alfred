"""Tests for web-search search.py (existing + extract pipeline)."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from extract_cache import ExtractCache, MAX_READ_LIMIT
from search import (
    SearchError,
    SearchResult,
    build_backend_order,
    dedupe_results,
    extract_result_pages,
    format_text,
    normalize_ddgs_results,
    normalize_tavily_results,
    search_with_fallback,
    allocate_visible_text,
    resolve_visible_budget,
)


class DummyBackend:
    """Simple stub backend for fallback tests."""

    def __init__(self, name, available=True, results=None, error=None):
        self.name = name
        self.available = available
        self.results = results or []
        self.error = error

    def is_available(self):
        return self.available

    def search(self, args):
        if self.error:
            raise SearchError(self.error)
        return list(self.results)


def make_args(**overrides):
    """Build argparse namespace for tests."""
    values = {
        "query": "example",
        "backend": "auto",
        "type": "text",
        "max_results": 5,
        "region": "wt-wt",
        "timelimit": None,
        "safesearch": "moderate",
        "output": "json",
        "timeout": 15,
        "extract": False,
        "extract_top": 2,
        "fallback": True,
        "verbose": False,
        "visible_chars": None,
        "full_extract": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_normalize_ddgs_text_results():
    raw = [
        {"title": "A", "href": "https://example.com/a", "body": "alpha"},
        {"title": "B", "href": "https://example.com/b", "body": "beta"},
    ]
    results = normalize_ddgs_results(raw, search_type="text")
    assert [item.rank for item in results] == [1, 2]
    assert results[0].backend == "ddgs"
    assert results[0].url == "https://example.com/a"


def test_normalize_tavily_payload():
    payload = {
        "results": [
            {
                "title": "Result A",
                "url": "https://example.com/a",
                "content": "alpha",
                "published_date": "2026-03-06",
            }
        ]
    }
    results = normalize_tavily_results(payload, search_type="news")
    assert len(results) == 1
    assert results[0].search_type == "news"
    assert results[0].published == "2026-03-06"


def test_dedupe_results_by_url():
    results = [
        SearchResult("A", "https://example.com/a", "", None, None, "ddgs", "text", 1),
        SearchResult("B", "https://example.com/a/", "", None, None, "ddgs", "text", 2),
        SearchResult("C", "https://example.com/c", "", None, None, "ddgs", "text", 3),
    ]
    deduped = dedupe_results(results)
    assert len(deduped) == 2
    assert [item.rank for item in deduped] == [1, 2]


def test_search_with_fallback_uses_second_backend(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    backend_map = {
        "tavily": DummyBackend("tavily", available=False),
        "ddgs": DummyBackend(
            "ddgs",
            results=[SearchResult("A", "https://example.com/a", "", None, None, "ddgs", "text", 1)],
        ),
    }
    response = search_with_fallback(make_args(), backend_map)
    assert response.ok is True
    assert response.backend == "ddgs"
    assert response.attempted_backends == ["ddgs"]
    assert response.errors == []
    assert response.materials_hint == "no_extract"
    assert response.schema_version == 2


def test_search_without_fallback_stops_on_first_backend(monkeypatch):
    backend_map = {
        "tavily": DummyBackend("tavily", error="boom"),
        "ddgs": DummyBackend(
            "ddgs",
            results=[SearchResult("A", "https://example.com/a", "", None, None, "ddgs", "text", 1)],
        ),
    }
    response = search_with_fallback(make_args(backend="tavily", fallback=False), backend_map)
    assert response.ok is False
    assert response.attempted_backends == ["tavily"]


def test_auto_backend_order(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "y")
    assert build_backend_order("auto") == ["tavily", "ddgs"]


def test_explicit_backend_order():
    assert build_backend_order("ddgs") == ["ddgs"]


def test_extract_pipeline_stores_and_budgets(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ALFRED_WEB_EXTRACT_CACHE_DIR", str(tmp_path / "c"))
    long_body = "WORD " * 10000  # >40k after normalize? "WORD " * 10000 = 50000
    html = f"<html><body><p>{long_body}</p></body></html>"

    results = [
        SearchResult(
            "Long Article",
            "https://example.com/long",
            "snippet " * 50,
            None,
            None,
            "ddgs",
            "text",
            1,
        )
    ]
    cache = ExtractCache(root=tmp_path / "c", max_bytes=50 * 1024 * 1024)
    extract_result_pages(
        results,
        top_n=1,
        timeout=5,
        cache=cache,
        html_fetcher=lambda url: html,
    )
    assert results[0].extract is not None
    assert results[0].extract.content_id.startswith("sha256:")
    assert results[0].extract.chars_full > 40_000

    stats = allocate_visible_text(results, budget=4000)
    assert stats["visible_text_chars"] <= 4000
    assert results[0].extracted_text == results[0].extract.preview
    assert len(results[0].extract.preview) < results[0].extract.chars_full
    assert results[0].extract.preview_truncated is True

    # Round-trip full text via cache
    cid = results[0].extract.content_id
    full = cache.full_text(cid)
    assert len(full) == results[0].extract.chars_full
    pages = []
    off = 0
    while off < len(full):
        page = cache.read_range(cid, off, MAX_READ_LIMIT)
        pages.append(page)
        if not page:
            break
        off += len(page)
    assert "".join(pages) == full


def test_search_with_extract_e2e_json(tmp_path: Path, monkeypatch):
    """E1: fixture page >40k; visible_text_chars <= 4000; content_id present."""
    monkeypatch.setenv("ALFRED_WEB_EXTRACT_CACHE_DIR", str(tmp_path / "c"))
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    long_body = ("Deep analysis paragraph. " * 2000)
    assert len(long_body) > 40_000
    html = f"<html><head><script>x</script></head><body>{long_body}</body></html>"

    result = SearchResult(
        "Deep Doc",
        "https://example.com/deep",
        "short snip",
        None,
        None,
        "ddgs",
        "text",
        1,
    )

    def fetcher(url: str) -> str:
        return html

    class ExtractBackend:
        name = "ddgs"

        def is_available(self):
            return True

        def search(self, args):
            return [result]

    import search as search_mod

    orig = search_mod.extract_result_pages

    def fake_extract(results, top_n, timeout, cache=None, html_fetcher=None):
        return orig(
            results,
            top_n,
            timeout,
            cache=cache or ExtractCache(root=tmp_path / "c", max_bytes=50 * 1024 * 1024),
            html_fetcher=fetcher,
        )

    monkeypatch.setattr(search_mod, "extract_result_pages", fake_extract)

    response = search_with_fallback(
        make_args(extract=True, extract_top=1, visible_chars=4000, backend="ddgs"),
        {"ddgs": ExtractBackend()},
        cache=ExtractCache(root=tmp_path / "c", max_bytes=50 * 1024 * 1024),
    )
    assert response.ok
    d = response.to_dict()
    assert d["schema_version"] == 2
    assert d["materials_hint"] == "extract_available"
    assert d["stats"]["visible_text_chars"] <= 4000
    assert d["stats"]["full_chars_total"] > 40_000
    ex = d["results"][0]["extract"]
    assert ex["content_id"]
    assert ex["chars_full"] > 40_000
    assert d["results"][0]["extracted_text"] == ex["preview"]
    # stdout material card is not near-full-text
    text_out = format_text(response)
    assert len(text_out) < 20_000
    assert "content_id:" in text_out
    assert "Read:" in text_out
    assert str(tmp_path) not in text_out


def test_e2_e3_paged_restore_over_30k(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ALFRED_WEB_EXTRACT_CACHE_DIR", str(tmp_path / "c"))
    body = "Z" * 35_000
    cache = ExtractCache(root=tmp_path / "c", max_bytes=50 * 1024 * 1024)
    cid = cache.store(body)
    chunks = []
    offset = 0
    while offset < 35_000:
        page = cache.read_range(cid, offset, MAX_READ_LIMIT)
        assert len(page) <= MAX_READ_LIMIT
        chunks.append(page)
        offset += len(page)
        if not page:
            break
    joined = "".join(chunks)
    assert joined == body
    assert hashlib.sha256(joined.encode()).hexdigest() == cid.split(":")[1]


def test_e4_no_extract_hint(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    backend_map = {
        "ddgs": DummyBackend(
            "ddgs",
            results=[
                SearchResult("A", "https://example.com/a", "snip", None, None, "ddgs", "text", 1)
            ],
        ),
    }
    response = search_with_fallback(make_args(extract=False, backend="ddgs"), backend_map)
    assert response.materials_hint == "no_extract"
    assert response.results[0].extract is None
    d = response.to_dict()
    assert d["materials_hint"] == "no_extract"


def test_partial_extract_failure(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ALFRED_WEB_EXTRACT_CACHE_DIR", str(tmp_path / "c"))
    cache = ExtractCache(root=tmp_path / "c", max_bytes=10 * 1024 * 1024)
    r1 = SearchResult("A", "https://ok.example", "s1", None, None, "ddgs", "text", 1)
    r2 = SearchResult("B", "https://bad.example", "s2", None, None, "ddgs", "text", 2)

    def fetcher(url: str) -> str:
        if "bad" in url:
            raise RuntimeError("network")
        return "<html><body>Good page content here</body></html>"

    extract_result_pages([r1, r2], top_n=2, timeout=5, cache=cache, html_fetcher=fetcher)
    assert r1.extract is not None
    assert r2.extract is None
    assert r2.extract_error == "extract_failed"
    allocate_visible_text([r1, r2], budget=4000)
    assert r1.extracted_text == r1.extract.preview
