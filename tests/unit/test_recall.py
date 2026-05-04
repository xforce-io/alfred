"""Tests for the BM25-lite ranker."""

from src.everbot.core.memory._recall import bm25_rank, _tokenize
from src.everbot.core.memory.models import MemoryEntry


def _entry(eid: str, content: str) -> MemoryEntry:
    return MemoryEntry(
        id=eid,
        content=content,
        category="fact",
        score=0.5,
        created_at="2026-05-01T00:00:00+00:00",
        last_activated="2026-05-01T00:00:00+00:00",
        activation_count=1,
        source_session="s",
    )


class TestTokenize:
    def test_chinese_chars_split_individually(self):
        assert _tokenize("用户决定") == ["用", "户", "决", "定"]

    def test_latin_words_lowercased_grouped(self):
        assert _tokenize("Hello-WORLD foo") == ["hello", "world", "foo"]

    def test_mixed_chinese_and_latin(self):
        toks = _tokenize("切到 deepseek-chat")
        assert toks == ["切", "到", "deepseek", "chat"]

    def test_preserves_multiplicity(self):
        # Unlike merger's set-based tokenize, BM25's must keep duplicates
        toks = _tokenize("foo foo bar foo")
        assert toks.count("foo") == 3
        assert toks.count("bar") == 1


class TestBM25Rank:
    def test_empty_entries(self):
        assert bm25_rank("anything", []) == []

    def test_empty_query(self):
        entries = [_entry("a", "用户喜欢 Python")]
        assert bm25_rank("", entries) == []

    def test_whitespace_only_query(self):
        entries = [_entry("a", "用户喜欢 Python")]
        assert bm25_rank("   ", entries) == []

    def test_no_matches_returns_empty(self):
        entries = [_entry("a", "用户喜欢 Python")]
        result = bm25_rank("rust", entries)
        assert result == []

    def test_single_match_returned(self):
        entries = [
            _entry("a", "用户喜欢 Python"),
            _entry("b", "用户讨厌 JavaScript"),
        ]
        result = bm25_rank("python", entries)
        assert len(result) == 1
        assert result[0][0].id == "a"
        assert result[0][1] > 0

    def test_higher_term_frequency_ranks_higher(self):
        entries = [
            _entry("once", "Python 不错"),
            _entry("thrice", "Python Python Python 都好用"),
        ]
        result = bm25_rank("python", entries)
        assert [e.id for e, _ in result][0] == "thrice"

    def test_rare_term_outweighs_common_term(self):
        # "决定" appears in many docs (low IDF), "deepseek" only one (high IDF)
        entries = [
            _entry("d1", "用户决定继续"),
            _entry("d2", "用户决定提交"),
            _entry("d3", "用户决定回滚"),
            _entry("d4", "用户决定切到 deepseek"),
        ]
        # Query mentions both; the doc with both terms should win,
        # but among the rest, only matching "决定" gives much lower score.
        result = bm25_rank("决定 deepseek", entries)
        assert result[0][0].id == "d4"
        # d4's score must be notably higher than the runner-up.
        if len(result) > 1:
            assert result[0][1] > result[1][1] * 1.5

    def test_chinese_query_matches_chinese_doc(self):
        entries = [
            _entry("a", "切到 deepseek-chat"),
            _entry("b", "用户喜欢简洁代码"),
        ]
        result = bm25_rank("切到", entries)
        assert result[0][0].id == "a"

    def test_min_score_filter(self):
        entries = [_entry("a", "用户喜欢 Python")]
        # IDF of "python" with 1 doc out of 1 → log((1-1+0.5)/(1+0.5)+1) = log(1.333) > 0
        # Use a high min_score to filter it out
        result = bm25_rank("python", entries, min_score=100.0)
        assert result == []

    def test_results_sorted_descending(self):
        entries = [
            _entry("low", "deepseek 一次"),
            _entry("high", "deepseek deepseek deepseek 用户"),
            _entry("mid", "deepseek deepseek 来了"),
        ]
        result = bm25_rank("deepseek", entries)
        scores = [s for _, s in result]
        assert scores == sorted(scores, reverse=True)

    def test_skips_entries_with_empty_content(self):
        entries = [
            _entry("empty", ""),
            _entry("real", "Python rocks"),
        ]
        result = bm25_rank("python", entries)
        assert [e.id for e, _ in result] == ["real"]
