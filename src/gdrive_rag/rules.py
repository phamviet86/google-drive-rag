"""Per-source path rules for search: ``demote`` puts all of a file's hits below the next
``DEMOTE_PLACES`` other files in the ranked list (their scores are lowered to just below that
file's best hit), ``exclude`` leaves it out of search results. Rules are applied at query time
only; the file stays indexed and readable by its ID, and changing rules never re-extracts or
re-embeds anything.

A pattern is a glob over the file's ``path`` as search results show it (inside the source
folder), compared without case or Vietnamese accents:

- ``*`` matches within one folder or file name, ``?`` one character, ``**`` any number of
  folders (``**/`` also none);
- a pattern without a leading ``/`` may start at any folder depth, so ``attachments/**`` matches
  ``Kế toán/attachments/a.docx``; a leading ``/`` anchors it at the top of the source;
- a trailing ``/`` means everything below that folder.

A rule applies to the documents of the source it is set on. A document held by several sources
gets the rules of all of them; ``exclude`` wins over ``demote``. Files named in search's
``files`` filter are never excluded.
"""

from __future__ import annotations

import bisect
import functools
import math
import unicodedata
from collections.abc import Iterable
from typing import Any

from .db import fts_fold

EFFECTS = ("demote", "exclude")
DEMOTE_PLACES = 10  # a demoted file goes below this many other files of the fused order
MAX_PATTERNS = 20  # per effect and source (keeps list_sources under the wire cap)
MAX_PATTERN_CHARS = 100
# A pattern matching all of these matches any path, so it would demote or hide everything.
# Every name is longer than a pattern can be, so "?" runs ("**??", "?*?") that only skip very
# short names are caught too; an exact length ("**/??") still matches none of them.
_PROBE = 2 * 60  # > MAX_PATTERN_CHARS
MATCH_ALL_PROBES = tuple(
    "/".join(letter * _PROBE + suffix for letter, suffix in shape)
    for shape in (
        (("a", ""),),  # a Google Doc at the top: no extension
        (("b", ".txt"),),
        (("c", ""), ("d", "")),
        (("e", ""), ("f", ""), ("g", ".docx")),
        (("h", ""), ("i", ""), ("j", ""), ("k", ".pdf")),
        (("l", ".m"), ("n", "")),  # a folder name with a dot
    )
)


