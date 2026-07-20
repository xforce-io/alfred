"""Unit tests for visible budget, schema v2, materials_hint (U1–U7)."""

from __future__ import annotations

import argparse
from pathlib import Path

from extract_cache import ExtractCache
from search import (
    ExtractInfo,
    SearchResult,
    allocate_visible_text,
    clamp_short_fields,
    make_read_command,
    resolve_visible_budget,
)


def _result(rank: int, snippet: str, full: str | None = None, content_id: str | None = None):
    r = SearchResult(
        title=f"T{rank}",
        url=f"https://example.com/{rank}",
        snippet=snippet,
        source=None,
        published=None,
        backend="ddgs",
        search_type="text",
        rank=rank,
    )
    if full is not None:
        cid = content_id or f"sha256:{'a' * 64}"
        r._full_text = full
        r.extract = ExtractInfo(
            preview="",
            preview_truncated=True,
            chars_full=len(full),
            content_id=cid,
            read_command=make_read_command(cid),
        )
    return r


def test_u1_budget_rank_priority():
    r1 = _result(1, "S" * 100, full="P" * 5000)
    r2 = _result(2, "Q" * 100, full="R" * 5000)
    stats = allocate_visible_text([r1, r2], budget=400)
    assert stats["visible_text_chars"] <= 400
    # Rank1 gets snippet + some preview first
    assert len(r1.snippet) + len(r1.extract.preview) >= len(r2.snippet) + len(
        r2.extract.preview
    )
    # Budget exhausted: rank2 preview empty or truncated hard
    assert r2.extract.preview_truncated is True or r2.extract.preview == ""
    if stats["visible_text_chars"] == 400:
        # remaining after r1 should leave little for r2
        assert len(r2.extract.preview) < 5000


def test_u2_visible_text_chars_matches_fields():
    r1 = _result(1, "abc", full="defghij")
    r2 = _result(2, "xyz", full="12345")
    stats = allocate_visible_text([r1, r2], budget=1000)
    actual = (
        len(r1.snippet)
        + len(r1.extract.preview)
        + len(r2.snippet)
        + len(r2.extract.preview)
    )
    assert stats["visible_text_chars"] == actual
    assert stats["visible_text_chars"] <= 1000
    assert stats["full_chars_total"] == len(r1._full_text) + len(r2._full_text)


def test_u1_budget_exhausted_empty_preview():
    r1 = _result(1, "S" * 300, full="BODY" * 100)
    r2 = _result(2, "T" * 300, full="MORE" * 100)
    stats = allocate_visible_text([r1, r2], budget=350)
    assert stats["visible_text_chars"] <= 350
    # r2 may have truncated snippet and empty preview
    if len(r1.snippet) + len(r1.extract.preview) >= 350:
        assert r2.extract.preview == ""
        assert r2.extract.preview_truncated is True


def test_u3_u4_schema_via_to_dict(tmp_path: Path):
    from search import SearchResponse

    r = _result(1, "snip", full="full body text here")
    allocate_visible_text([r], budget=4000)
    resp = SearchResponse(
        ok=True,
        query="q",
        search_type="text",
        backend="ddgs",
        attempted_backends=["ddgs"],
        count=1,
        results=[r],
        errors=[],
        materials_hint="extract_available",
        stats={
            "visible_text_chars": len(r.snippet) + len(r.extract.preview),
            "visible_text_budget": 4000,
            "full_chars_total": r.extract.chars_full,
        },
    )
    d = resp.to_dict()
    assert d["schema_version"] == 2
    assert d["materials_hint"] == "extract_available"
    assert d["result_count"] == d["count"] == 1
    assert "stats" in d
    ex = d["results"][0]["extract"]
    assert ex is not None
    assert ex["content_id"].startswith("sha256:")
    assert "read_command" in ex
    assert "$SKILL_DIR" in ex["read_command"]
    # U4 deprecated alias
    assert d["results"][0]["extracted_text"] == ex["preview"]


def test_u6_materials_hint_no_extract():
    from search import SearchResponse

    r = _result(1, "only snip")
    allocate_visible_text([r], budget=4000)
    resp = SearchResponse(
        ok=True,
        query="q",
        search_type="text",
        backend="ddgs",
        attempted_backends=["ddgs"],
        count=1,
        results=[r],
        errors=[],
        materials_hint="no_extract",
        stats={"visible_text_chars": len(r.snippet), "visible_text_budget": 4000, "full_chars_total": 0},
    )
    assert resp.to_dict()["materials_hint"] == "no_extract"
    assert resp.to_dict()["results"][0]["extract"] is None


def test_u7_short_field_truncation():
    r = SearchResult(
        title="T" * 500,
        url="https://example.com/" + "u" * 3000,
        snippet="s",
        source="src" * 100,
        published="pub" * 100,
        backend="ddgs",
        search_type="text",
        rank=1,
    )
    clamp_short_fields([r])
    assert len(r.title) <= 300
    assert len(r.url) <= 2000
    assert len(r.source) <= 200
    assert len(r.published) <= 200


def test_resolve_visible_budget_priority(monkeypatch):
    monkeypatch.delenv("ALFRED_WEB_EXTRACT_VISIBLE_CHARS", raising=False)
    args = argparse.Namespace(visible_chars=None)
    assert resolve_visible_budget(args) == 4000
    monkeypatch.setenv("ALFRED_WEB_EXTRACT_VISIBLE_CHARS", "1234")
    assert resolve_visible_budget(args) == 1234
    args.visible_chars = 99
    assert resolve_visible_budget(args) == 99


def test_preview_truncated_flag_when_cut():
    r = _result(1, "", full="X" * 1000)
    allocate_visible_text([r], budget=100)
    assert r.extract.preview_truncated is True
    assert len(r.extract.preview) == 100
    assert r.extract.chars_full == 1000
