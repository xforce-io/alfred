"""BM25-lite ranker for keyword recall over memory entries.

A pure-Python, dependency-free Okapi BM25 implementation. Sized for
year-scale event volumes (≤5000 entries) where the cost of a real
inverted-index library outweighs the gain from grep/scan-time scoring.

Token granularity matches ``merger._tokenize``: CJK characters are
treated as single tokens, Latin runs are lower-cased word tokens.
"""

import math
from collections import Counter
from typing import List, Tuple

from .models import MemoryEntry

# Standard BM25 hyperparameters; identical to rank_bm25 defaults.
_K1 = 1.5
_B = 0.75


def _tokenize(text: str) -> List[str]:
    """Tokenize for BM25 — preserves multiplicity (unlike the merger's set version)."""
    tokens: List[str] = []
    buf: List[str] = []
    for ch in text:
        if "一" <= ch <= "鿿":
            if buf:
                tokens.append("".join(buf).lower())
                buf.clear()
            tokens.append(ch)
        elif ch.isalnum() or ch == "_":
            buf.append(ch)
        else:
            if buf:
                tokens.append("".join(buf).lower())
                buf.clear()
    if buf:
        tokens.append("".join(buf).lower())
    return tokens


def bm25_rank(
    query: str,
    entries: List[MemoryEntry],
    *,
    min_score: float = 0.0,
) -> List[Tuple[MemoryEntry, float]]:
    """Rank entries by BM25 relevance to ``query``.

    Returns a list of ``(entry, score)`` tuples in descending score order.
    Entries with score ≤ ``min_score`` are dropped — by default the cutoff
    is 0 so any matching token surfaces a result.
    """
    if not entries:
        return []
    query_tokens = [t for t in _tokenize(query) if t]
    if not query_tokens:
        return []

    corpus_tokens = [_tokenize(entry.content) for entry in entries]
    doc_lens = [len(toks) for toks in corpus_tokens]
    if not any(doc_lens):
        return []
    avgdl = sum(doc_lens) / len(doc_lens)

    # Document frequency only needs to be computed once per unique query term.
    n = len(entries)
    df = {
        token: sum(1 for toks in corpus_tokens if token in toks)
        for token in set(query_tokens)
    }
    # Okapi BM25 IDF with +1 smoothing (avoids negatives for very common terms).
    idf = {
        t: math.log((n - df[t] + 0.5) / (df[t] + 0.5) + 1)
        for t in df
    }

    scored: List[Tuple[MemoryEntry, float]] = []
    for entry, toks, dl in zip(entries, corpus_tokens, doc_lens):
        if dl == 0:
            continue
        tf = Counter(toks)
        score = 0.0
        for token in query_tokens:
            f = tf[token]
            if f == 0:
                continue
            denom = f + _K1 * (1 - _B + _B * dl / avgdl)
            score += idf[token] * (f * (_K1 + 1)) / denom
        if score > min_score:
            scored.append((entry, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored
