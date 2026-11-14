"""Ranking helpers for hybrid search: a lexical score that counts distinct query terms weighted
by rarity (so "tháng", "là", "bao nhiêu" matter little and a long table cannot win on
repetition), a bonus for query words that stand next to each other in the chunk, and
score-aware fusion with the vector list.

Plain Python over the FTS candidates; no extra model or API call per query.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Sequence

from .db import fts_fold

_WORD = re.compile(r"\w+", re.UNICODE)
PHRASE_WEIGHT = 0.3  # share of the lexical score given to adjacent query-word pairs


def tokens(text: str) -> list[str]:
    """Lower-case, accent-free word tokens, the same way the FTS index folds them."""
    folded = unicodedata.normalize("NFD", fts_fold(text).lower())
    return _WORD.findall("".join(c for c in folded if not unicodedata.combining(c)))


def idf(document_frequency: int, total: int) -> float:
    """BM25 inverse document frequency; 0 for a word that no chunk contains, because no
    candidate can match it and it would only dilute every score ("gì", "nhỉ" in a question)."""
    if document_frequency <= 0:
        return 0.0
    return math.log((total - document_frequency + 0.5) / (document_frequency + 0.5) + 1.0)


def lexical_score(query: Sequence[str], weights: dict[str, float], text: str) -> float:
    """0..1: rarity-weighted share of distinct query terms found in ``text``, plus a share for
    adjacent query-term pairs that also stand together in ``text``."""
    words = tokens(text)
    present = set(words)
    distinct = list(dict.fromkeys(query))
    total = sum(weights[t] for t in distinct) or 1.0
    coverage = sum(weights[t] for t in distinct if t in present) / total
    pairs = list(zip(query, query[1:], strict=False))
    if not pairs:
        return coverage
    adjacent = set(zip(words, words[1:], strict=False))
    pair_total = sum(weights[a] + weights[b] for a, b in pairs) or 1.0
    phrase = sum(weights[a] + weights[b] for a, b in pairs if (a, b) in adjacent) / pair_total
    return (1 - PHRASE_WEIGHT) * coverage + PHRASE_WEIGHT * phrase
