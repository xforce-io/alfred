"""Tests for web-search search.py."""

from __future__ import annotations

import argparse

from search import (
    SearchError,
    SearchResult,
    build_backend_order,
    dedupe_results,
    normalize_ddgs_results,
    normalize_tavily_results,
    search_with_fallback,
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
        return self.results


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