def fold(text: str) -> str:
    """Lower-case and accent-free, the way patterns and paths are compared."""
    decomposed = unicodedata.normalize("NFD", fts_fold(text).lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def clean(patterns: Any, name: str) -> list[str]:
    """Validated patterns (stripped, duplicates dropped, order kept). Messages never repeat a
    value."""
    if not isinstance(patterns, list | tuple):
        raise ValueError(f"{name} must be a list of path patterns")
    result: list[str] = []
    for value in patterns:
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a list of strings")
        pattern = value.strip()
        if not pattern or pattern in {"/", "**", "/**"}:
            raise ValueError(f"{name}: an empty or match-everything pattern is not allowed")
        if len(pattern) > MAX_PATTERN_CHARS:
            raise ValueError(f"{name}: a pattern is longer than {MAX_PATTERN_CHARS} characters")
        if any(unicodedata.category(c) == "Cc" for c in pattern):
            raise ValueError(f"{name}: a pattern contains a control character")
        if all(matches(pattern, probe) for probe in MATCH_ALL_PROBES):
            raise ValueError(
                f"{name}: a pattern matches every file path (like * or **/*); "
                "name a folder or a file type instead"
            )
        if pattern not in result:
            result.append(pattern)
    if len(result) > MAX_PATTERNS:
        raise ValueError(f"{name}: at most {MAX_PATTERNS} patterns")
    return result


class Glob:
    """One pattern as a small NFA run in step with the path: time is linear in the path length
    times the pattern length, so no pattern (``**a**a**a…``) can backtrack exponentially."""

    def __init__(self, pattern: str):
        text = fold(pattern)
        anchored = text.startswith("/")
        text = text.lstrip("/")
        if text.endswith("/"):
            text += "**"
        tokens: list[tuple[str, str | int]] = []

        def any_folders() -> None:  # "**/": nothing, or anything ending in "/"
            start = len(tokens)
            tokens.extend([("skip", 0), ("dstar", ""), ("lit", "/")])
            tokens[start] = ("skip", len(tokens))

        if not anchored:
            any_folders()
        i = 0
        while i < len(text):
            if text.startswith("**/", i):
                any_folders()
                i += 3
            elif text.startswith("**", i):
                tokens.append(("dstar", ""))
                i += 2
            elif text[i] == "*":
                tokens.append(("star", ""))
                i += 1
            elif text[i] == "?":
                tokens.append(("one", ""))
                i += 1
            else:
                tokens.append(("lit", text[i]))
                i += 1
        self.tokens = tokens
        self._closures: dict[int, frozenset[int]] = {}

    def _closure(self, state: int) -> frozenset[int]:
        known = self._closures.get(state)
        if known is not None:
            return known
        seen: set[int] = set()
        stack = [state]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            if current < len(self.tokens):
                kind, value = self.tokens[current]
                if kind == "skip":
                    stack += [current + 1, int(value)]
                elif kind in {"star", "dstar"}:
                    stack.append(current + 1)
        result = frozenset(seen)
        self._closures[state] = result
        return result

    def match(self, folded_path: str) -> bool:
        """``folded_path`` is already ``fold``-ed; the whole path must match."""
        end = len(self.tokens)
        states = self._closure(0)
        for char in folded_path:
            following: set[int] = set()
            for state in states:
                if state == end:
                    continue
                kind, value = self.tokens[state]
                if kind == "lit" and char == value or kind == "one" and char != "/":
                    following |= self._closure(state + 1)
                elif kind == "dstar" or kind == "star" and char != "/":
                    following |= self._closure(state)
            if not following:
                return False
            states = frozenset(following)
        return end in states


@functools.lru_cache(maxsize=1024)
def compile_pattern(pattern: str) -> Glob:
    return Glob(pattern)


def demote(
    order: list[int],
    document_of: dict[int, int],
    demoted: set[int],
    scores: dict[int, float],
) -> list[int]:
    """Put every hit of each demoted document below the best hit of the next
    ``DEMOTE_PLACES`` other (not demoted) documents after its own best hit (below all of them
    when fewer follow), and return the order by the new scores. ``order`` is chunk ids, best
    first; ``document_of`` maps them to document ids; ``demoted`` holds document ids.

    A demoted hit's score is lowered to just below that anchor hit, or kept when it is already
    lower, so its weaker hits stay where their own score puts them instead of moving with the
    best one past stronger hits of other files. Other hits keep their scores and order; ties
    keep the old order. Scores are therefore in order, and the result does not depend on how
    many hits are shown.

    By document, not by chunk: a file with many matching chunks must drop as a whole. A fixed
    shift, not a multiplier: an old copy whose name matches the query keeps far more signals
    than the canonical file, and any factor that fixes one query is arbitrary for the next."""
    first: dict[int, int] = {}
    for position, chunk_id in enumerate(order):
        first.setdefault(document_of.get(chunk_id, -chunk_id), position)
    others = sorted(p for d, p in first.items() if d not in demoted)
    kept = [c for c in order if document_of.get(c, -c) not in demoted]
    if not kept:
        return order
    lowest = scores[kept[-1]]
    for chunk_id in order:
        document = document_of.get(chunk_id, -chunk_id)
        if document not in demoted:
            continue
        after = bisect.bisect_right(others, first[document])
        if after + DEMOTE_PLACES <= len(others):
            anchor = scores[order[others[after + DEMOTE_PLACES - 1]]]
        else:
            anchor = lowest
        scores[chunk_id] = min(scores[chunk_id], math.nextafter(anchor, 0.0))
    place = {chunk_id: index for index, chunk_id in enumerate(order)}
    return sorted(order, key=lambda chunk_id: (-scores[chunk_id], place[chunk_id]))


def matches(pattern: str, path: str) -> bool:
    return compile_pattern(pattern).match(fold(path))


def effects(
    rules: Iterable[tuple[int, str, str]], documents: Iterable[tuple[int, str, int]]
) -> dict[int, tuple[str, str]]:
    """``{document id: (effect, pattern)}`` for documents a rule hits. ``rules`` are
    ``(source id, effect, pattern)``, ``documents`` ``(document id, path, source id)`` with one
    row per source membership. ``exclude`` wins over ``demote``; the first matching pattern of
    an effect is reported."""
    by_source = group(rules)
    return combine(
        (document_id, row_effect(by_source.get(source_id, []), path))
        for document_id, path, source_id in documents
    )


def group(rules: Iterable[tuple[int, str, str]]) -> dict[int, list[tuple[str, str]]]:
    """``{source id: [(effect, pattern), ...]}`` in rule order."""
    grouped: dict[int, list[tuple[str, str]]] = {}
    for source_id, effect, pattern in rules:
        grouped.setdefault(source_id, []).append((effect, pattern))
    return grouped


def row_effect(rules: list[tuple[str, str]], path: str) -> tuple[str, str] | None:
    """The effect of one source's rules on one path: the first matching exclude, else the
    first matching demote, else None."""
    folded = fold(path)
    demoted: tuple[str, str] | None = None
    for effect, pattern in rules:
        if (effect == "exclude" or demoted is None) and compile_pattern(pattern).match(folded):
            if effect == "exclude":
                return effect, pattern
            demoted = (effect, pattern)
    return demoted


def combine(
    rows: Iterable[tuple[int, tuple[str, str] | None]],
) -> dict[int, tuple[str, str]]:
    """Per document over its source memberships: exclude wins, else the first demote."""
    output: dict[int, tuple[str, str]] = {}
    for document_id, effect in rows:
        if effect is None or output.get(document_id, ("",))[0] == "exclude":
            continue
        if effect[0] == "exclude" or document_id not in output:
            output[document_id] = effect
    return output
